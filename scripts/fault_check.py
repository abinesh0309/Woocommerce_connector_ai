#!/usr/bin/env python3
"""Fault injection in ONE long-lived connector session: store down, key revoked, 40 concurrent calls.

Asserts clear, actionable messages and recovery WITHOUT restarting the connector.

  uv run python scripts/fault_check.py --fake      # offline, against tests/fake_woo.py
  uv run python scripts/fault_check.py             # against the Docker dev store (docker/, scripts/wp.sh)
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mcp import Client  # noqa: E402

from woo_connector.app import build_server  # noqa: E402
from woo_connector.auth import credentials  # noqa: E402
from woo_connector.tools import common  # noqa: E402

results: list[bool] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))


async def call(name: str, args: dict | None = None):
    async with Client(build_server()) as c:
        r = await c.call_tool(name, args or {})
    return r.is_error, ("\n".join(getattr(x, "text", "") for x in r.content) if r.is_error else r.structured_content)


class FakeControl:
    def __init__(self):
        from tests.fake_woo import KEY, SECRET, RunningFake

        self.running = RunningFake().__enter__()
        self.creds = credentials.Credentials(self.running.url, KEY, SECRET, "read")
        self._cls = RunningFake

    def stop_store(self):
        self.running.__exit__(None, None, None)

    def start_store(self):
        self.running = self._cls(self.running.fake, port=self.running.port).__enter__()

    def revoke_key(self):
        self.running.fake.revoked = True

    def restore_key(self):  # same key becomes valid again; a "reconnect" would write fresh credentials
        self.running.fake.revoked = False
        return self.creds

    def close(self):
        self.running.__exit__(None, None, None)


class DockerControl:
    def __init__(self):
        self.wp = str(ROOT / "scripts" / "wp.sh")
        self.compose = ["docker", "compose", "-f", str(ROOT / "docker" / "docker-compose.yml")]
        self.creds = credentials.load()

    def stop_store(self):
        subprocess.run([*self.compose, "stop", "wordpress"], check=True, capture_output=True)

    def start_store(self):
        subprocess.run([*self.compose, "start", "wordpress"], check=True, capture_output=True)
        time.sleep(8)  # php-fpm/apache warm-up

    def revoke_key(self):
        subprocess.run([self.wp, "db", "query", "DELETE FROM wp_woocommerce_api_keys"], check=True, capture_output=True)

    def restore_key(self):
        out = subprocess.run([self.wp, "eval-file", "/seed/make_key.php"], check=True, capture_output=True, text=True).stdout
        data = json.loads(out.strip().splitlines()[-1])
        creds = credentials.Credentials(self.creds.store_url, data["consumer_key"], data["consumer_secret"], "read")
        credentials.save(creds)  # what `woo-connector auth` would have done
        return creds

    def close(self):
        pass


async def run(ctl) -> None:
    from woo_connector.woocommerce.client import ClientSettings, WooClient

    client = WooClient(ctl.creds, ClientSettings(max_rps=20, burst=20, budget_s=45))
    common.configure(client=client, store_url=ctl.creds.store_url)
    session_client = common.runtime  # one long-lived runtime/session for the whole script

    err, out = await call("get_store_overview")
    check(not err, "baseline: the connector works")

    print("\n-- store goes down; 40 concurrent calls --")
    ctl.stop_store()
    t0 = time.monotonic()
    outcomes = await asyncio.gather(*(call("list_orders", {"per_page": 1}) for _ in range(40)))
    took = time.monotonic() - t0
    check(all(err for err, _ in outcomes), "all 40 calls fail cleanly (none hang, none crash the connector)")
    check(
        all("Could not reach the store" in text for _, text in outcomes), "every failure says the store is unreachable and what to check", outcomes[0][1][:160]
    )
    check(not any("Traceback" in text for _, text in outcomes), "no stack traces reach the agent")
    check(took < 120, f"bounded time: {took:.1f}s for 40 concurrent calls")

    print("\n-- store comes back, same session --")
    ctl.start_store()
    err, out = await call("get_store_overview")
    check(not err, "recovers without restarting the connector", str(out)[:160])

    print("\n-- key revoked --")
    ctl.revoke_key()
    err, text = await call("list_orders", {"per_page": 1})
    check(err and "reconnect the store" in text, "revoked key -> 'the merchant needs to reconnect the store'", text[:160])
    check(session_client._client is None, "the failed auth dropped the cached client (fresh credentials will be loaded)")

    print("\n-- merchant reconnects (new/restored key), same session --")
    creds = ctl.restore_key()
    session_client._client = WooClient(creds, ClientSettings(max_rps=20, burst=20))  # what credentials.load() would build after `auth`
    err, out = await call("list_orders", {"per_page": 1})
    check(not err and out["total"] >= 1, "works again after reconnecting, without restarting the connector", str(out)[:160])
    ctl.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    args = ap.parse_args()
    ctl = FakeControl() if args.fake else DockerControl()
    asyncio.run(run(ctl))
    print(f"\n{results.count(True)} passed, {results.count(False)} failed")
    return 1 if False in results else 0


if __name__ == "__main__":
    sys.exit(main())
