import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from tests.conftest import API, STORE, FakeClock, make_client, page_response
from woo_connector.woocommerce.errors import AuthError, BadRequest, NotFound, NotJson, RateLimited, RedirectRefused, UpstreamError
from woo_connector.woocommerce.ratelimit import TokenBucket, parse_retry_after

ORDERS = f"{API}/wc/v3/orders"


# ---------------------------------------------------------------- token bucket


async def test_bucket_allows_burst_then_waits_for_refill(clock):
    b = TokenBucket(rate=2, burst=3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await b.acquire()
    assert clock.sleeps == []
    await b.acquire()
    assert clock.sleeps == [pytest.approx(0.5)]


async def test_bucket_refills_over_time_up_to_burst(clock):
    b = TokenBucket(rate=1, burst=2, clock=clock, sleep=clock.sleep)
    await b.acquire()
    await b.acquire()
    clock.t += 100
    await b.acquire()
    await b.acquire()
    assert clock.sleeps == []  # burst is capped at 2 but both were available again
    await b.acquire()
    assert len(clock.sleeps) == 1


async def test_pause_blocks_every_caller_then_trickles_back(clock):
    b = TokenBucket(rate=1, burst=5, clock=clock, sleep=clock.sleep)
    b.pause(10)
    start = clock.t
    times: list[float] = []

    async def worker():
        await b.acquire()
        times.append(clock.t)

    await asyncio.gather(*(worker() for _ in range(3)))
    assert min(times) >= start + 10  # nobody got through during the pause
    assert max(times) - min(times) >= 1.9  # then they trickle at 1 req/s instead of bursting


def test_pause_never_shortens(clock):
    b = TokenBucket(rate=1, burst=1, clock=clock, sleep=clock.sleep)
    b.pause(10)
    b.pause(2)
    assert b.paused_for == pytest.approx(10)


def test_retry_after_seconds_date_and_garbage():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("  2.5 ") == 2.5
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now=lambda: now) == pytest.approx(30)
    assert parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now=lambda: now) == 0.0
    for junk in (None, "", "soon", "-", "nan", "inf"):
        assert parse_retry_after(junk) is None
    assert parse_retry_after("-5") == 0.0


# ---------------------------------------------------------------- client: success paths


async def test_basic_auth_over_https_and_no_key_in_query(client, router):
    route = router.get(ORDERS).mock(return_value=page_response([{"id": 1}]))
    await client.get("wc/v3/orders", {"per_page": 1, "status": None, "dates_are_gmt": True})
    req = route.calls.last.request
    assert req.headers["authorization"].startswith("Basic ")
    q = dict(parse_qsl(urlsplit(str(req.url)).query))
    assert q == {"per_page": "1", "dates_are_gmt": "true"}
    assert "ck_test" not in str(req.url)


async def test_oauth1_over_http_signs_in_query_with_fresh_nonce_per_attempt(clock, router):
    c = make_client(clock, store="http://shop.test")
    route = router.get("http://shop.test/wp-json/wc/v3/orders").mock(side_effect=[httpx.Response(503, headers={"Retry-After": "1"}), page_response([])])
    await c.get("wc/v3/orders", {"per_page": 1})
    first, second = (dict(parse_qsl(urlsplit(str(call.request.url)).query)) for call in route.calls)
    assert "authorization" not in route.calls[0].request.headers
    assert first["oauth_consumer_key"] == "ck_test" and first["oauth_signature_method"] == "HMAC-SHA256"
    assert first["oauth_nonce"] != second["oauth_nonce"]
    assert first["oauth_signature"] != second["oauth_signature"]


async def test_get_page_reads_total_headers_and_has_more(client, router):
    router.get(ORDERS).mock(return_value=page_response([{"id": 1}, {"id": 2}], total=5, pages=3))
    page = await client.get_page("wc/v3/orders", page=1, per_page=2)
    assert (page.total, page.total_pages, page.has_more, page.next_page) == (5, 3, True, 2)


async def test_get_all_is_bounded_and_reports_truncation(client, router):
    router.get(ORDERS).mock(return_value=page_response([{"id": 1}], total=99, pages=99))
    items, total, truncated = await client.get_all("wc/v3/orders", max_pages=3)
    assert len(items) == 3 and total == 99 and truncated
    assert client.stats.requests == 3


# ---------------------------------------------------------------- client: retries and rate limiting


