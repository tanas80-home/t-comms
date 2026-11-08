"""Paths, settings (board.toml, plus an optional per-machine board.local.toml) and agent tokens (agents.toml)."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RUNTIME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
LOCAL_SETTINGS = "board.local.toml"   # per-machine overrides next to board.toml; gitignored
SECTIONS = ("server", "limits", "tasks", "web")
NOT_SETTINGS = {"dispatch", "conversations", "unstick", "config_path"}   # Settings fields that are not [server]/[limits]/[tasks]/[web] keys
PREVENTION_KEYS = ("prevention_owner", "prevention_thread", "prevention_forward_to")   # [unstick] (prevention_config)


@dataclass(frozen=True)
class PreventionConfig:
    """[unstick] prevention_owner / prevention_thread: where Unstick and automatic recovery send prevention
    proposals (agent_comms/prevention.py). Off unless both are set. prevention_forward_to (optional): the agent the
    owner may forward a proposal to when it needs a code change (a triage owner forwarding to the maintainer)."""
    owner: str
    thread_id: int
    forward_to: str | None = None


def prevention_config(table: object) -> PreventionConfig | None:
    """The [unstick] table, validated strictly: only prevention_owner (an agent name) and prevention_thread (a thread
    id, a positive whole number), both or neither, plus the optional prevention_forward_to (an agent name other than
    the owner, only with an owner). None when off (absent, or both left empty). Raises ValueError."""
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ValueError("[unstick] must be a table")
    unknown = sorted(set(table) - set(PREVENTION_KEYS))
    if unknown:
        raise ValueError(f"unknown setting [unstick] {unknown[0]}; known: {', '.join(PREVENTION_KEYS)}")
    owner = table.get("prevention_owner", "")
    thread = table.get("prevention_thread", 0)
    if not isinstance(owner, str) or (owner and not NAME_RE.match(owner)):
        raise ValueError("[unstick] prevention_owner must be an agent name (lowercase letters, digits, - and _), "
                         "or \"\" for off")
    if isinstance(thread, bool) or not isinstance(thread, int) or thread < 0:
        raise ValueError("[unstick] prevention_thread must be a thread id (a positive whole number), or 0 for off")
    forward_to = table.get("prevention_forward_to", "")
    if not isinstance(forward_to, str) or (forward_to and not NAME_RE.match(forward_to)):
        raise ValueError("[unstick] prevention_forward_to must be an agent name (lowercase letters, digits, - and _), "
                         "or \"\" for off")
    if not owner and not thread:
        if forward_to:
            raise ValueError("[unstick] prevention_forward_to needs prevention_owner and prevention_thread")
        return None
    if not owner or not thread:
        raise ValueError("[unstick] set both prevention_owner and prevention_thread, or neither")
    if forward_to == owner:
        raise ValueError("[unstick] prevention_forward_to must be another agent than prevention_owner")
    return PreventionConfig(owner, thread, forward_to or None)


def deep_merge(base: dict, over: dict) -> dict:
    """`over` wins. Tables merge key by key, recursively; any other value (including a list) replaces wholesale."""
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def home() -> Path:
    return Path(os.environ.get("AGENT_COMMS_HOME", REPO_ROOT)).expanduser()


def human_token_file() -> Path:
    return Path(os.environ.get("AGENT_COMMS_TOKEN_FILE", "~/.config/agent-comms/human.token")).expanduser()


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    # Optional HTTPS origin behind a loopback reverse proxy.
    public_origin: str = ""
    # [server] hub: on a machine that is not the hub, the hub's gateway URL; `board mcp` and `board brief` then talk to
    # it instead of a local database (remote.py). Empty on the hub itself.
    hub: str = ""
    db_path: Path = field(default_factory=lambda: home() / "data" / "board.db")
    agents_path: Path = field(default_factory=lambda: home() / "agents.toml")
    lease_ttl_minutes: int = 30
    max_agent_posts_per_thread_without_human: int = 12
    daily_post_cap_per_agent: int = 200
    body_max_bytes: int = 4096
    max_refs: int = 20
    require_human_accept: bool = False
    # [tasks]: the dispatcher automatically asks the owner (or creator) of abandoned or orphaned work to recover it,
    # once per stall (agent_comms/autorecover.py). The human's setting; agents cannot change it.
    auto_recover_stalled_work: bool = True
    # [web]: dashboard sign-in sessions (see weblogin.py). File-only: the Settings page cannot change them.
    session_days: int = 30        # sliding: a session unused this long ends; use renews it (at most hourly)
    session_max_days: int = 90    # absolute: a session ends this long after sign-in, however much it is used
    # The raw [dispatch] table (merged across layers); validated by dispatch.DispatchConfig.
    dispatch: dict = field(default_factory=dict)
    # The raw [conversations] table: dashboard links to agents' own conversations; validated by
    # conversations.ConversationConfig.
    conversations: dict = field(default_factory=dict)
    # The raw [unstick] table: the prevention inbox (prevention_owner, prevention_thread); validated strictly by
    # prevention_config when the settings are read, so a bad value is refused at start and on reload.
    unstick: dict = field(default_factory=dict)
    # The board.toml these settings came from (board.local.toml is beside it), or None when they were built in
    # code. Not a setting: it lets a running Board hot-reload them (Board.reload_settings) and the Settings page
    # save edits to board.local.toml.
    config_path: Path | None = field(default=None, compare=False)

    @classmethod
    def known_keys(cls) -> set[str]:
        return {f.name for f in fields(cls)} - NOT_SETTINGS

    @classmethod
    def read_layer(cls, layer: Path) -> dict:
        """One settings file, parsed, with its [server]/[limits]/[tasks]/[web] keys checked ({} when it is absent)."""
        if not layer.exists():
            return {}
        d = tomllib.loads(layer.read_text())
        known = cls.known_keys()
        for section in SECTIONS:
            table = d.get(section, {})
            if not isinstance(table, dict):
                raise ValueError(f"[{section}] must be a table in {layer}")
            for k in table:
                if k not in known:
                    raise ValueError(f"unknown setting [{section}] {k} in {layer}")
        return d

    @classmethod
    def read_layers(cls, path: Path | None = None, local: bool = True) -> dict:
        """board.toml, then board.local.toml beside it (when `local` and present), deep-merged per section.
        Each layer's [server]/[limits]/[tasks]/[web] keys are checked, so an error names the file at fault."""
        path = path or home() / "board.toml"
        data: dict = {}
        for layer in [path] + ([path.with_name(LOCAL_SETTINGS)] if local else []):
            data = deep_merge(data, cls.read_layer(layer))
        return data

    @classmethod
    def from_data(cls, data: dict) -> "Settings":
        """Settings from already-merged layers (see read_layers)."""
        s = cls()
        for section in SECTIONS:
            for k, v in data.get(section, {}).items():
                setattr(s, k, Path(v).expanduser() if k.endswith("_path") else v)
        s.dispatch = data.get("dispatch", {})
        s.conversations = data.get("conversations", {})
        s.unstick = data.get("unstick", {})
        prevention_config(s.unstick)   # strict: raises ValueError naming the bad key
        for p in ("db_path", "agents_path"):
            val = getattr(s, p)
            if not val.is_absolute():
                setattr(s, p, home() / val)
        return s

    @classmethod
    def load(cls, path: Path | None = None, local: bool = True) -> "Settings":
        path = path or home() / "board.toml"
        s = cls.from_data(cls.read_layers(path, local))
        # Only a board that reads the overlay can hot-reload it or save edits to it.
        s.config_path = path if local else None
        return s


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "ac_" + secrets.token_urlsafe(32)


