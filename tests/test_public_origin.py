"""Public HTTPS through a loopback proxy retains browser and MCP boundaries."""

import asyncio
import io
import json
from urllib.parse import urlsplit

import httpx
import pytest
from conftest import ASK, PROJECT
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agent_comms import cli
from agent_comms.api import create_app

ORIGIN = "https://comms.example.test"
DASH = {"X-Board-Request": "1", "Origin": ORIGIN}


def test_public_login_cookie_csrf_and_logout(env):
    env.settings.public_origin = ORIGIN
    app = create_app(env.board)
    human = {"Authorization": f"Bearer {env.tokens['human']}"}
    agent = {"Authorization": f"Bearer {env.tokens['codex']}"}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post("/api/login-links", headers=agent).status_code == 403
        assert client.get("/api/whoami").status_code == 401
        link = client.post("/api/login-links", headers=human).json()["url"]
        assert link.startswith(f"{ORIGIN}/login/")
        login = client.get(urlsplit(link).path, follow_redirects=False)
        assert login.status_code == 303
        cookie = login.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        identity = client.get("/api/whoami", headers=DASH).json()
        assert identity["is_human"] is True
        assert client.get("/api/whoami").status_code == 403
        hostile = DASH | {"Origin": "https://other.example.test"}
        assert client.get("/api/whoami", headers=hostile).status_code == 403
        logout = client.post("/api/web-sessions/logout", headers=DASH)
        assert logout.status_code == 200
        assert client.get("/api/whoami", headers=DASH).status_code == 401


def test_proxy_headers_cannot_authorize_unconfigured_host_or_remote_peer(env):
    env.settings.public_origin = ORIGIN
    with TestClient(create_app(env.board), base_url="https://other.test") as client:
        spoof = {"X-Forwarded-Host": "comms.example.test",
                 "X-Forwarded-Proto": "https"}
        assert client.get("/", headers=spoof).status_code == 403
    with TestClient(
        create_app(env.board), base_url=ORIGIN, client=("192.0.2.9", 41000)
    ) as client:
        assert client.get("/").status_code == 403


@pytest.mark.parametrize("origin", [
    "http://comms.example.test", ORIGIN + "/path",
    "https://user:pass@comms.example.test",
])
def test_invalid_public_origin_is_rejected_before_serving(env, origin):
    env.settings.public_origin = origin
    with pytest.raises(ValueError, match="public_origin"):
        create_app(env.board)


def test_native_mcp_public_origin_validation(env):
    env.settings.public_origin = ORIGIN
    message = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "public-test", "version": "test"}},
    }
    headers = {"Authorization": f"Bearer {env.tokens['codex']}",
               "Accept": "application/json, text/event-stream", "Origin": ORIGIN}
    with TestClient(create_app(env.board), base_url=ORIGIN) as client:
        good = client.post("/mcp", json=message, headers=headers)
        assert good.status_code == 200
        if good.headers["content-type"].startswith("text/event-stream"):
            result = next(
                json.loads(line[6:]) for line in good.iter_lines()
                if line.startswith("data: "))
        else:
            result = good.json()
        assert result["id"] == 1 and "protocolVersion" in result["result"]
        bad = client.post(
            "/mcp", json=message,
            headers=headers | {"Origin": "https://other.example.test"})
        assert bad.status_code == 403


def test_public_mcp_question_human_reply_and_project_context(env):
    env.settings.public_origin = ORIGIN
    other = env.board.create_thread(
        env.p["human"], env.sid["human"], "Other scope", "/work/other")["id"]
    hidden = env.post("human", other, "Unrelated maintenance")
    app = create_app(env.board)

    async def go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
        headers = {"Authorization": f"Bearer {env.tokens['codex']}",
                   "Origin": ORIGIN}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, headers=headers) as http,
            Client(streamable_http_client(
                ORIGIN + "/mcp", http_client=http)) as agent,
        ):
            async def call(name, arguments):
                result = await agent.call_tool(name, arguments)
                assert not result.is_error, result
                return json.loads(result.content[0].text)

            tools = {tool.name for tool in (await agent.list_tools()).tools}
            assert {"board_register", "board_post", "board_read_updates"} <= tools
            registered = await call("board_register", {"project": PROJECT})
            sid = registered["session_id"]
            question = await call("board_post", {
                "session_id": sid, "new_thread_title": "Maintenance window",
                "body": "Which window?", "type": "question",
                "needs_response": True, "decision_question": ASK})
            path = f"/api/posts/{question['id']}/resolve"
            answer = {"action": "reply", "text": "After 22:00 UTC"}
            refused = await http.post(ORIGIN + path, json=answer)
            assert refused.status_code == 403
            async with httpx.AsyncClient(
                transport=transport, base_url=ORIGIN
            ) as human:
                bearer = {"Authorization": f"Bearer {env.tokens['human']}"}
                minted = await human.post("/api/login-links", headers=bearer)
                assert (await human.get(minted.json()["url"])).status_code == 303
                reply = await human.post(path, json=answer, headers=DASH)
                assert reply.status_code == 200
            updates = await call("board_read_updates", {"session_id": sid})
            assert any(p["body"] == answer["text"] for p in updates["posts"])
            assert all(p["id"] != hidden["id"] for p in updates["posts"])

    asyncio.run(go())


@pytest.mark.parametrize("origin", [ORIGIN, "https://other.test", "http://127.0.0.1:8787"])
def test_dashboard_cli_accepts_only_the_configured_public_login_origin(
    env, monkeypatch, origin
):
    env.settings.public_origin = ORIGIN
    with TestClient(create_app(env.board)) as server:
        def urlopen(request, timeout):
            assert request.full_url == "http://127.0.0.1:8787/api/login-links"
            reply = server.post(
                "/api/login-links",
                headers={"Authorization": request.get_header("Authorization")})
            assert reply.status_code == 200
            payload = reply.json()
            payload["url"] = payload["url"].replace(ORIGIN, origin, 1)
            return io.BytesIO(json.dumps(payload).encode())

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        if origin == ORIGIN:
            assert cli._login_link(env.settings, env.tokens["human"]).startswith(
                ORIGIN + "/login/")
        else:
            with pytest.raises(SystemExit, match="unexpected sign-in link"):
                cli._login_link(env.settings, env.tokens["human"])