async def test_429_with_retry_after_is_honoured_and_succeeds(client, router, clock):
    router.get(ORDERS).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), page_response([{"id": 1}])])
    resp = await client.get("wc/v3/orders")
    assert resp.data == [{"id": 1}]
    assert sum(clock.sleeps) >= 3
    assert client.stats.rate_limited == 1 and client.stats.retries == 1


async def test_429_pauses_other_concurrent_callers(clock, router):
    c = make_client(clock)
    calls: list[float] = []

    def handler(request):
        calls.append(clock.t)
        return httpx.Response(429, headers={"Retry-After": "5"}) if len(calls) == 1 else page_response([])

    router.get(ORDERS).mock(side_effect=handler)
    await asyncio.gather(*(c.get("wc/v3/orders", {"n": i}) for i in range(4)))
    first = calls[0]
    assert len(calls) == 5  # 4 callers + 1 retry
    # Nobody else got a request out while the store said "wait 5 seconds".
    assert [t for t in calls if first < t < first + 5] == []


async def test_retry_after_longer_than_limit_fails_fast_with_clear_message(client, router, clock):
    router.get(ORDERS).mock(return_value=httpx.Response(429, headers={"Retry-After": "900"}))
    with pytest.raises(RateLimited, match="900s"):
        await client.get("wc/v3/orders")
    assert clock.sleeps == []


async def test_time_budget_is_enforced(clock, router):
    c = make_client(clock, budget_s=10)
    router.get(ORDERS).mock(return_value=httpx.Response(429, headers={"Retry-After": "20"}))
    with pytest.raises(RateLimited):
        await c.get("wc/v3/orders")


async def test_budget_exceeded_across_several_retries(clock, router):
    c = make_client(clock, budget_s=12, max_attempts=10)
    router.get(ORDERS).mock(return_value=httpx.Response(503, headers={"Retry-After": "5"}))
    with pytest.raises(RateLimited, match="time budget"):
        await c.get("wc/v3/orders")
    assert clock.t - 1000 <= 12


async def test_gives_up_after_max_attempts(client, router):
    route = router.get(ORDERS).mock(return_value=httpx.Response(503, headers={"Retry-After": "1"}))
    with pytest.raises(RateLimited, match="12 attempts"):
        await client.get("wc/v3/orders")
    assert route.call_count == 12


async def test_429_slows_the_shared_rate_once_and_success_recovers_it(clock, router):
    c = make_client(clock)
    base = c._bucket.base_rate
    router.get(ORDERS).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(429, headers={"Retry-After": "1"}), *[page_response([])] * 60]
    )
    await c.get("wc/v3/orders")
    # two spaced 429s: 4 -> 2 -> 1 req/s, then one success nudges it up by 5%
    assert c._bucket.rate == pytest.approx(base * 0.25 * 1.05)
    slowed = c._bucket.rate
    for _ in range(40):
        await c.get("wc/v3/orders")
    assert slowed < c._bucket.rate <= base


def test_slowdown_is_applied_once_per_second_and_has_a_floor(clock):
    b = TokenBucket(rate=4, burst=4, clock=clock, sleep=clock.sleep)
    b.slow_down()
    b.slow_down()
    b.slow_down()
    assert b.rate == 2
    for _ in range(10):
        clock.t += 2
        b.slow_down()
    assert b.rate == b.min_rate


async def test_502_is_retried_with_backoff(client, router, clock):
    route = router.get(ORDERS).mock(side_effect=[httpx.Response(502), httpx.Response(504), page_response([])])
    await client.get("wc/v3/orders")
    assert route.call_count == 3
    assert clock.sleeps == [0.5, 1.0]


async def test_network_error_is_retried_then_reported(client, router):
    route = router.get(ORDERS).mock(side_effect=[httpx.ConnectError("boom"), page_response([])])
    assert (await client.get("wc/v3/orders")).data == []
    assert route.call_count == 2
    router.get(ORDERS).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(UpstreamError, match="Could not reach the store"):
        await client.get("wc/v3/orders")


@pytest.mark.parametrize(("status", "exc"), [(401, AuthError), (403, AuthError), (404, NotFound), (400, BadRequest), (500, UpstreamError)])
async def test_non_retryable_statuses_are_not_retried(client, router, status, exc):
    route = router.get(ORDERS).mock(return_value=httpx.Response(status, json={"code": "x", "message": "nope"}))
    with pytest.raises(exc):
        await client.get("wc/v3/orders")
    assert route.call_count == 1