@dataclass
class AgentSpec:
    name: str
    runtime: str
    token_sha256: str
    is_human: bool = False


def read_agents(path: Path) -> dict[str, AgentSpec]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text()).get("agents", {})
    out = {}
    for name, d in data.items():
        out[name] = AgentSpec(name, d["runtime"], d["token_sha256"], bool(d.get("is_human", False)))
    return out


def write_agents(path: Path, agents: dict[str, AgentSpec]) -> None:
    lines = [
        "# agent-comms identities. GITIGNORED. Only SHA-256 hashes of tokens are stored here;",
        "# the plaintext token is printed once by `board create-agent`.",
        "",
    ]
    for a in agents.values():
        lines += [f"[agents.{a.name}]", f'runtime = "{a.runtime}"', f'token_sha256 = "{a.token_sha256}"']
        if a.is_human:
            lines.append("is_human = true")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def check_create_agent(path: Path, name: str, runtime: str, is_human: bool = False,
                       rotate: bool = False) -> dict[str, AgentSpec]:
    """Raise ValueError unless create_agent(...) with these arguments would succeed; returns the current agents."""
    if not NAME_RE.match(name):
        raise ValueError("agent name must match [a-z][a-z0-9_-]{0,31}")
    if not re.match(r"^[A-Za-z0-9._-]{1,40}$", runtime):
        raise ValueError("runtime must be a short identifier, e.g. claude-code, codex-cli")
    agents = read_agents(path)
    if name in agents and not rotate:
        raise ValueError(f"agent {name!r} already exists (use --rotate to issue a new token)")
    if is_human and any(a.is_human for a in agents.values() if a.name != name):
        raise ValueError("a human agent already exists; v1 supports exactly one human")
    return agents


