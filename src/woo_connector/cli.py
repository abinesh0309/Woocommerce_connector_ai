"""``woo-connector`` command line."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from woo_connector import __version__
from woo_connector.auth import credentials
from woo_connector.auth.bearer import generate_token
from woo_connector.auth.connect import ConsentError, authorize, manual_credentials
from woo_connector.auth.credentials import NotConnected, normalize_store_url
from woo_connector.logging_setup import configure as configure_logging
from woo_connector.woocommerce.client import ClientSettings, WooClient
from woo_connector.woocommerce.errors import WooError

SPEC_FILE = "mcp-tool-spec.json"


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


async def build_spec() -> dict:
    from mcp import Client

    from woo_connector.app import INSTRUCTIONS, build_server

    async with Client(build_server()) as client:
        tools = (await client.list_tools()).tools
    out = []
    for t in tools:
        d = t.model_dump(by_alias=True, exclude_none=True, mode="json")
        out.append(
            {
                "name": d["name"],
                "description": d.get("description", ""),
                "annotations": d.get("annotations", {}),
                "inputSchema": d["inputSchema"],
                "outputSchema": d.get("outputSchema"),
            }
        )
    return {
        "name": "woo-connector",
        "version": __version__,
        "protocol": "Model Context Protocol",
        "transports": ["stdio", "streamable-http (/mcp, bearer token)"],
        "instructions": INSTRUCTIONS,
        "tools": out,
    }


def cmd_spec(args) -> int:
    spec = asyncio.run(build_spec())
    if args.summary:
        print(f"{len(spec['tools'])} tools, all read-only: " + ", ".join(t["name"] for t in spec["tools"]))
        return 0
    text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text)
        err(f"Wrote {args.output} ({len(spec['tools'])} tools).")
    return 0


def cmd_auth(args) -> int:
    if not args.store:
        err("Pass --store https://your-shop.example (or set WOO_STORE_URL).")
        return 2
    try:
        if args.manual:
            creds = manual_credentials(args.store, os.environ.get("WOO_CONSUMER_KEY"), os.environ.get("WOO_CONSUMER_SECRET"))
            err("Checking the key against the store...")
            visible = asyncio.run(_probe(creds))
            path = credentials.save(creds)
            print(
                f"Saved to {path}. The store shows {visible} order(s) to this key.\nKey scope is unknown for pasted keys: make sure you created it with 'Read' permissions."  # noqa: E501
            )
        else:
            creds = authorize(
                args.store,
                open_browser=not args.no_browser,
                announce=err,
                scope=args.scope,
                callback_host=args.callback_host,
                callback_url=args.callback_url,
                callback_port=args.callback_port,
                return_port=args.return_port,
                timeout=args.timeout,
            )
            print(f"Connected to {creds.store_url} with '{creds.key_permissions}' permissions. Saved to {credentials.credentials_path()}.")
    except (ConsentError, ValueError, WooError) as exc:
        err(f"Error: {exc}")
        return 1
    return 0


async def _probe(creds) -> int:
    client = WooClient(creds, ClientSettings.from_env())
    try:
        page = await client.get_page("wc/v3/orders", page=1, per_page=1)
        return page.total
    finally:
        await client.aclose()


def cmd_status(args) -> int:
    try:
        creds = credentials.load(args.store)
        total = asyncio.run(_probe(creds))
    except (NotConnected, WooError) as exc:
        err(f"Not working: {exc}")
        return 1
    print(f"OK: {creds.store_url} is reachable. Key permissions: {creds.key_permissions}. Orders visible to this key: {total}.")
    return 0


def cmd_forget(args) -> int:
    try:
        store = args.store or (credentials.saved_stores() or [None])[0]
        if not store:
            err("Nothing to forget: no store is saved.")
            return 1
        removed = credentials.forget(store)
    except ValueError as exc:
        err(f"Error: {exc}")
        return 1
    if not removed:
        err(f"No saved credentials for {normalize_store_url(store)}.")
        return 1
    print(
        f"Deleted the local credentials for {normalize_store_url(store)}.\nThe key still works until you revoke it: WooCommerce > Settings > Advanced > REST API > Revoke."  # noqa: E501
    )
    return 0


def cmd_serve(args) -> int:
    from woo_connector.app import build_http_app, build_server
    from woo_connector.tools.common import runtime

    configure_logging()
    runtime.store_url = args.store
    try:
        credentials.load(args.store)
    except NotConnected as exc:
        err(f"Warning: {exc} Tools will report this until a store is connected.")
    server = build_server()
    if not args.http:
        server.run("stdio")
        return 0
    import uvicorn

    token = os.environ.get("WOO_MCP_TOKEN")
    if not token:
        token = generate_token()
        err(f"WOO_MCP_TOKEN was not set; generated one for this run:\n  {token}")
    err(f"Serving MCP on http://{args.host}:{args.port}/mcp (Authorization: Bearer <token>); health at /healthz")
    uvicorn.run(build_http_app(server, token, args.host), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_web(args) -> int:
    import secrets as _secrets

    import uvicorn

    from woo_connector.tools.common import runtime
    from woo_connector.web.app import WebApp

    configure_logging()
    runtime.store_url = args.store
    try:
        creds = credentials.load(args.store)
    except NotConnected as exc:
        err(f"Error: {exc}")
        return 1
    password = os.environ.get("WOO_WEB_PASSWORD")
    if not password:
        password = _secrets.token_urlsafe(12)
        err(f"WOO_WEB_PASSWORD was not set; generated one for this run:\n  {password}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        err("Warning: listening beyond this machine. Put it behind HTTPS (a reverse proxy) so the password and session cookie are encrypted.")
    if not os.environ.get("OPENROUTER_API_KEY"):
        err("Note: OPENROUTER_API_KEY is not set, so the chat panel will be disabled. The dashboard works without it.")
    web = WebApp(password, os.environ.get("WOO_WEB_SECRET"))
    err(f"Dashboard for {creds.store_url}: http://{args.host}:{args.port}")
    uvicorn.run(web.app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_chat(args) -> int:
    from woo_connector.agent import chat
    from woo_connector.tools.common import runtime

    configure_logging()
    runtime.store_url = args.store
    return asyncio.run(chat(args.question, args.model))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="woo-connector", description="Read-only WooCommerce connector (MCP server) for AI agents.")
    p.add_argument("--store", default=os.environ.get("WOO_STORE_URL"), help="Store URL, e.g. https://shop.example.com (or WOO_STORE_URL)")
    p.add_argument("--version", action="version", version=f"woo-connector {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("auth", help="Connect a store (consent flow, or --manual to paste a key)")
    a.add_argument("--manual", action="store_true", help="Paste a consumer key/secret instead of the browser consent flow")
    a.add_argument("--no-browser", action="store_true", help="Print the approval URL instead of opening a browser")
    a.add_argument("--callback-url", help="Public HTTPS URL that forwards to --callback-port (needed when the store is not on this machine)")
    a.add_argument(
        "--callback-host", default="localhost", help="Host name the store should call back (default localhost; host.docker.internal for Docker stores)"
    )
    a.add_argument("--callback-port", type=int, default=0)
    a.add_argument("--return-port", type=int, default=0)
    a.add_argument("--scope", default="read", choices=["read", "write", "read_write"], help="Key permissions to request (default read)")
    a.add_argument("--timeout", type=float, default=300.0)
    a.set_defaults(func=cmd_auth)

    sub.add_parser("status", help="Check the saved connection").set_defaults(func=cmd_status)
    sub.add_parser("forget", help="Delete the local credentials").set_defaults(func=cmd_forget)

    s = sub.add_parser("serve", help="Run the MCP server (stdio by default)")
    s.add_argument("--http", action="store_true", help="Streamable HTTP at /mcp behind a bearer token")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    w = sub.add_parser("web", help="Run the dashboard + AI chat in your browser")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8080)
    w.set_defaults(func=cmd_web)

    c = sub.add_parser("chat", help="Try the tools with an OpenRouter model")
    c.add_argument("-q", "--question")
    c.add_argument("--model")
    c.set_defaults(func=cmd_chat)

    sp = sub.add_parser("spec", help="Write the MCP tool specification")
    sp.add_argument("-o", "--output", default=SPEC_FILE, help=f"Output file (default {SPEC_FILE}; '-' for stdout)")
    sp.add_argument("--summary", action="store_true")
    sp.set_defaults(func=cmd_spec)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
