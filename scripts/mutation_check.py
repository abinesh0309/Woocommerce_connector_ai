#!/usr/bin/env python3
"""Plant deliberate bugs one at a time and require the test suite to fail for each.

Always works on a COPY of the repo (killing this script mid-run can never leave a bug in your source),
and restores each file in a `finally`.

    uv run python scripts/mutation_check.py            # all mutations
    uv run python scripts/mutation_check.py -k masking # only mutations whose name contains 'masking'
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (name, file relative to repo, exact old text, new text)
MUTATIONS = [
    ("pii: email unmasked", "src/woo_connector/records/shaping.py", 'return f"{local[:1]}***@{domain}"', "return str(email)"),
    ("pii: phone unmasked", "src/woo_connector/records/shaping.py", 'return "*" * (len(digits) - 4) + digits[-4:]', "return str(phone)"),
    ("pii: street address exposed when masked", "src/woo_connector/records/shaping.py", 'full = pii == "full"\n    out = {', "full = True\n    out = {"),
    ("customers: role filter off", "src/woo_connector/tools/customers.py", 'return [c for c in items if c.get("role") == "customer"]', "return items"),
    (
        "consent: callback state unchecked",
        "src/woo_connector/auth/connect.py",
        "return isinstance(supplied, str) and secrets.compare_digest(supplied.encode(), self.state.encode())",
        "return True",
    ),
    (
        "client: 429 not retried",
        "src/woo_connector/woocommerce/client.py",
        "RETRY_STATUSES = frozenset({429, 502, 503, 504})",
        "RETRY_STATUSES = frozenset({502, 503, 504})",
    ),
    (
        "client: Retry-After ignored",
        "src/woo_connector/woocommerce/client.py",
        'retry_after = parse_retry_after(response.headers.get("Retry-After"))',
        "retry_after = None",
    ),
    ("client: no time budget", "src/woo_connector/woocommerce/client.py", "if self._clock() + delay > deadline:", "if False:"),
    ("client: no max-wait fail-fast", "src/woo_connector/woocommerce/client.py", "if wait > self.settings.max_wait_s:", "if False:"),
    ("client: redirects followed", "src/woo_connector/woocommerce/client.py", "follow_redirects=False", "follow_redirects=True"),
    ("client: reused OAuth nonce", "src/woo_connector/woocommerce/oauth1.py", "nonce or secrets.token_hex(16)", 'nonce or "fixed-nonce"'),
    (
        "oauth1: secret without trailing &",
        "src/woo_connector/woocommerce/oauth1.py",
        'key = (rfc3986(consumer_secret) + "&").encode()',
        "key = rfc3986(consumer_secret).encode()",
    ),
    (
        "client: 401 vs 403 mixed up (revoked key not advised to reconnect)",
        "src/woo_connector/woocommerce/client.py",
        "            if status == 403:",
        "            if status in (401, 403):",
    ),
    ("client: clock-skew diagnosis removed", "src/woo_connector/woocommerce/client.py", 'if "timestamp" in low or "nonce" in low:', "if False:"),
    ("client: cache ignores expiry", "src/woo_connector/woocommerce/client.py", "if hit and hit[0] > self._clock():", "if hit:"),
    ("credentials: file world-readable", "src/woo_connector/auth/credentials.py", "os.fchmod(fd, 0o600)", "os.fchmod(fd, 0o644)"),
    (
        "shaping: text not truncated",
        "src/woo_connector/records/shaping.py",
        '    return raw if len(raw) <= limit else raw[: limit - 1].rstrip() + "…"',
        "    return raw",
    ),
    ("shaping: HTML not stripped", "src/woo_connector/records/shaping.py", 'if "<" in raw:', "if False:"),
    (
        "bearer: websocket allowed",
        "src/woo_connector/auth/bearer.py",
        'await send({"type": "websocket.close", "code": 1008})',
        "await self.app(scope, receive, send)",
    ),
    (
        "bearer: any Authorization header accepted",
        "src/woo_connector/auth/bearer.py",
        "return secrets.compare_digest(supplied.strip(), self._token)",
        "return True",
    ),
    (
        "tools: stale client kept after auth failure",
        "src/woo_connector/tools/common.py",
        "            await runtime.reset()\n            raise ToolError(str(exc)) from None",
        "            raise ToolError(str(exc)) from None",
    ),
    (
        "low stock: parent stock counted per variation",
        "src/woo_connector/tools/products.py",
        'if v.get("manage_stock") == "parent":\n                continue',
        "if False:\n                continue",
    ),
    (
        "search_orders: exact match not first",
        "src/woo_connector/tools/orders.py",
        'items = [exact] + [o for o in items if o.get("id") != exact["id"]]',
        'items = [o for o in items if o.get("id") != exact["id"]] + [exact]',
    ),
    (
        "schema: per_page cap removed",
        "src/woo_connector/tools/common.py",
        "PerPage = Annotated[int, Field(ge=1, le=100,",
        "PerPage = Annotated[int, Field(ge=1, le=100000,",
    ),
    (
        "tracking: invents a hint-less empty result",
        "src/woo_connector/tools/orders.py",
        'if source == "none":\n        hint = (',
        "if False:\n        hint = (",
    ),
    (
        "annotations: readOnlyHint dropped",
        "src/woo_connector/app.py",
        "READ_ONLY = ToolAnnotations(readOnlyHint=True,",
        "READ_ONLY = ToolAnnotations(readOnlyHint=False,",
    ),
]


def copy_repo(dest: Path) -> None:
    ignore = shutil.ignore_patterns(".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "*.egg-info")
    shutil.copytree(ROOT, dest, ignore=ignore)


def run_suite(repo: Path, python: str) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONPATH": str(repo / "src") + os.pathsep + str(repo), "XDG_CONFIG_HOME": str(repo / ".cfg"), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--timeout", "40", "--tb=line"], cwd=repo, env=env, capture_output=True, text=True
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-400:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only mutations whose name contains this text")
    args = ap.parse_args()
    python = sys.executable
    workdir = Path(tempfile.mkdtemp(prefix="woo-mutation-"))
    repo = workdir / "repo"
    copy_repo(repo)
    print(f"Working on a copy: {repo}\n")

    probe = subprocess.run(
        [python, "-c", "import woo_connector, sys; print(woo_connector.__file__)"],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(repo / "src")},
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not probe.startswith(str(repo)):
        print(f"Refusing to run: the copy is not what Python imports ({probe}).")
        shutil.rmtree(workdir, ignore_errors=True)
        return 2

    ok, tail = run_suite(repo, python)
    if not ok:
        print("The unmutated suite is not green; fix that first.\n" + tail)
        shutil.rmtree(workdir, ignore_errors=True)
        return 2
    print("Baseline suite: green\n")

    survivors, caught = [], 0
    selected = [m for m in MUTATIONS if args.k in m[0]]
    try:
        for name, rel, old, new in selected:
            path = repo / rel
            original = path.read_text()
            if old not in original:
                print(f"[SKIP ] {name}: pattern not found in {rel} (update the mutation)")
                survivors.append(f"{name} (pattern missing)")
                continue
            t0 = time.time()
            try:
                path.write_text(original.replace(old, new, 1))
                passed, tail = run_suite(repo, python)
            finally:
                path.write_text(original)
            if passed:
                print(f"[SURVIVED] {name}  <-- the tests did NOT notice this bug")
                survivors.append(name)
            else:
                caught += 1
                print(f"[caught ] {name}  ({time.time() - t0:.0f}s)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n{caught}/{len(selected)} mutations caught" + (f"; survivors: {survivors}" if survivors else ""))
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
