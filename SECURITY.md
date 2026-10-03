# Security

## Threats and controls

| Threat | Control | Evidence |
|---|---|---|
| Prompt injection via customer notes, order notes, product text | Text stripped of HTML, truncated to 500 chars, returned only under `*_text` keys; server instructions say never to follow it | `test_untrusted_text_only_under_text_keys`, `demo.py` |
| Personal data sent to an LLM provider | Emails/phones masked, street lines withheld by default (`WOO_PII_MODE=masked`); `full` is opt-in | `test_masked_detail_withholds_street_and_masks_contact` |
| Key theft from disk | Credentials file `0600` in a `0700` directory, atomic write; secrets never in `repr` or logs | `test_credentials_file_is_private_atomic_and_multi_store` |
| Key does more than intended | Requested with `scope=read`; the connector only sends GET; the store rejects writes with 401 | `test_the_connector_never_writes`, `demo.py` |
| CSRF / key injection on the consent callback | 32-char random `user_id` used as state; wrong state -> 403 and key discarded; key saved only after the browser returns with `success=1` | `tests/test_connect.py` |
| Open port | stdio by default (no network surface); HTTP binds `127.0.0.1` and requires a bearer token (constant-time compare); websockets refused; `/healthz` reveals only status+version | `tests/test_bearer.py` |
| SSRF / credential forwarding via redirects | `follow_redirects=False`; any 3xx is an error | `test_redirect_is_refused_and_credentials_do_not_follow` |
| Overloading the merchant's server | Token bucket (default 4 req/s), concurrency cap 4, bounded scans, `Retry-After`, adaptive slow-down on 429 | `tests/test_client.py` |
| Secrets in logs | Logging to stderr only; httpx/httpcore forced to WARNING because OAuth1 URLs contain the consumer key | module-level in `client.py` |
| Web UI: password guessing | Lockout after 5 failures / 5 min per IP; constant-time compare | `test_brute_force_is_locked_out` |
| Web UI: cross-site requests | SameSite=Strict cookie + required `X-Requested-With` header on POST | `test_post_without_the_csrf_header_is_refused` |
| Web UI: stolen/forged cookie | HMAC-signed, expiring, revocable on sign-out | `test_tampered_or_expired_cookies_are_rejected`, `test_logout_revokes_...` |
| Web UI: XSS from customer-written text | Strict CSP, no inline script, text inserted with `textContent` only; no `innerHTML` anywhere (tested) | `test_pages_have_no_inline_script_or_handlers`, `scripts/ui_smoke.py` |
| Another store's data leaking via ID guessing | Customer tool returns only `role == customer`; other IDs are "not found" | `test_get_customer_rejects_admins_as_not_found` |

## Residual risks
- **WooCommerce keys never expire.** Revoke them in WooCommerce > Settings > Advanced > REST API; `woo-connector forget` only deletes the local copy.
- **Plain-HTTP stores** use OAuth 1.0a in the query string: the request is signed, but contents are not encrypted. Use HTTPS.
- **Binding the HTTP transport to `0.0.0.0`** (the Docker default) disables the SDK's Host-header (DNS-rebinding) check; the bearer token is then the only control. Keep it on a private network and use a long random `WOO_MCP_TOKEN`.
- **`WOO_VERIFY_TLS=false`** disables certificate checking; development only.
- **Masking is not anonymisation.** Names, cities and postcodes are still returned.
- **The mutation and fake-store tests are written by the same author as the code**; they are not a substitute for testing against your own store.

- **Web UI** uses one shared password, in-memory sessions and plain HTTP unless you add HTTPS in front. Login lockout is per source IP and is weaker behind a proxy that hides client addresses.

## Reporting
Open a private security advisory on the repository, or email abinesh0309@gmail.com. Please do not post keys or customer data.
