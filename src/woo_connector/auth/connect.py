"""WooCommerce's built-in consent flow (``/wc-auth/v1/authorize``) and a manual API-key path.

Flow: browser -> store consent page -> store POSTs the new key *server to server* to our HTTPS callback
-> store redirects the browser to our return URL with ``success=1``. We keep the key in memory and
only write it to disk once the browser has landed on the return URL, because WooCommerce deletes the
key if anything after the callback fails.
"""

import getpass
import json
import secrets
import ssl
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from woo_connector.auth import credentials
from woo_connector.auth.credentials import Credentials, normalize_store_url


class ConsentError(Exception):
    """The consent flow failed; the message says why and what to do."""


_PAGE = "<!doctype html><meta charset=utf-8><title>Woo Connector</title><body style='font:16px system-ui;max-width:32rem;margin:4rem auto'><h2>{title}</h2><p>{body}</p>"  # noqa: E501


def make_self_signed_cert(directory: Path, hosts: tuple[str, ...]) -> tuple[Path, Path]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    san = ",".join(f"DNS:{h}" for h in hosts) + ",IP:127.0.0.1"
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=woo-connector-callback",
                "-addext",
                f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ConsentError(
            "Could not create a temporary TLS certificate (is `openssl` installed?). WooCommerce only sends the key to an https:// callback. "
            "Install openssl, pass --callback-url with a public HTTPS tunnel, or use `woo-connector auth --manual`."
        ) from exc
    return cert, key


