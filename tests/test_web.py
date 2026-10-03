import json

import httpx
import pytest

from tests.conftest import API, STORE, customer, order, page_response, product
from woo_connector.tools import common
from woo_connector.web import app as webmod
from woo_connector.web.app import WebApp

PW = "correct horse"
H = {"X-Requested-With": "woo-web"}
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
NOT_FOUND = httpx.Response(404, json={"code": "x", "message": "Invalid ID."})


@pytest.fixture
def web(tools_ready, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return WebApp(PW, "test-secret")


def make(web):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://127.0.0.1")


async def login(c, password=PW):
    return await c.post("/api/login", json={"password": password}, headers=H)


# ---------------------------------------------------------------- access control


async def test_everything_requires_sign_in(web):
    async with make(web) as c:
        assert (await c.get("/", follow_redirects=False)).headers["location"] == "/login"
        for path in ("/api/overview", "/api/orders", "/api/orders/1", "/api/products", "/api/low-stock", "/api/customers", "/api/info", "/api/config"):
            assert (await c.get(path)).status_code == 401, path
        assert (await c.post("/api/chat", json={"message": "hi"}, headers=H)).status_code == 401
        assert (await c.get("/healthz")).json()["status"] == "ok"
        assert (await c.get("/login")).status_code == 200
        assert (await c.get("/static/app.js")).status_code == 200  # code is public; data is not
        assert (await c.get("/static/../../etc/passwd")).status_code == 404
        assert (await c.get("/static/index.html")).status_code == 404  # only assets are served from /static


async def test_login_sets_a_locked_down_cookie_and_unlocks_the_api(web, router):
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([order()], total=3))
    async with make(web) as c:
        assert (await login(c, "wrong")).status_code == 401
        ok = await login(c)
        assert ok.status_code == 200
        cookie = ok.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert (await c.get("/", follow_redirects=False)).status_code == 200
        assert (await c.get("/login", follow_redirects=False)).status_code == 303
        assert (await c.get("/api/overview")).status_code == 200


async def test_post_without_the_csrf_header_is_refused(web):
    async with make(web) as c:
        assert (await c.post("/api/login", json={"password": PW})).status_code == 403
        await login(c)
        assert (await c.post("/api/logout")).status_code == 403
        assert (await c.post("/api/chat", json={"message": "hi"})).status_code == 403


async def test_brute_force_is_locked_out(web):
    async with make(web) as c:
        codes = [(await login(c, f"guess{i}")).status_code for i in range(6)]
        assert codes == [401] * 5 + [429]
        assert (await login(c)).status_code == 429  # even the right password waits out the window


async def test_tampered_or_expired_cookies_are_rejected(web, monkeypatch):
    async with make(web) as c:
        await login(c)
        good = c.cookies[webmod.COOKIE]
        sid, exp, sig = good.split(".")
        for bad in (f"{sid}.{exp}.{'0' * len(sig)}", f"{sid}.{int(exp) + 9999}.{sig}", "junk", ""):
            c.cookies.clear()
            c.cookies.set(webmod.COOKIE, bad)
            assert (await c.get("/api/overview")).status_code == 401
        c.cookies.clear()
        c.cookies.set(webmod.COOKIE, good)
        real = webmod.time.time
        monkeypatch.setattr(webmod.time, "time", lambda: real() + webmod.SESSION_TTL_S + 10)
        assert (await c.get("/api/overview")).status_code == 401


async def test_logout_revokes_the_session_even_if_the_cookie_is_replayed(web, router):
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([]))
    async with make(web) as c:
        await login(c)
        stolen = c.cookies[webmod.COOKIE]
        assert (await c.post("/api/logout", headers=H)).status_code == 200
        c.cookies.set(webmod.COOKIE, stolen)
        assert (await c.get("/api/overview")).status_code == 401


