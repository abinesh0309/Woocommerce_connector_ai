"""Shared plumbing for tools: lazy client, error translation, pagination types, date parsing."""

import asyncio
import functools
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from woo_connector.auth import credentials
from woo_connector.auth.credentials import NotConnected
from woo_connector.woocommerce.client import ClientSettings, Page, WooClient
from woo_connector.woocommerce.errors import AuthError, NotFound, WooError

PageNo = Annotated[int, Field(ge=1, description="Page number, starting at 1. Use `next_page` from the previous result.")]
PerPage = Annotated[int, Field(ge=1, le=100, description="Items per page (1-100). Prefer filters over big pages.")]

OrderStatus = Literal["any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed", "checkout-draft"]


class Runtime:
    """Process-wide state. Tests call :func:`configure` to inject a client."""

    def __init__(self) -> None:
        self.store_url: str | None = None
        self._client: WooClient | None = None
        self._pii_override: str | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> WooClient:
        async with self._lock:
            if self._client is None:
                creds = credentials.load(self.store_url)  # raises NotConnected with next steps
                self._client = WooClient(creds, ClientSettings.from_env())
            return self._client

    async def reset(self) -> None:
        """Drop the cached client so the next call reloads credentials (e.g. after a reconnect)."""
        async with self._lock:
            old, self._client = self._client, None
        if old is not None:
            await old.aclose()

    @property
    def pii(self) -> str:
        mode = (self._pii_override or os.environ.get("WOO_PII_MODE", "masked")).lower()
        return mode if mode in ("masked", "full") else "masked"

    @property
    def key_permissions(self) -> str:
        return self._client.creds.key_permissions if self._client else "unknown"


runtime = Runtime()


def configure(*, client: WooClient | None = None, store_url: str | None = None, pii_mode: str | None = None) -> None:
    runtime._client = client
    runtime.store_url = store_url
    runtime._pii_override = pii_mode


def guarded[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Translate internal errors into messages an agent can act on (never stack traces)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except NotConnected as exc:
            raise ToolError(str(exc)) from None
        except AuthError as exc:
            await runtime.reset()
            raise ToolError(str(exc)) from None
        except NotFound as exc:
            if exc.route_missing:
                raise ToolError(
                    "The store does not expose this WooCommerce endpoint. Check that WooCommerce is active, permalinks are set to anything other than 'Plain', "
                    "and the store URL is correct."
                ) from None
            raise ToolError(str(exc) or "Not found.") from None
        except WooError as exc:
            raise ToolError(str(exc)) from None

    return wrapper  # type: ignore[return-value]


def not_found(kind: str, ident: object, hint: str) -> NoReturn:
    raise ToolError(f"{kind} {ident} does not exist. {hint}")


async def gather_strict(*aws: Awaitable[Any]) -> list[Any]:
    """Run concurrently; if any fail, raise the first failure in argument order (no orphaned task errors)."""
    results = await asyncio.gather(*aws, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return list(results)


def parse_when(value: str | None, name: str) -> str | None:
    """ISO date or datetime -> ``YYYY-MM-DDTHH:MM:SS`` in UTC. No offset means UTC; a bare date means 00:00 UTC."""
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ToolError(f"{name} must be an ISO date like 2026-09-01 or datetime like 2026-09-01T10:30:00Z (no UTC offset means UTC). Got '{value}'.") from None
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def page_out(page: Page, items: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "items": items,
        "page": page.page,
        "per_page": page.per_page,
        "total": page.total,
        "total_pages": page.total_pages,
        "has_more": page.has_more,
    }
    if page.next_page is not None:
        out["next_page"] = page.next_page
    return out
