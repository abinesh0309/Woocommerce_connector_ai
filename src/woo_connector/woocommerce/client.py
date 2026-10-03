"""The one HTTP client: throttled, bounded, retrying, GET-only, never follows redirects."""

import asyncio
import logging
import math
import os
import random
import time
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from woo_connector import __version__
from woo_connector.auth.credentials import Credentials
from woo_connector.woocommerce import oauth1
from woo_connector.woocommerce.errors import (
    AuthError,
    BadRequest,
    NotFound,
    NotJson,
    RateLimited,
    RedirectRefused,
    UpstreamError,
)
from woo_connector.woocommerce.ratelimit import TokenBucket, parse_retry_after

# OAuth1 request URLs carry the consumer key and signature in the query string, and httpx logs full URLs at INFO.
# Silence it here (not only in the CLI) so no entry point can leak them.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
PAUSE_STATUSES = frozenset({429, 503})

Query = dict[str, Any]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class ClientSettings:
    max_rps: float = 4.0
    burst: int = 8
    max_concurrency: int = 4
    max_attempts: int = 5  # network errors and 502/504
    max_throttle_attempts: int = 12  # 429/503: each is preceded by an honoured pause; the time budget is the real bound
    max_wait_s: float = 30.0  # refuse to wait longer than this for one Retry-After
    budget_s: float = 60.0  # total time a single request may take, retries included
    timeout_s: float = 20.0
    cache_ttl_s: float = 0.0
    cache_entries: int = 256
    verify_tls: bool = True

    @classmethod
    def from_env(cls) -> "ClientSettings":
        return cls(
            max_rps=_env_float("WOO_MAX_RPS", 4.0),
            burst=int(_env_float("WOO_BURST", 8)),
            cache_ttl_s=_env_float("WOO_CACHE_TTL", 0.0),
            verify_tls=os.environ.get("WOO_VERIFY_TLS", "true").lower() not in ("0", "false", "no"),
        )


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    cache_hits: int = 0
    throttle_wait_s: float = 0.0
    by_status: Counter = field(default_factory=Counter)


@dataclass
class Resp:
    data: Any
    headers: httpx.Headers
    status: int


@dataclass
class Page:
    items: list[Any]
    page: int
    per_page: int
    total: int
    total_pages: int

    @property
    def has_more(self) -> bool:
        return self.page < self.total_pages

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.has_more else None