class ConsentSession:
    def __init__(
        self,
        store_url: str,
        *,
        scope: str = "read",
        app_name: str = "Woo Connector (read-only)",
        callback_host: str = "localhost",
        callback_url: str | None = None,
        callback_port: int = 0,
        return_port: int = 0,
        timeout: float = 300.0,
    ):
        if callback_url is not None and not callback_url.lower().startswith("https://"):
            raise ConsentError("--callback-url must be https:// (WooCommerce refuses to send API keys to a plain http callback).")
        self.store_url = normalize_store_url(store_url)
        self.scope = scope
        self.app_name = app_name
        self.callback_host = callback_host
        self.public_callback_url = callback_url
        self.timeout = timeout
        self.state = secrets.token_hex(16)  # 32 chars, echoed back by WooCommerce: doubles as CSRF state
        self._callback_port, self._return_port = callback_port, return_port
        self._pending: dict | None = None
        self._result: Credentials | None = None
        self._error: str | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._servers: list[ThreadingHTTPServer] = []
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.callback_port = 0
        self.return_port = 0

    # ---- handlers -------------------------------------------------------------------------

    def _state_ok(self, supplied: object) -> bool:
        return isinstance(supplied, str) and secrets.compare_digest(supplied.encode(), self.state.encode())

    def handle_callback(self, body: bytes, content_type: str) -> tuple[int, str]:
        try:
            if "json" in content_type or body.lstrip().startswith(b"{"):
                data = json.loads(body or b"{}")
            else:
                data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        except (ValueError, UnicodeDecodeError):
            return 400, "bad request"
        if not isinstance(data, dict) or not self._state_ok(data.get("user_id")):
            return 403, "forbidden"  # wrong state: discard everything, keep nothing
        if not (data.get("consumer_key") and data.get("consumer_secret")):
            return 400, "missing key"
        with self._lock:
            self._pending = {
                "consumer_key": str(data["consumer_key"]),
                "consumer_secret": str(data["consumer_secret"]),
                "key_permissions": str(data.get("key_permissions", "unknown")),
            }
        return 200, "ok"

    def handle_return(self, query: str) -> tuple[int, str]:
        q = {k: v[0] for k, v in parse_qs(query).items()}
        with self._lock:
            if not self._state_ok(q.get("user_id")):
                return 403, _PAGE.format(title="Invalid request", body="State mismatch. Nothing was saved.")
            if q.get("success") == "1" and self._pending:
                p, self._pending = self._pending, None
                self._result = Credentials(self.store_url, p["consumer_key"], p["consumer_secret"], p["key_permissions"])
                self._done.set()
                return 200, _PAGE.format(title="Connected", body="The store is connected. You can close this tab and return to the terminal.")
            self._pending = None  # discard any held key
            self._error = "The store reported that authorisation did not complete (success!=1) or no key arrived at the callback."
            self._done.set()
            return 400, _PAGE.format(title="Not connected", body="Authorisation did not complete. Nothing was saved. Return to the terminal for details.")

    def _handler(self, kind: str):
        session = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # never log (query strings hold state)
                pass

            def _reply(self, code: int, text: str, ctype: str):
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if kind != "callback":
                    return self._reply(404, "not found", "text/plain")
                n = min(int(self.headers.get("Content-Length") or 0), 65536)
                code, text = session.handle_callback(self.rfile.read(n), self.headers.get("Content-Type", ""))
                self._reply(code, text, "text/plain")

            def do_GET(self):
                if kind != "return":
                    return self._reply(404, "not found", "text/plain")
                code, text = session.handle_return(urlsplit(self.path).query)
                self._reply(code, text, "text/html; charset=utf-8")

        return Handler

    # ---- lifecycle ------------------------------------------------------------------------

    def start(self) -> str:
        """Start both listeners and return the URL the merchant must open."""
        cb = ThreadingHTTPServer(
            ("0.0.0.0" if self.callback_host not in ("localhost", "127.0.0.1") else "127.0.0.1", self._callback_port), self._handler("callback")
        )
        if self.public_callback_url is None:  # local callback: terminate TLS ourselves with a throwaway cert
            self._tmp = tempfile.TemporaryDirectory(prefix="woo-connector-")
            cert, key = make_self_signed_cert(Path(self._tmp.name), (self.callback_host, "localhost", "host.docker.internal"))
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            cb.socket = ctx.wrap_socket(cb.socket, server_side=True)
        ret = ThreadingHTTPServer(("127.0.0.1", self._return_port), self._handler("return"))
        self._servers = [cb, ret]
        self.callback_port, self.return_port = cb.server_address[1], ret.server_address[1]
        for srv in self._servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        callback = self.public_callback_url or f"https://{self.callback_host}:{self.callback_port}/callback"
        query = {
            "app_name": self.app_name,
            "scope": self.scope,
            "user_id": self.state,
            "return_url": f"http://localhost:{self.return_port}/return",
            "callback_url": callback,
        }
        return f"{self.store_url}/wc-auth/v1/authorize?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())

    def wait(self) -> Credentials:
        if not self._done.wait(self.timeout):
            raise ConsentError(f"Timed out after {int(self.timeout)}s waiting for approval. Nothing was saved. Run `woo-connector auth` again.")
        if self._result is None:
            raise ConsentError(self._error or "Authorisation failed.")
        return self._result

    def close(self) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        if self._tmp:
            self._tmp.cleanup()


def authorize(store_url: str, *, open_browser: bool = True, announce=print, **kwargs) -> Credentials:
    session = ConsentSession(store_url, **kwargs)
    try:
        url = session.start()
        announce("Open this URL in a browser where you are logged in to the store as an administrator:\n\n  " + url + "\n")
        if open_browser:
            webbrowser.open(url)
        creds = session.wait()
    finally:
        session.close()
    if creds.key_permissions not in (session.scope, "unknown"):
        announce(f"Warning: requested scope '{session.scope}' but the store granted '{creds.key_permissions}'.")
    credentials.save(creds)
    return creds


def manual_credentials(store_url: str, key: str | None = None, secret: str | None = None) -> Credentials:
    """Collect a pasted key (WooCommerce > Settings > Advanced > REST API). Scope is not guessed."""
    key = (input("Consumer key (ck_...): ") if key is None else key).strip()
    secret = (getpass.getpass("Consumer secret (cs_..., hidden): ") if secret is None else secret).strip()
    if not key or not secret:
        raise ConsentError("Both the consumer key and secret are required.")
    return Credentials(normalize_store_url(store_url), key, secret, "unknown")
