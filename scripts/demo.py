#!/usr/bin/env python3
"""Drive a real MCP client through merchant scenarios; print [PASS]/[FAIL] per assertion; exit non-zero on failure.

  uv run python scripts/demo.py                    # against the store saved by `woo-connector auth`
  uv run python scripts/demo.py --store URL        # a specific store
  uv run python scripts/demo.py --fake             # against the offline fake store in tests/fake_woo.py
  uv run python scripts/demo.py --rate-limit       # + 15 concurrent calls survive a simulated 5-req/10s limit
  uv run python scripts/demo.py --https            # same scenarios over TLS with Basic auth (Docker store via Caddy)

Scenarios are written against the shape of the seeded dev store (docker/seed/seed.php), not fixed IDs.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mcp import Client  # noqa: E402

from woo_connector.app import build_server  # noqa: E402
from woo_connector.auth import credentials  # noqa: E402
from woo_connector.tools import common  # noqa: E402
from woo_connector.woocommerce import oauth1  # noqa: E402
from woo_connector.woocommerce.client import ClientSettings, WooClient  # noqa: E402

INJECTION_MARKER = "ignore all previous instructions"
results: list[bool] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))


async def call(name: str, args: dict | None = None):
    async with Client(build_server()) as c:
        r = await c.call_tool(name, args or {})
    text = "\n".join(getattr(x, "text", "") for x in r.content)
    return r.is_error, (text if r.is_error else r.structured_content)


def keys_holding(node, needle: str, path: str = "") -> list[str]:
    """Return the key names under which `needle` (lower-case) appears anywhere in the tree."""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += keys_holding(v, needle, k)
    elif isinstance(node, list):
        for v in node:
            found += keys_holding(v, needle, path)
    elif isinstance(node, str) and needle in node.lower():
        found.append(path)
    return found


async def scenarios(store_url: str, creds) -> None:
    err, info = await call("get_store_info")
    check(not err and info.get("read_only") is True and info.get("pii_mode") == "masked", "store info: read-only, PII masked by default", str(info)[:200])
    if creds.key_permissions != "unknown":
        check(info.get("key_permissions") == "read", "the stored key is a read-only key")

    err, found = await call("search_orders", {"query": "dana@example.com"})
    rows = [] if err else found["items"]
    check(
        bool(rows) and all("***" in r.get("customer_email", "") for r in rows),
        "find an order by customer email; the address comes back masked",
        str(found)[:200],
    )
    blob = json.dumps(found)
    check("dana@example.com" not in blob, "no full email address anywhere in the result")

    err, products = await call("list_products", {"product_type": "variable"})
    variable = [] if err else products["items"]
    if variable:
        err, detail = await call("get_product", {"product_id": variable[0]["id"]})
        check(
            not err and detail.get("variations") and all("stock" in v for v in detail["variations"]),
            "per-variant stock for a variable product",
            str(detail)[:200],
        )
    else:
        check(False, "per-variant stock (the store has no variable product: did you run the seed?)")

    err, over = await call("get_store_overview")
    check(
        not err and over["needs_attention"] and all(isinstance(v, int) for v in over["counts"].values()),
        "what needs attention: counts + sentences",
        str(over)[:200],
    )

    # prompt injection arrives as data
    hostile = None
    for status in ("on-hold", "processing", "pending", "completed"):
        err, page = await call("list_orders", {"status": status, "per_page": 100})
        for row in [] if err else page["items"]:
            err, full = await call("get_order", {"order_id": row["id"], "include_notes": "none"})
            if not err and INJECTION_MARKER in full.get("customer_note_text", "").lower():
                hostile = full
                break
        if hostile:
            break
    check(hostile is not None, "found the order whose customer note tries a prompt injection")
    if hostile:
        holders = keys_holding(hostile, INJECTION_MARKER)
        check(holders and all(h.endswith("_text") for h in holders), "the injection arrives only under a *_text key (data, not instructions)", str(holders))
        check(len(hostile["customer_note_text"]) <= 500, "untrusted text is truncated to 500 chars")

    # staff notes are hidden from customer-facing reads
    err, held = await call("list_orders", {"status": "on-hold", "per_page": 100})
    staff_seen = customer_clean = False
    for row in [] if err else held["items"]:
        _, allnotes = await call("get_order", {"order_id": row["id"], "include_notes": "all"})
        if any(not n["visible_to_customer"] for n in allnotes.get("notes", [])):
            staff_seen = True
            _, cust = await call("get_order", {"order_id": row["id"], "include_notes": "customer"})
            customer_clean = all(n["visible_to_customer"] for n in cust.get("notes", []))
            break
    check(staff_seen and customer_clean, "staff notes exist, and include_notes='customer' hides them")

    err, text = await call("get_customer", {"customer_id": 1})
    check(err and "does not exist" in text, "the admin account is not reachable as a customer")

    err, text = await call("get_order", {"order_id": 999999})
    check(err and "Use search_orders" in text and "Traceback" not in text, "unknown order -> an error that says what to do next")

    # the key itself cannot write
    base = f"{store_url}/wp-json/wc/v3/orders"
    import httpx

    verify = os.environ.get("WOO_VERIFY_TLS", "true").lower() not in ("0", "false", "no")
    with httpx.Client(verify=verify, timeout=20) as http:
        if store_url.startswith("https://"):
            r = http.post(base, json={"status": "cancelled"}, auth=(creds.consumer_key, creds.consumer_secret))
        else:
            r = http.post(oauth1.sign_url("POST", base, [], creds.consumer_key, creds.consumer_secret), json={"status": "cancelled"})
    if creds.key_permissions == "read":
        check(r.status_code == 401, "a WRITE with the connector's own key is refused by the store (HTTP 401)", f"got {r.status_code}")
    else:
        print(f"[SKIP] write refusal: key scope is '{creds.key_permissions}'. Only a key created with scope=read proves this (got HTTP {r.status_code}).")


async def rate_limit_scenario(set_limit, clear_limit, store_url: str, creds) -> None:
    set_limit()
    try:
        client = WooClient(creds, ClientSettings(max_rps=50, burst=50, budget_s=90))
        results_ = await asyncio.gather(*(client.get_page("wc/v3/orders", page=1, per_page=1) for _ in range(15)), return_exceptions=True)
        failures = [r for r in results_ if isinstance(r, Exception)]
        check(not failures, "15 concurrent calls all succeed under a simulated 5 req / 10 s limit", str(failures[:1]))
        check(client.stats.rate_limited >= 1, f"the limit really fired and was handled ({client.stats.rate_limited} x 429, {client.stats.retries} retries)")
        await client.aclose()
    finally:
        clear_limit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--rate-limit", action="store_true")
    ap.add_argument("--https", action="store_true", help="use https://localhost:8443 (Caddy, self-signed) with Basic auth")
    args = ap.parse_args()

    fake_ctx = None
    set_limit = clear_limit = lambda: None  # noqa: E731
    if args.fake:
        from tests.fake_woo import KEY, SECRET, RunningFake

        fake_ctx = RunningFake().__enter__()
        creds = credentials.Credentials(fake_ctx.url, KEY, SECRET, "read")
        set_limit = lambda: setattr(fake_ctx.fake, "limit", (5, 10.0))  # noqa: E731
        clear_limit = lambda: setattr(fake_ctx.fake, "limit", None)  # noqa: E731
    else:
        store = args.store or ("https://localhost:8443" if args.https else None)
        if args.https:
            os.environ["WOO_VERIFY_TLS"] = "false"  # Caddy's internal CA; dev store only
        creds = credentials.load(store)
        if args.https and creds.store_url.startswith("http://"):
            creds = credentials.Credentials("https://localhost:8443", creds.consumer_key, creds.consumer_secret, creds.key_permissions)
        wp = ROOT / "scripts" / "wp.sh"
        set_limit = lambda: subprocess.run([str(wp), "option", "update", "connector_dev_rate_limit", "5"], check=True, capture_output=True)  # noqa: E731
        clear_limit = lambda: subprocess.run([str(wp), "option", "delete", "connector_dev_rate_limit"], check=False, capture_output=True)  # noqa: E731

    client = WooClient(creds, ClientSettings.from_env())
    common.configure(client=client, store_url=creds.store_url, pii_mode="masked")
    print(f"Store: {creds.store_url}  (auth: {'Basic over HTTPS' if creds.store_url.startswith('https://') else 'OAuth 1.0a over HTTP'})\n")
    try:
        asyncio.run(scenarios(creds.store_url, creds))
        if args.rate_limit:
            print()
            asyncio.run(rate_limit_scenario(set_limit, clear_limit, creds.store_url, creds))
    finally:
        if fake_ctx:
            fake_ctx.__exit__(None, None, None)
    failed = results.count(False)
    print(f"\n{results.count(True)} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
