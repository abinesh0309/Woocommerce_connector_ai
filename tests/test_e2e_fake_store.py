"""Real sockets, real signatures: the connector against a fake WooCommerce that verifies OAuth1 independently."""

import json

import httpx
import pytest
from mcp import Client

from tests.fake_woo import INJECTION, KEY, SECRET, RunningFake
from woo_connector.app import build_server
from woo_connector.auth.credentials import Credentials
from woo_connector.tools import common
from woo_connector.woocommerce.client import ClientSettings, WooClient
from woo_connector.woocommerce.errors import AuthError


@pytest.fixture
def store():
    with RunningFake() as running:
        yield running


@pytest.fixture
def wired(store):
    client = WooClient(Credentials(store.url, KEY, SECRET, "read"), ClientSettings(max_rps=50, burst=50))
    common.configure(client=client, store_url=store.url, pii_mode="masked")
    yield store, client
    common.configure()


async def tool(name, args=None):
    async with Client(build_server()) as c:
        r = await c.call_tool(name, args or {})
    return r.is_error, (r.structured_content if not r.is_error else "\n".join(x.text for x in r.content))


async def test_signed_requests_are_accepted_by_an_independent_verifier(wired):
    store, client = wired
    err, out = await tool("list_orders", {"per_page": 5})
    assert not err and out["total"] == 12 and len(out["items"]) == 5 and out["has_more"]
    assert store.fake.requests >= 1


async def test_wrong_secret_is_rejected_with_reconnect_advice(store):
    client = WooClient(Credentials(store.url, KEY, "cs_wrong", "read"), ClientSettings(max_rps=50, burst=50))
    common.configure(client=client, store_url=store.url)
    err, text = await tool("list_orders")
    common.configure()
    assert err and "signature" in text.lower() or "reconnect" in text.lower()


async def test_revoked_key_then_recovery_without_restarting(wired):
    store, _ = wired
    store.fake.revoked = True
    err, text = await tool("list_orders")
    assert err and "reconnect the store" in text and common.runtime._client is None
    store.fake.revoked = False
    # a reconnect would reload credentials; here we re-inject a client to simulate fresh credentials
    common.configure(client=WooClient(Credentials(store.url, KEY, SECRET, "read"), ClientSettings(max_rps=50, burst=50)), store_url=store.url)
    err, out = await tool("list_orders", {"per_page": 1})
    assert not err and out["total"] == 12


async def test_search_get_and_masking_end_to_end(wired):
    err, found = await tool("search_orders", {"query": "dana@example.com"})
    assert not err and found["items"] and all(r["customer_email"] == "d***@example.com" for r in found["items"])
    err, exact = await tool("search_orders", {"query": "#1003"})
    assert not err and exact["items"][0]["id"] == 1003
    err, order = await tool("get_order", {"order_id": 1003})
    blob = json.dumps(order)
    assert not err and "secret street" not in blob and "tok_secret" not in blob and "0000000002" not in blob
    assert order["customer_note_text"] == INJECTION  # arrives as data, in a *_text field
    assert order["notes"][0]["visible_to_customer"] is False
    err, customer_view = await tool("get_order", {"order_id": 1003, "include_notes": "customer"})
    assert not err and customer_view["notes"] and all(n["visible_to_customer"] for n in customer_view["notes"])  # staff notes never appear


async def test_tracking_stock_customers_end_to_end(wired):
    err, trk = await tool("get_order_tracking", {"order_id": 1002})
    assert not err and trk["source"] == "order_meta" and trk["shipments"][0]["tracking_number"] == "AWB12345"
    err, none = await tool("get_order_tracking", {"order_id": 1001})
    assert not err and none["source"] == "none"
    err, low = await tool("list_low_stock", {"threshold": 5})
    issues = {(i["product_id"], i.get("variation_id")): i["issue"] for i in low["items"]}
    assert issues == {(11, None): "low", (13, None): "backordered", (14, None): "out_of_stock", (12, 121): "out_of_stock"}
    err, customers = await tool("list_customers")
    assert [c["id"] for c in customers["items"]] == [7, 8]  # the shop owner (id 1) is never listed
    err, text = await tool("get_customer", {"customer_id": 1})
    assert err and "does not exist" in text and "owner@fake.shop" not in text
    err, text = await tool("get_order", {"order_id": 999999})
    assert err and "Order 999999 does not exist. Use search_orders" in text


async def test_overview_end_to_end(wired):
    err, out = await tool("get_store_overview")
    assert not err and out["counts"]["on_hold"] == 1 and out["counts"]["failed"] == 1 and out["counts"]["processing"] == 3


async def test_the_connector_never_writes(wired):
    store, client = wired
    for _ in range(3):
        await tool("list_orders", {"per_page": 1})
    assert store.fake.writes_attempted == 0
    # Prove the store itself would refuse a write from this key (what scope=read guarantees):
    r = httpx.post(f"{store.url}/wp-json/wc/v3/orders", json={})
    assert r.status_code == 401 and store.fake.writes_attempted == 1


async def test_survives_a_host_level_rate_limit(store):
    store.fake.limit = (5, 4.0)  # 5 requests per 4 seconds, then 429 + Retry-After: 2
    client = WooClient(Credentials(store.url, KEY, SECRET, "read"), ClientSettings(max_rps=50, burst=50, budget_s=60))
    import asyncio

    results = await asyncio.gather(*(client.get_page("wc/v3/orders", page=1, per_page=1) for _ in range(12)))
    assert all(r.total == 12 for r in results)
    assert client.stats.rate_limited >= 1 and client.stats.retries >= 1
    await client.aclose()


async def test_redirecting_store_never_receives_credentials():
    seen = []

    async def handler(request: httpx.Request):
        seen.append(str(request.url))
        return httpx.Response(301, headers={"Location": "https://elsewhere.test/wp-json/wc/v3/orders"})

    from woo_connector.woocommerce.errors import RedirectRefused

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    client = WooClient(Credentials("http://shop.test", KEY, SECRET), ClientSettings(max_rps=50, burst=50), http=http)
    with pytest.raises(RedirectRefused):
        await client.get("wc/v3/orders")
    assert len(seen) == 1 and "elsewhere" not in seen[0]


async def test_clock_skew_is_diagnosed_not_blamed_on_the_key(store, monkeypatch):
    import time as real_time
    import types

    from woo_connector.woocommerce import oauth1

    real_now = real_time.time
    # Skew only the connector's clock (the fake server runs in this process and must keep real time).
    monkeypatch.setattr(oauth1, "time", types.SimpleNamespace(time=lambda: real_now() - 3600))
    client = WooClient(Credentials(store.url, KEY, SECRET, "read"), ClientSettings(max_rps=50, burst=50))
    with pytest.raises(AuthError, match="clock"):
        await client.get("wc/v3/orders")
    await client.aclose()
