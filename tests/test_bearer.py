import pytest
from starlette.testclient import TestClient

from woo_connector.app import build_http_app, build_server
from woo_connector.auth.bearer import BearerGuard

TOKEN = "s3cret-token"


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


async def run(guard, scope):
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    await guard(scope, receive, send)
    return sent


def http_scope(auth=None, path="/mcp", method="POST"):
    headers = [(b"authorization", auth.encode())] if auth is not None else []
    return {"type": "http", "method": method, "path": path, "headers": headers}


@pytest.mark.parametrize("auth", [None, "", "Bearer", "Bearer wrong", "Basic " + TOKEN, "bearer", TOKEN, "Bearer  "])
async def test_missing_wrong_or_malformed_token_is_401(auth):
    inner = Recorder()
    sent = await run(BearerGuard(inner, TOKEN), http_scope(auth))
    assert sent[0]["status"] == 401 and inner.calls == []
    assert (b"www-authenticate", b'Bearer realm="woo-connector"') in sent[0]["headers"]


async def test_correct_token_passes_and_scheme_is_case_insensitive():
    inner = Recorder()
    for auth in (f"Bearer {TOKEN}", f"bearer {TOKEN}"):
        sent = await run(BearerGuard(inner, TOKEN), http_scope(auth))
        assert sent[0]["status"] == 200
    assert inner.calls == ["http", "http"]


async def test_websocket_is_closed_never_forwarded():
    inner = Recorder()
    sent = await run(BearerGuard(inner, TOKEN), {"type": "websocket", "path": "/mcp", "headers": []})
    assert sent == [{"type": "websocket.close", "code": 1008}] and inner.calls == []


async def test_lifespan_passes_through_and_unknown_scopes_fail_closed():
    inner = Recorder()
    await run(BearerGuard(inner, TOKEN), {"type": "lifespan"})
    await run(BearerGuard(inner, TOKEN), {"type": "something-new"})
    assert inner.calls == ["lifespan"]


async def test_healthz_is_open_but_only_for_get():
    inner = Recorder()
    assert (await run(BearerGuard(inner, TOKEN), http_scope(path="/healthz", method="GET")))[0]["status"] == 200
    assert (await run(BearerGuard(inner, TOKEN), http_scope(path="/healthz", method="POST")))[0]["status"] == 401


def test_empty_token_is_refused():
    with pytest.raises(ValueError):
        BearerGuard(Recorder(), "")


def test_full_http_app_healthz_and_mcp_auth():
    app = build_http_app(build_server(), TOKEN)
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        health = http.get("/healthz")
        assert health.status_code == 200 and set(health.json()) == {"status", "version"}
        assert http.post("/mcp", json=init, headers=headers).status_code == 401
        assert http.post("/mcp", json=init, headers={**headers, "Authorization": "Bearer nope"}).status_code == 401
        ok = http.post("/mcp", json=init, headers={**headers, "Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200, ok.text