async def test_security_headers_and_no_store(web):
    async with make(web) as c:
        r = await c.get("/login")
        assert "script-src 'self'" in r.headers["content-security-policy"] and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert (await c.get("/api/overview")).headers["cache-control"] == "no-store"


def test_pages_have_no_inline_script_or_handlers():
    import re

    for name in ("index.html", "login.html"):
        html = (webmod.STATIC / name).read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), name
        assert not re.search(r"\son\w+=", html), name
    js = (webmod.STATIC / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js and "eval(" not in js


# ---------------------------------------------------------------- data API


async def test_overview_orders_and_masking(web, router):
    def handler(request):
        return page_response([order()], total=2)

    router.get(f"{API}/wc/v3/orders").mock(side_effect=handler)
    async with make(web) as c:
        await login(c)
        ov = (await c.get("/api/overview")).json()
        assert ov["counts"]["processing"] == 2 and ov["needs_attention"]
        rows = (await c.get("/api/orders?per_page=5&status=processing")).json()
        assert rows["items"][0]["customer_email"] == "d***@example.com"
        assert "dana@example.com" not in json.dumps(rows)


async def test_search_routes_to_search_orders_with_exact_number_first(web, router):
    router.get(f"{API}/wc/v3/orders/1042").mock(return_value=httpx.Response(200, json=order(1042)))
    route = router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([order(7), order(1042)]))
    async with make(web) as c:
        await login(c)
        rows = (await c.get("/api/orders?q=%231042")).json()
        assert [r["id"] for r in rows["items"]] == [1042, 7]
        assert dict(route.calls.last.request.url.params)["search"] == "#1042"


async def test_bad_parameters_are_rejected_not_forwarded(web, router):
    route = router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([]))
    async with make(web) as c:
        await login(c)
        for path in ("/api/orders?per_page=500", "/api/orders?status=bogus", "/api/orders?page=0"):
            r = await c.get(path)
            assert r.status_code == 400 and r.json() == {"error": "Invalid parameters."}, path
        assert route.call_count == 0


async def test_unknown_order_is_a_404_with_the_next_step(web, router):
    router.get(f"{API}/wc/v3/orders/999999").mock(return_value=NOT_FOUND)
    router.get(f"{API}/wc/v3/orders/999999/notes").mock(return_value=NOT_FOUND)
    async with make(web) as c:
        await login(c)
        r = await c.get("/api/orders/999999")
        assert r.status_code == 404 and "Use search_orders" in r.json()["error"] and "Error executing" not in r.json()["error"]


async def test_hostile_text_stays_json_data(web, router):
    tags = "Hi <img src=x onerror=alert(1)>there<script>alert(2)</script>"
    encoded = "&lt;img src=x onerror=alert(3)&gt;"  # decodes to literal characters: safe only because the UI inserts text, never HTML
    router.get(f"{API}/wc/v3/orders/5").mock(return_value=httpx.Response(200, json=order(5, customer_note=tags)))
    router.get(f"{API}/wc/v3/orders/5/notes").mock(
        return_value=httpx.Response(200, json=[{"note": encoded, "customer_note": True, "author": "system", "date_created_gmt": "2026-09-01T00:00:00"}])
    )
    async with make(web) as c:
        await login(c)
        r = await c.get("/api/orders/5")
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        assert body["customer_note_text"] == "Hi there"  # tags and script content stripped server-side
        assert body["notes"][0]["note_text"] == "<img src=x onerror=alert(3)>"  # literal text; see tests/ui_smoke.mjs for the rendering check


async def test_stock_products_customers_endpoints(web, router):
    router.get(f"{API}/wc/v3/products").mock(return_value=page_response([product(manage_stock=True, stock_quantity=2)]))
    router.get(f"{API}/wc/v3/customers").mock(return_value=page_response([customer(), customer(id=1, role="administrator")]))
    router.get(f"{API}/wc/v3/customers/1").mock(return_value=httpx.Response(200, json=customer(id=1, role="administrator")))
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([]))
    async with make(web) as c:
        await login(c)
        low = (await c.get("/api/low-stock?threshold=5")).json()
        assert low["items"][0]["issue"] == "low"
        assert [x["id"] for x in (await c.get("/api/customers")).json()["items"]] == [7]
        assert (await c.get("/api/customers/1")).status_code == 404  # staff accounts are never customers


