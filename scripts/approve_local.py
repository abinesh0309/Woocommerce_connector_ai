#!/usr/bin/env python3
"""Approve a WooCommerce consent URL headlessly against the DEV store (logs in as admin/admin).

    uv run woo-connector --store http://localhost:8080 auth --no-browser --callback-host host.docker.internal &
    uv run python scripts/approve_local.py '<the URL it printed>'

Best effort: it logs in at /wp-login.php, opens the consent page and submits its approve form.
Use a real browser if WooCommerce's markup differs. Dev store only: never point this at a real site.
"""

import re
import sys
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx


def main(url: str) -> int:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if parts.hostname not in ("localhost", "127.0.0.1"):
        print("Refusing: this helper only talks to a local dev store.")
        return 2
    with httpx.Client(follow_redirects=True, timeout=30) as http:
        http.get(f"{base}/wp-login.php")  # sets the test cookie
        r = http.post(
            f"{base}/wp-login.php", data={"log": "admin", "pwd": "admin", "wp-submit": "Log In", "redirect_to": f"{base}/wp-admin/", "testcookie": "1"}
        )
        if "wp-admin" not in str(r.url) and "Dashboard" not in r.text:
            print("Login failed (is the dev store seeded with admin/admin?)")
            return 1
        page = http.get(url)
        form = re.search(r"<form[^>]*>(.*?)</form>", page.text, re.S)
        if not form:
            print("No consent form found on the page; approve in a browser instead.")
            return 1
        action = re.search(r'<form[^>]*action="([^"]*)"', page.text)
        fields = {unescape(n): unescape(v) for n, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(1))}
        post = http.post(urljoin(url, unescape(action.group(1)) if action else url), data=fields)
        print(f"Submitted the consent form (HTTP {post.status_code}). Check the terminal running `woo-connector auth`.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]) if len(sys.argv) > 1 else print(__doc__) or 2)