async def test_redirect_is_refused_and_credentials_do_not_follow(client, router):
    route = router.get(ORDERS).mock(return_value=httpx.Response(301, headers={"Location": "https://evil.test/wp-json/wc/v3/orders"}))
    evil = router.get("https://evil.test/wp-json/wc/v3/orders").mock(return_value=page_response([]))
    with pytest.raises(RedirectRefused, match="canonical URL"):
        await client.get("wc/v3/orders")
    assert route.call_count == 1 and evil.call_count == 0


async def test_non_json_body_is_reported_helpfully(client, router):
    router.get(ORDERS).mock(return_value=httpx.Response(200, text="<html>Checking your browser...</html>", headers={"content-type": "text/html"}))
    with pytest.raises(NotJson, match="store URL"):
        await client.get("wc/v3/orders")


async def test_forbidden_403_is_a_permission_message_not_reconnect(client, router):
    router.get(ORDERS).mock(return_value=httpx.Response(403, json={"code": "woocommerce_rest_cannot_view", "message": "Sorry, you cannot list resources."}))
    with pytest.raises(AuthError, match="refused access"):
        await client.get("wc/v3/orders")


async def test_revoked_key_message_says_reconnect(client, router):
    router.get(ORDERS).mock(return_value=httpx.Response(401, json={"code": "woocommerce_rest_cannot_view", "message": "Consumer secret is invalid."}))
    with pytest.raises(AuthError, match="reconnect the store"):
        await client.get("wc/v3/orders")


@pytest.mark.parametrize("message", ["Invalid timestamp.", "Invalid nonce - nonce has already been used."])
async def test_clock_skew_gets_its_own_message(clock, router, message):
    c = make_client(clock, store="http://shop.test")
    router.get("http://shop.test/wp-json/wc/v3/orders").mock(
        return_value=httpx.Response(401, json={"code": "woocommerce_rest_authentication_error", "message": message})
    )
    with pytest.raises(AuthError, match="clock") as info:
        await c.get("wc/v3/orders")
    assert "reconnecting will not help" in str(info.value)


async def test_missing_route_is_flagged(client, router):
    router.get(ORDERS).mock(return_value=httpx.Response(404, json={"code": "rest_no_route", "message": "No route"}))
    with pytest.raises(NotFound) as info:
        await client.get("wc/v3/orders")
    assert info.value.route_missing


# ---------------------------------------------------------------- cache


async def test_cache_hit_and_miss_and_errors_not_cached(clock, router):
    c = make_client(clock, cache_ttl_s=30)
    route = router.get(ORDERS).mock(side_effect=[httpx.Response(500, json={"message": "fatal"}), page_response([{"id": 1}]), page_response([{"id": 2}])])
    with pytest.raises(UpstreamError):
        await c.get("wc/v3/orders", {"a": 1})
    first = await c.get("wc/v3/orders", {"a": 1})  # error was not cached -> real request
    again = await c.get("wc/v3/orders", {"a": 1})  # served from cache
    other = await c.get("wc/v3/orders", {"a": 2})  # different query -> miss
    assert first.data == again.data == [{"id": 1}] and other.data == [{"id": 2}]
    assert route.call_count == 3 and c.stats.cache_hits == 1
    clock.t += 31
    router.get(ORDERS).mock(return_value=page_response([{"id": 3}]))
    assert (await c.get("wc/v3/orders", {"a": 1})).data == [{"id": 3}]  # expired


async def test_cache_is_off_by_default(client, router):
    route = router.get(ORDERS).mock(return_value=page_response([]))
    await client.get("wc/v3/orders")
    await client.get("wc/v3/orders")
    assert route.call_count == 2


async def test_only_get_is_ever_sent(client, router):
    route = router.route().mock(return_value=page_response([]))
    await client.get("wc/v3/orders")
    assert {c.request.method for c in route.calls} == {"GET"}
    assert not hasattr(client, "post") and not hasattr(client, "put") and not hasattr(client, "delete")


def test_credentials_repr_hides_secrets():
    from woo_connector.auth.credentials import Credentials

    text = repr(Credentials(STORE, "ck_real", "cs_real", "read"))
    assert "ck_real" not in text and "cs_real" not in text


def test_fake_clock_fixture_is_monotonic():
    c = FakeClock()
    assert c() == 1000.0