async def test_config_reports_whether_chat_is_available(web, monkeypatch):
    async with make(web) as c:
        await login(c)
        assert (await c.get("/api/config")).json()["chat_enabled"] is False
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert (await c.get("/api/config")).json()["chat_enabled"] is True


# ---------------------------------------------------------------- chat


def reply(content=None, tool_calls=None):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]})


async def test_chat_is_off_without_a_key(web):
    async with make(web) as c:
        await login(c)
        r = await c.post("/api/chat", json={"message": "hi"}, headers=H)
        assert r.status_code == 503 and "OPENROUTER_API_KEY" in r.json()["error"]


async def test_chat_answers_uses_tools_and_keeps_per_session_history(web, router, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([order()]))
    call = {"id": "c1", "type": "function", "function": {"name": "list_orders", "arguments": "{}"}}
    llm = router.post(OR_URL).mock(side_effect=[reply(tool_calls=[call]), reply("One order."), reply("Still one.")])
    async with make(web) as c:
        await login(c)
        first = await c.post("/api/chat", json={"message": "how many orders?"}, headers=H)
        assert first.json() == {"answer": "One order.", "tools": ["list_orders"]}
        second = await c.post("/api/chat", json={"message": "and now?"}, headers=H)
        assert second.json()["answer"] == "Still one."
        sent = json.loads(llm.calls[2].request.content)["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]  # turn 1 was remembered
    async with make(web) as other:  # a different session starts fresh
        await login(other)
        llm.mock(side_effect=[reply("fresh")])
        await other.post("/api/chat", json={"message": "hello"}, headers=H)
        assert [m["role"] for m in json.loads(llm.calls.last.request.content)["messages"]] == ["system", "user"]


async def test_chat_validation_limits_and_upstream_errors(web, router, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router.post(OR_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "no"}}))
    async with make(web) as c:
        await login(c)
        assert (await c.post("/api/chat", json={"message": "   "}, headers=H)).status_code == 400
        assert (await c.post("/api/chat", json={"message": "x" * 2001}, headers=H)).status_code == 413
        assert (await c.post("/api/chat", content=b"{bad", headers={**H, "Content-Type": "application/json"})).status_code == 400
        r = await c.post("/api/chat", json={"message": "hi"}, headers=H)
        assert r.status_code == 502 and "OPENROUTER_API_KEY" in r.json()["error"]


async def test_chat_is_rate_limited_per_session(web, router, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router.post(OR_URL).mock(return_value=reply("ok"))
    async with make(web) as c:
        await login(c)
        codes = [(await c.post("/api/chat", json={"message": "hi"}, headers=H)).status_code for _ in range(webmod.CHAT_MAX_PER_MINUTE + 1)]
        assert codes[:-1] == [200] * webmod.CHAT_MAX_PER_MINUTE and codes[-1] == 429


def test_history_trimming_never_splits_a_turn():
    history = []
    for i in range(40):
        history += [
            {"role": "user", "content": str(i)},
            {"role": "assistant", "content": None, "tool_calls": [1]},
            {"role": "tool", "content": "x"},
            {"role": "assistant", "content": "a"},
        ]
    trimmed = WebApp._trim(history)
    assert len(trimmed) <= webmod.MAX_HISTORY_MESSAGES and trimmed[0]["role"] == "user"


def test_a_password_is_mandatory():
    with pytest.raises(ValueError):
        WebApp("")


def test_unused_imports_keep_linters_quiet():
    assert STORE and common
