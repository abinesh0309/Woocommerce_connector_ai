"""ASGI guard for the HTTP transport: every request needs ``Authorization: Bearer $WOO_MCP_TOKEN``."""

import json
import secrets

OPEN_PATHS = frozenset({"/healthz"})


def generate_token() -> str:
    return secrets.token_urlsafe(32)


class BearerGuard:
    """Fails closed: unknown scope types are refused, websockets are closed, lifespan passes through."""

    def __init__(self, app, token: str, open_paths: frozenset[str] = OPEN_PATHS):
        if not token:
            raise ValueError("A non-empty bearer token is required.")
        self.app = app
        self._token = token.encode()
        self._open_paths = open_paths

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            return  # unknown scope type: refuse silently

        if scope["method"] == "GET" and scope["path"] in self._open_paths:
            await self.app(scope, receive, send)
            return

        if self._authorized(scope):
            await self.app(scope, receive, send)
            return

        body = json.dumps({"error": "unauthorized", "hint": "Send 'Authorization: Bearer <WOO_MCP_TOKEN>'."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="woo-connector"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, supplied = value.partition(b" ")
                if scheme.lower() != b"bearer":
                    return False
                return secrets.compare_digest(supplied.strip(), self._token)
        return False
