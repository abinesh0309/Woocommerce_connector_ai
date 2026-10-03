# Changelog

## 0.2.0
- `woo-connector web`: password-protected dashboard (overview, orders, stock, customers) with a docked AI chat, same read-only tools.
- Windows fix: credential file write no longer needs `os.fchmod`.
- `OPENROUTER_URL` override.
- Added author metadata and an MIT LICENSE file.

## 0.1.0
- Read-only WooCommerce MCP server with 14 tools (orders, tracking, products, stock, customers, store overview).
- Consent flow (`woo-connector auth`) and manual API-key path (`auth --manual`).
- Throttled, bounded HTTP client: token bucket, concurrency cap, `Retry-After`, adaptive slow-down on 429, time budget, no redirects.
- PII masking by default; untrusted text sanitised into `*_text` fields.
- Streamable HTTP transport with bearer guard; stdio by default.
- Optional OpenRouter reference agent (`woo-connector chat`).
