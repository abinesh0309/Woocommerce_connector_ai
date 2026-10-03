#!/usr/bin/env python3
"""Run the real dashboard JavaScript (in jsdom) against a live `woo-connector web` server backed by the fake store.

    cd tests/ui && npm install      # once: installs jsdom
    uv run python scripts/ui_smoke.py

Needs Node.js. A fake OpenRouter answers the chat so no key or network is needed.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from tests.fake_woo import KEY, SECRET, RunningFake  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return server


async def fake_llm(request):
    body = await request.json()
    last = body["messages"][-1]
    if last["role"] == "user":  # first step: ask for the overview tool
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "get_store_overview", "arguments": "{}"}}],
        }
    else:  # after the tool result: answer from it
        counts = json.loads(last["content"])["counts"]
        msg = {
            "role": "assistant",
            "content": f"**{counts['processing']} orders** are processing and {counts['on_hold']} is on hold. See the [admin panel](http://shop.example/wp-admin) or [bad](javascript:alert(1)). <img src=x onerror=alert(1)>",
        }
    return JSONResponse({"choices": [{"message": msg}]})


def main() -> int:
    llm_port = free_port()
    os.environ.update(
        OPENROUTER_API_KEY="sk-or-test", OPENROUTER_URL=f"http://127.0.0.1:{llm_port}/v1/chat/completions", XDG_CONFIG_HOME="/tmp/woo-ui-smoke-cfg"
    )
    from woo_connector.auth.credentials import Credentials
    from woo_connector.tools import common
    from woo_connector.web.app import WebApp
    from woo_connector.woocommerce.client import ClientSettings, WooClient

    llm = serve(Starlette(routes=[Route("/v1/chat/completions", fake_llm, methods=["POST"])]), llm_port)
    with RunningFake() as store:
        common.configure(
            client=WooClient(Credentials(store.url, KEY, SECRET, "read"), ClientSettings(max_rps=50, burst=50)), store_url=store.url, pii_mode="masked"
        )
        web_port = free_port()
        web = serve(WebApp("ui-test-password", "ui-test-secret").app, web_port)
        try:
            proc = subprocess.run(["node", str(ROOT / "tests/ui/ui_smoke.mjs"), f"http://127.0.0.1:{web_port}", "ui-test-password"], cwd=ROOT / "tests/ui")
        finally:
            web.should_exit = llm.should_exit = True
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