def create_agent(path: Path, name: str, runtime: str, is_human: bool = False, rotate: bool = False,
                 token: str | None = None) -> str:
    """Add an agent (or rotate its token) and return the plaintext token (`token`: one the caller already stored)."""
    agents = check_create_agent(path, name, runtime, is_human, rotate)
    token = token or new_token()
    agents[name] = AgentSpec(name, runtime, hash_token(token), is_human)
    write_agents(path, agents)
    return token


def token_dir() -> Path:
    return Path(os.environ.get("AGENT_COMMS_TOKEN_DIR", "~/.config/agent-comms")).expanduser()


def agent_token_file(name: str) -> Path:
    """~/.config/agent-comms/<name>.token: the file `board mcp --agent <name>` reads."""
    if not NAME_RE.match(name):
        raise ValueError("invalid agent name")
    return token_dir() / f"{name}.token"


def stage_private(path: Path, text: str, tighten: bool = False) -> Path:
    """Write `text` to a new private temp file beside `path` (mode 600, flushed to disk) and return it; the caller
    renames it over `path` (os.replace, atomic) or deletes it. Missing directories are created 700. `tighten` (only for
    the board's own token directory): the directory must be a real directory owned by the user and is tightened to 700
    if it already exists; otherwise an existing directory's permissions are never changed."""
    import stat
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if tighten:
        meta = d.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
            raise ValueError(f"{d} must be a directory owned by you")
        if stat.S_IMODE(meta.st_mode) & 0o077:
            os.chmod(d, 0o700)
    tmp = d / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def write_private(path: Path, text: str, overwrite: bool = False, tighten: bool = False) -> None:
    """Write a file only its owner can read (mode 600), atomically. Refuses to replace an existing file unless
    `overwrite`, and never follows a symlink. `tighten`: see stage_private (the token directory only)."""
    if path.is_symlink() or (path.exists() and not overwrite):
        raise ValueError(f"{path} already exists")
    tmp = stage_private(path, text, tighten=tighten)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_agent_token(agents_path: Path, name: str, runtime: str, rotate: bool = False) -> Path:
    """`board create-agent --save-token`: create (or rotate) the agent and store its token in agent_token_file(name),
    keeping the old token registered and on disk on any failure. Order: validate against agents.toml; stage the new
    token privately beside the file; hard-link the old file (if any) to a private backup; rename the staged file over
    it; update agents.toml last; drop the backup. If agents.toml cannot be updated, the backup is renamed back. Returns
    the token file."""
    f = agent_token_file(name)
    if f.is_symlink() or (f.exists() and not rotate):
        raise ValueError(f"{f} already exists; pass --rotate to replace the agent's token")
    check_create_agent(agents_path, name, runtime, False, rotate)
    token = new_token()
    staged = stage_private(f, token + "\n", tighten=True)
    backup = None
    try:
        if f.exists():
            backup = f.with_name(f".{f.name}.{secrets.token_hex(6)}.bak")
            os.link(f, backup)
        os.replace(staged, f)
    except BaseException:
        staged.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    try:
        create_agent(agents_path, name, runtime, False, rotate, token=token)
    except BaseException as first:
        try:
            if backup is not None:
                os.replace(backup, f)       # the old token is registered again and back on disk
            else:
                f.unlink(missing_ok=True)   # a new agent: no token file without a registered identity
        except OSError as second:
            raise OSError(f"agents.toml was not updated ({first}) and restoring {f} failed ({second}); the old "
                          f"token is in {backup}") from first
        raise
    if backup is not None:
        backup.unlink(missing_ok=True)
    return f


def agent_mcp_config(name: str) -> dict:
    """A Claude Code MCP config (for `claude --strict-mcp-config --mcp-config <file>`) whose agent-comms server signs in
    as `name`: this checkout's stdio launcher, which reads ~/.config/agent-comms/<name>.token. No secrets in it."""
    if not NAME_RE.match(name):
        raise ValueError("invalid agent name")
    launcher = REPO_ROOT / "integrations" / "claude-code" / "stdio.sh"
    return {"mcpServers": {"agent-comms": {"type": "stdio", "command": "bash", "args": [str(launcher)],
                                           "env": {"AGENT_COMMS_HOME": str(home()), "AGENT_COMMS_AGENT": name}}}}


def load_agent_token(name: str) -> str:
    """Read ~/.config/agent-comms/<name>.token, refusing files others could read or swap."""
    import stat

    if not NAME_RE.match(name):
        raise ValueError("invalid agent name")
    path = agent_token_file(name)
    try:
        meta = path.lstat()
    except OSError as e:
        raise ValueError(f"cannot read token file {path}: {e.strerror}") from None
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
        raise ValueError(f"{path} must be a regular file owned by you")
    if stat.S_IMODE(meta.st_mode) & 0o077:
        raise ValueError(f"{path} must have mode 600 or stricter")
    token = path.read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError(f"{path} is empty or malformed")
    return token