def _clean_query(params: Query | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        out.append((key, str(value)))
    return out


class WooClient:
    def __init__(
        self,
        creds: Credentials,
        settings: ClientSettings | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] | None = None,
    ):
        self.creds = creds
        self.settings = settings or ClientSettings()
        self.stats = Stats()
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(0.5, 1.0))
        self._bucket = TokenBucket(self.settings.max_rps, self.settings.burst, clock=clock, sleep=sleep)
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        self._use_basic = creds.store_url.startswith("https://")
        self._cache: OrderedDict[tuple, tuple[float, Resp]] = OrderedDict()
        self._http = http or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.settings.timeout_s, connect=10.0),
            verify=self.settings.verify_tls,
            headers={"Accept": "application/json", "User-Agent": f"woo-connector/{__version__}"},
        )

    # ------------------------------------------------------------------ public API

    @property
    def store_url(self) -> str:
        return self.creds.store_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: Query | None = None) -> Resp:
        """GET ``{store}/wp-json/{path}``. The only HTTP verb this class can send."""
        query = _clean_query(params)
        cache_key = (path, tuple(sorted(query)))
        ttl = self.settings.cache_ttl_s
        if ttl > 0:
            hit = self._cache.get(cache_key)
            if hit and hit[0] > self._clock():
                self._cache.move_to_end(cache_key)
                self.stats.cache_hits += 1
                return hit[1]

        resp = await self._request(path, query)

        if ttl > 0:  # only successful responses reach this line
            self._cache[cache_key] = (self._clock() + ttl, resp)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.settings.cache_entries:
                self._cache.popitem(last=False)
        return resp

    async def get_json(self, path: str, params: Query | None = None) -> Any:
        return (await self.get(path, params)).data

    async def get_page(self, path: str, params: Query | None = None, *, page: int = 1, per_page: int = 20) -> Page:
        resp = await self.get(path, {**(params or {}), "page": page, "per_page": per_page})
        items = resp.data if isinstance(resp.data, list) else []
        total = _int_header(resp.headers, "X-WP-Total", len(items))
        total_pages = _int_header(resp.headers, "X-WP-TotalPages", 1 if items else 0)
        return Page(items, page, per_page, total, total_pages)

    async def iter_pages(self, path: str, params: Query | None = None, *, per_page: int = 100, max_pages: int = 5) -> AsyncIterator[Page]:
        page = 1
        while page <= max_pages:
            result = await self.get_page(path, params, page=page, per_page=per_page)
            yield result
            if not result.has_more:
                return
            page += 1

    async def get_all(self, path: str, params: Query | None = None, *, per_page: int = 100, max_pages: int = 5) -> tuple[list[Any], int, bool]:
        """Return ``(items, total_available, truncated)``; never scans more than ``max_pages`` pages."""
        items: list[Any] = []
        total = 0
        truncated = False
        async for result in self.iter_pages(path, params, per_page=per_page, max_pages=max_pages):
            items.extend(result.items)
            total = result.total
            truncated = result.has_more
        return items, total, truncated

    # ------------------------------------------------------------------ internals

    def _build_url(self, path: str, query: list[tuple[str, str]]) -> tuple[str, httpx.BasicAuth | None]:
        base = f"{self.creds.store_url}/wp-json/{path.lstrip('/')}"
        if self._use_basic:
            qs = "&".join(f"{oauth1.rfc3986(k)}={oauth1.rfc3986(v)}" for k, v in query)
            return (base + ("?" + qs if qs else "")), httpx.BasicAuth(self.creds.consumer_key, self.creds.consumer_secret)
        # http:// store: fresh OAuth 1.0a signature (new nonce + timestamp) for every attempt.
        return oauth1.sign_url("GET", base, query, self.creds.consumer_key, self.creds.consumer_secret), None

    def _backoff(self, attempt: int) -> float:
        return min(20.0, 0.5 * 2 ** (attempt - 1)) * self._jitter()

    async def _request(self, path: str, query: list[tuple[str, str]]) -> Resp:
        deadline = self._clock() + self.settings.budget_s
        attempt = 0  # network / 5xx attempts
        throttled = 0  # 429 / 503 responses
        while True:
            attempt += 1
            before = self._bucket.waited_s
            await self._bucket.acquire()
            self.stats.throttle_wait_s += self._bucket.waited_s - before
            if self._clock() > deadline:
                raise RateLimited("The request exceeded its time budget while waiting for the store's rate limit. Tell the user and try again later.")

            url, auth = self._build_url(path, query)
            try:
                async with self._sem:
                    self.stats.requests += 1
                    response = await self._http.get(url, auth=auth) if auth else await self._http.get(url)
            except httpx.TransportError as exc:
                self.stats.by_status["network_error"] += 1
                if attempt >= self.settings.max_attempts:
                    raise UpstreamError(
                        f"Could not reach the store at {self.creds.store_url} ({type(exc).__name__}). Check the URL and that the site is up, then retry."
                    ) from exc
                delay = self._backoff(attempt)
                self._check_budget(delay, deadline, "network errors")
                self.stats.retries += 1
                await self._sleep(delay)
                continue

            status = response.status_code
            self.stats.by_status[status] += 1

            if status < 300:
                self._bucket.note_success()
                return self._ok(response)
            if 300 <= status < 400:
                raise RedirectRefused(
                    f"The store redirected the API request (HTTP {status}) to {response.headers.get('location', 'another URL')}. "
                    "Credentials are never sent to a redirected address. The merchant should reconnect using the store's canonical URL "
                    "(`woo-connector auth --store <canonical URL>`), e.g. with/without www or https instead of http."
                )
            if status not in RETRY_STATUSES:
                raise self._error_for(status, response)

            # --- retryable: 429 / 502 / 503 / 504
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if status in PAUSE_STATUSES:
                if status == 429:
                    self.stats.rate_limited += 1
                throttled += 1
                attempt -= 1  # throttling does not use up the (smaller) network/5xx attempt allowance
                if throttled >= self.settings.max_throttle_attempts:
                    raise self._give_up(status, retry_after, self.settings.max_throttle_attempts)
                wait = retry_after if retry_after is not None else self._backoff(throttled)
                if wait > self.settings.max_wait_s:
                    raise RateLimited(f"The store is rate limiting requests and asked to wait {int(math.ceil(wait))}s. Tell the user and try again later.")
                self._check_budget(wait, deadline, "rate limiting")
                self._bucket.pause(wait)  # every concurrent caller waits...
                self._bucket.slow_down()  # ...then resumes at a lower rate, learning the host's real limit
                self.stats.retries += 1
                continue  # acquire() blocks until the shared pause ends

            if attempt >= self.settings.max_attempts:
                raise self._give_up(status, retry_after, self.settings.max_attempts)
            delay = max(retry_after or 0.0, self._backoff(attempt))
            if delay > self.settings.max_wait_s:
                raise self._give_up(status, retry_after, attempt)
            self._check_budget(delay, deadline, "upstream errors")
            self.stats.retries += 1
            await self._sleep(delay)

    def _check_budget(self, delay: float, deadline: float, what: str) -> None:
        if self._clock() + delay > deadline:
            raise RateLimited(
                f"Giving up: retrying after {what} would exceed the {int(self.settings.budget_s)}s time budget. Tell the user and try again later."
            )

    def _give_up(self, status: int, retry_after: float | None, attempts: int) -> Exception:
        if status in PAUSE_STATUSES:
            hint = f" (asked to wait {int(math.ceil(retry_after))}s)" if retry_after else ""
            return RateLimited(f"The store kept rate limiting requests{hint} after {attempts} attempts. Tell the user and try again later.")
        return UpstreamError(f"The store returned HTTP {status} {attempts} times in a row. It may be overloaded or down; try again later.")

    def _ok(self, response: httpx.Response) -> Resp:
        ctype = response.headers.get("content-type", "")
        try:
            data = response.json()
        except ValueError:
            data = None
        if data is None and (not response.content or "json" not in ctype):
            raise NotJson(
                "The store answered with something that is not JSON (often a firewall/WAF challenge page, a maintenance page, or the wrong URL). "
                "Check the store URL and that /wp-json/ is reachable without a browser check."
            )
        return Resp(data, response.headers, response.status_code)

    def _error_for(self, status: int, response: httpx.Response) -> Exception:
        code, message = "", ""
        try:
            body = response.json()
            if isinstance(body, dict):
                code = str(body.get("code", ""))
                message = str(body.get("message", ""))
        except ValueError:
            if "json" not in response.headers.get("content-type", ""):
                return NotJson(
                    f"The store answered HTTP {status} with a non-JSON page (a firewall/WAF or maintenance page?). Check the store URL and firewall rules."
                )
        low = message.lower()
        if status in (401, 403):
            if "timestamp" in low or "nonce" in low:
                return AuthError(
                    f"The store rejected the request signature ({message.strip() or 'timestamp/nonce'}). This usually means the clock on this machine or on the store "  # noqa: E501
                    "is wrong (more than 15 minutes off). Fix the system time; reconnecting will not help."
                )
            # NB: WooCommerce answers an invalid/revoked key with 401 + 'woocommerce_rest_cannot_view', so only 403 means 'no permission'.
            if status == 403:
                return AuthError(
                    f"The store refused access ({message.strip() or 'forbidden'}). The API key's owner may lack permission (use an Administrator or Shop manager) "  # noqa: E501
                    "or a firewall is blocking the request."
                )
            return AuthError(
                f"The store rejected the API key ({message.strip() or 'unauthorized'}). It was probably revoked or mistyped. "
                "The merchant needs to reconnect the store (`woo-connector auth`). If the site is on Apache with CGI/FastCGI, the host may be stripping the Authorization header."  # noqa: E501
            )
        if status == 404:
            return NotFound(message or "Not found.", code)
        if status >= 500:
            return UpstreamError(
                f"The store failed with HTTP {status} ({message.strip() or 'server error'}). This is a problem on the store's side (PHP error, plugin conflict?). Try again later."  # noqa: E501
            )
        return BadRequest(f"The store rejected the request (HTTP {status}{', ' + code if code else ''}): {message.strip() or 'no details'}")


def _int_header(headers: httpx.Headers, name: str, default: int) -> int:
    try:
        return int(headers.get(name, default))
    except (TypeError, ValueError):
        return default
