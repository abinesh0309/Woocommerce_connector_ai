"""Browser UI: a dashboard and an AI chat for ONE store, behind a password.

It reuses the exact same read-only tools as the MCP server (called through an in-process MCP client, so every
input is validated by the same schemas). Nothing here can change the store.
"""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import Any

from mcp import Client
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from woo_connector import __version__
from woo_connector.agent import DEFAULT_MODEL, AgentError, ask
from woo_connector.app import build_server

STATIC = Path(__file__).parent / "static"
COOKIE = "woo_session"
SESSION_TTL_S = 12 * 3600
LOGIN_MAX_FAILURES, LOGIN_WINDOW_S = 5, 300
CHAT_MAX_PER_MINUTE = 20
CHAT_MAX_CHARS = 2000
MAX_HISTORY_MESSAGES = 60
MAX_SESSIONS = 200
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "woo-web"

CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"  # noqa: E501
_PREFIX = re.compile(r"^Error executing tool \w+: ")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class WebApp:
    def __init__(self, password: str, secret: str | None = None, *, cookie_secure: bool | None = None):
        if not password:
            raise ValueError("A password is required for the web UI.")
        self._password = _digest(password)
        self._secret = (secret or secrets.token_hex(32)).encode()
        self._cookie_secure = cookie_secure
        self._server = build_server()
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._chat_hits: dict[str, deque[float]] = defaultdict(deque)
        self._histories: OrderedDict[str, list[dict]] = OrderedDict()
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._revoked: set[str] = set()
        self._static = {name: (STATIC / name).read_bytes() for name in ("index.html", "login.html", "app.js", "login.js", "styles.css")}
        self.app = Starlette(
            routes=[
                Route("/", self.index),
                Route("/login", self.login_page),
                Route("/healthz", self.healthz),
                Route("/static/{name}", self.static),
                Route("/api/login", self.login, methods=["POST"]),
                Route("/api/logout", self.logout, methods=["POST"]),
                Route("/api/config", self.api_config),
                Route("/api/info", self.api_info),
                Route("/api/overview", self.api_overview),
                Route("/api/orders", self.api_orders),
                Route("/api/orders/{order_id:int}", self.api_order),
                Route("/api/orders/{order_id:int}/tracking", self.api_tracking),
                Route("/api/products", self.api_products),
                Route("/api/products/{product_id:int}", self.api_product),
                Route("/api/low-stock", self.api_low_stock),
                Route("/api/customers", self.api_customers),
                Route("/api/customers/{customer_id:int}", self.api_customer),
                Route("/api/chat", self.api_chat, methods=["POST"]),
            ],
            middleware=[Middleware(BaseHTTPMiddleware, dispatch=self._headers)],
        )

    # ---------------------------------------------------------------- plumbing

    async def _headers(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/") or request.url.path in ("/", "/login"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def _issue(self) -> str:
        sid = secrets.token_hex(16)
        payload = f"{sid}.{int(time.time()) + SESSION_TTL_S}"
        return f"{payload}.{self._sign(payload)}"

    def session(self, request: Request) -> str | None:
        raw = request.cookies.get(COOKIE, "")
        try:
            sid, exp, sig = raw.split(".")
            if not secrets.compare_digest(sig, self._sign(f"{sid}.{exp}")) or int(exp) < time.time() or sid in self._revoked:
                return None
        except ValueError:
            return None
        return sid

    def _secure(self, request: Request) -> bool:
        if self._cookie_secure is not None:
            return self._cookie_secure
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    @staticmethod
    def _csrf_ok(request: Request) -> bool:
        return request.headers.get(CSRF_HEADER) == CSRF_VALUE  # a custom header cannot be sent cross-site without a CORS preflight

    @staticmethod
    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def _tool(self, name: str, args: dict[str, Any]) -> Response:
        async with Client(self._server) as client:
            result = await client.call_tool(name, {k: v for k, v in args.items() if v is not None})
        if not result.is_error:
            return JSONResponse(result.structured_content)
        text = _PREFIX.sub("", "\n".join(getattr(c, "text", "") for c in result.content))
        if "validation error" in text:
            return JSONResponse({"error": "Invalid parameters."}, 400)
        status = 404 if "does not exist" in text else 429 if "rate limiting" in text else 502
        return JSONResponse({"error": text}, status)

    @staticmethod
    def _int(request: Request, name: str, default: int | None = None) -> int | None:
        try:
            return int(request.query_params[name])
        except (KeyError, ValueError):
            return default

    @staticmethod
    def _str(request: Request, name: str) -> str | None:
        value = request.query_params.get(name, "").strip()
        return value or None

    def _guard(self, request: Request) -> Response | None:
        if self.session(request) is None:
            return JSONResponse({"error": "Sign in required."}, 401)
        return None

    # ---------------------------------------------------------------- pages and auth

    async def healthz(self, _request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    async def index(self, request: Request) -> Response:
        if self.session(request) is None:
            return RedirectResponse("/login", 303)
        return Response(self._static["index.html"], media_type="text/html; charset=utf-8")

    async def login_page(self, request: Request) -> Response:
        if self.session(request) is not None:
            return RedirectResponse("/", 303)
        return Response(self._static["login.html"], media_type="text/html; charset=utf-8")

    async def static(self, request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ("app.js", "login.js", "styles.css"):
            return Response("Not found", 404)
        media = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
        return Response(self._static[name], media_type=media)

    async def login(self, request: Request) -> Response:
        if not self._csrf_ok(request):
            return JSONResponse({"error": "Forbidden."}, 403)
        ip, now = self._client_ip(request), time.monotonic()
        attempts = self._failures[ip]
        while attempts and now - attempts[0] > LOGIN_WINDOW_S:
            attempts.popleft()
        if len(attempts) >= LOGIN_MAX_FAILURES:
            return JSONResponse({"error": "Too many failed attempts. Wait a few minutes and try again."}, 429)
        try:
            supplied = str((await request.json()).get("password", ""))
        except ValueError:
            supplied = ""
        if not hmac.compare_digest(_digest(supplied), self._password):
            attempts.append(now)
            return JSONResponse({"error": "Wrong password."}, 401)
        attempts.clear()
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, self._issue(), max_age=SESSION_TTL_S, httponly=True, samesite="strict", secure=self._secure(request), path="/")
        return response

    async def logout(self, request: Request) -> Response:
        if not self._csrf_ok(request):
            return JSONResponse({"error": "Forbidden."}, 403)
        sid = self.session(request)
        if sid:
            self._revoked.add(sid)
            self._histories.pop(sid, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    # ---------------------------------------------------------------- data API (all GET, all read-only)

    async def api_config(self, request: Request) -> Response:
        if denied := self._guard(request):
            return denied
        return JSONResponse(
            {"chat_enabled": bool(os.environ.get("OPENROUTER_API_KEY")), "model": os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL), "version": __version__}
        )

    async def api_info(self, request: Request) -> Response:
        return denied if (denied := self._guard(request)) else await self._tool("get_store_info", {})

    async def api_overview(self, request: Request) -> Response:
        return denied if (denied := self._guard(request)) else await self._tool("get_store_overview", {})

    async def api_orders(self, request: Request) -> Response:
        if denied := self._guard(request):
            return denied
        common = {"page": self._int(request, "page", 1), "per_page": self._int(request, "per_page", 20)}
        status = self._str(request, "status") or "any"
        if q := self._str(request, "q"):
            return await self._tool("search_orders", {"query": q, "status": status, **common})
        return await self._tool("list_orders", {"status": status, "customer_id": self._int(request, "customer_id"), **common})

    async def api_order(self, request: Request) -> Response:
        return (
            denied if (denied := self._guard(request)) else await self._tool("get_order", {"order_id": request.path_params["order_id"], "include_notes": "all"})
        )

    async def api_tracking(self, request: Request) -> Response:
        return denied if (denied := self._guard(request)) else await self._tool("get_order_tracking", {"order_id": request.path_params["order_id"]})

    async def api_products(self, request: Request) -> Response:
        if denied := self._guard(request):
            return denied
        common = {"page": self._int(request, "page", 1), "per_page": self._int(request, "per_page", 20)}
        if q := self._str(request, "q"):
            return await self._tool("search_products", {"query": q, **common})
        return await self._tool("list_products", {"stock_status": self._str(request, "stock_status") or "any", **common})

    async def api_product(self, request: Request) -> Response:
        return denied if (denied := self._guard(request)) else await self._tool("get_product", {"product_id": request.path_params["product_id"]})

    async def api_low_stock(self, request: Request) -> Response:
        if denied := self._guard(request):
            return denied
        return await self._tool("list_low_stock", {"threshold": self._int(request, "threshold", 5), "max_products": self._int(request, "max_products", 500)})

    async def api_customers(self, request: Request) -> Response:
        if denied := self._guard(request):
            return denied
        common = {"page": self._int(request, "page", 1), "per_page": self._int(request, "per_page", 20)}
        if q := self._str(request, "q"):
            return await self._tool("search_customers", {"query": q, **common})
        return await self._tool("list_customers", common)

    async def api_customer(self, request: Request) -> Response:
        return denied if (denied := self._guard(request)) else await self._tool("get_customer", {"customer_id": request.path_params["customer_id"]})

    # ---------------------------------------------------------------- chat

    @staticmethod
    def _trim(history: list[dict]) -> list[dict]:
        """Drop whole oldest turns (never split a tool-call from its results)."""
        while len(history) > MAX_HISTORY_MESSAGES:
            users = [i for i, m in enumerate(history) if m["role"] == "user"]
            if len(users) < 2:
                break
            del history[users[0] : users[1]]
        return history

    async def api_chat(self, request: Request) -> Response:
        sid = self.session(request)
        if sid is None:
            return JSONResponse({"error": "Sign in required."}, 401)
        if not self._csrf_ok(request):
            return JSONResponse({"error": "Forbidden."}, 403)
        if not os.environ.get("OPENROUTER_API_KEY"):
            return JSONResponse({"error": "Chat is off: set OPENROUTER_API_KEY and restart the web server."}, 503)
        try:
            message = str((await request.json()).get("message", "")).strip()
        except ValueError:
            message = ""
        if not message:
            return JSONResponse({"error": "Type a question first."}, 400)
        if len(message) > CHAT_MAX_CHARS:
            return JSONResponse({"error": f"Keep questions under {CHAT_MAX_CHARS} characters."}, 413)
        hits, now = self._chat_hits[sid], time.monotonic()
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= CHAT_MAX_PER_MINUTE:
            return JSONResponse({"error": "Too many questions in a minute. Wait a moment."}, 429)
        hits.append(now)

        lock = self._chat_locks.setdefault(sid, asyncio.Lock())
        async with lock:  # one turn at a time per session keeps the history consistent
            history = self._histories.get(sid)
            try:
                answer, history, used = await ask(message, history=history, mcp_server=self._server)
            except AgentError as exc:
                return JSONResponse({"error": str(exc)}, 502)
            self._histories[sid] = self._trim(history)
            self._histories.move_to_end(sid)
            while len(self._histories) > MAX_SESSIONS:
                dropped, _ = self._histories.popitem(last=False)
                self._chat_locks.pop(dropped, None)
        return JSONResponse({"answer": answer, "tools": sorted(set(used), key=used.index)})
