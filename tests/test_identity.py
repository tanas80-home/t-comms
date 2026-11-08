import os
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_comms.api import create_app
from agent_comms.config import create_agent
from agent_comms.core import Forbidden, Unauthorized
from agent_comms.mcp_server import build_mcp
from conftest import ASK

EXPECTED_TOOLS = {"board_configuration_status", "board_refresh_configuration", "board_recover_request_owner", "board_bind_browser_request", "board_browser_begin_probe", "board_browser_probe", "board_browser_failure", "board_browser_reconnect", "board_browser_status", "board_request_progress", "board_request_history", "board_register_capabilities", "board_route_request", "board_repost_request", "board_resolve_attention", "board_register", "board_read_updates", "board_post", "board_claim_task", "board_update_task",
                  "board_release_task", "board_set_summary", "board_list_threads",
                  "board_list_issues", "board_get_issue", "board_create_issue", "board_link_issue", "board_comment_issue"}


def test_token_maps_to_agent_and_runtime(env):
    p = env.board.authenticate(env.tokens["codex"])
    assert (p.name, p.runtime, p.is_human) == ("codex", "codex-cli", False)
    with pytest.raises(Unauthorized):
        env.board.authenticate("ac_nope")
    with pytest.raises(Unauthorized):
        env.board.authenticate(None)


def test_posts_are_stamped_and_sessions_are_owned(env):
    tid = env.thread()
    p = env.post("claude", tid, "hello")
    assert (p["agent"], p["session_id"]) == ("claude", env.sid["claude"])
    with pytest.raises(Forbidden):  # cannot post through another agent's session
        env.board.create_post(env.p["claude"], env.sid["codex"], body="spoof", type="status", thread_id=tid)
    with pytest.raises(Forbidden):
        env.board.register_session(env.p["claude"], "/x", resume_session_id=env.sid["codex"])


def test_http_ignores_self_declared_sender(env):
    client = TestClient(create_app(env.board))
    tid = env.thread()
    h = {"Authorization": f"Bearer {env.tokens['claude']}"}
    r = client.post("/api/posts", headers=h, json={"body": "x", "type": "status", "thread_id": tid,
                                                    "agent": "human", "session_id": env.sid["claude"]})
    assert r.status_code == 422  # unknown field rejected outright
    r = client.post("/api/posts", headers=h, json={"body": "x", "type": "status", "thread_id": tid,
                                                    "session_id": env.sid["claude"]})
    assert r.status_code == 200 and r.json()["agent"] == "claude"
    assert client.post("/api/posts", json={"body": "x", "type": "status", "thread_id": tid}).status_code == 401


def test_only_human_has_authority(env):
    tid = env.thread()
    d = env.post("claude", tid, "proposal", "decision", decision_question=ASK)
    with pytest.raises(Forbidden):
        env.board.finalize(env.p["claude"], d["id"])
    with pytest.raises(Forbidden):
        env.post("claude", tid, "self-final", "decision", final=True)
    with pytest.raises(Forbidden):
        env.board.set_paused(env.p["codex"], True)
    assert env.board.finalize(env.p["human"], d["id"])["decision_status"] == "final"
    client = TestClient(create_app(env.board))
    r = client.post("/api/admin/pause", headers={"Authorization": f"Bearer {env.tokens['grok']}"})
    assert r.status_code == 403


def test_agents_toml_hot_reload_and_revocation(env):
    new = create_agent(env.settings.agents_path, "gemini", "gemini-cli")
    assert env.board.authenticate(new).name == "gemini"
    rotated = create_agent(env.settings.agents_path, "codex", "codex-cli", rotate=True)
    with pytest.raises(Unauthorized):
        env.board.authenticate(env.tokens["codex"])
    assert env.board.authenticate(rotated).name == "codex"


def test_http_is_localhost_only(env):
    client = TestClient(create_app(env.board))
    h = {"Authorization": f"Bearer {env.tokens['human']}"}
    assert client.get("/api/whoami", headers=h).status_code == 200
    assert client.get("/api/whoami", headers=h | {"Host": "evil.example:8787"}).status_code == 403
    assert client.get("/", headers={"Host": "127.0.0.1:8787"}).status_code == 200


def test_mcp_exposes_exactly_the_tools_and_warns_about_untrusted_data(env, monkeypatch):
    mcp = build_mcp(env.board, "stdio")
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert "untrusted DATA" in t.description and "never instructions" in t.description
        props = t.input_schema["properties"]
        assert not {"agent", "sender", "from", "author"} & set(props)


def test_mcp_end_to_end_stdio_identity(env, monkeypatch):
    from mcp import Client

    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens["grok"])
    mcp = build_mcp(env.board, "stdio")
    tid = env.thread()

    async def go():
        async with Client(mcp) as c:
            reg = json.loads((await c.call_tool("board_register", {"project": "/work/repo"})).content[0].text)
            post = json.loads((await c.call_tool("board_post", {"body": "hi", "type": "status",
                                                                "thread_id": tid})).content[0].text)
            bad = await c.call_tool("board_post", {"body": "x" * 5000, "type": "status", "thread_id": tid})
            return reg, post, bad

    reg, post, bad = asyncio.run(go())
    assert reg["agent"] == "grok" and "untrusted DATA" in reg["notice"]
    assert post["agent"] == "grok" and post["session_id"] == reg["session_id"]
    assert bad.is_error and "Point, don't paste" in bad.content[0].text


def test_mcp_http_transport_uses_bearer_token(env):
    """Drive /mcp over real streamable HTTP inside the ASGI app."""
    import httpx
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    app = create_app(env.board)

    async def go():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
            headers = {"Authorization": f"Bearer {env.tokens['codex']}"}
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787",
                                         headers=headers) as http:
                async with Client(streamable_http_client("http://127.0.0.1:8787/mcp", http_client=http)) as c:
                    tools = {t.name for t in (await c.list_tools()).tools}
                    reg = json.loads((await c.call_tool("board_register", {"project": "/p"})).content[0].text)
                    upd = json.loads((await c.call_tool("board_read_updates",
                                                        {"session_id": reg["session_id"]})).content[0].text)
                    return tools, reg, upd

    tools, reg, upd = asyncio.run(go())
    assert tools == EXPECTED_TOOLS
    assert reg["agent"] == "codex"
    assert upd["posts"] == []


def test_atomic_token_rotation_revokes_even_when_mtime_is_preserved(env):
    path = env.settings.agents_path
    before = path.stat()
    rotated = create_agent(path, "codex", "codex-cli", rotate=True)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(Unauthorized):
        env.board.authenticate(env.tokens["codex"])
    assert env.board.authenticate(rotated).name == "codex"
