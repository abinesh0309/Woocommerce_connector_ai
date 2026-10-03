from datetime import UTC, datetime, timedelta
from typing import Any

from woo_connector.records import shaping
from woo_connector.records.schemas import StoreInfo, StoreOverview
from woo_connector.tools.common import gather_strict, guarded, runtime
from woo_connector.woocommerce.errors import BadRequest, NotFound


@guarded
async def get_store_info() -> StoreInfo:
    """Describe the connected store: name, currency, country, what the API key may do, and the PII mode.

    Call this first when unsure which store you are talking to or whether amounts are in the expected currency.
    This connector is read-only. Never invent store details that are not returned here.
    """
    c = await runtime.client()
    root = await c.get_json("")
    warnings: list[str] = []
    currency = country = None
    try:
        settings = await c.get_json("wc/v3/settings/general")
        values = {s.get("id"): s.get("value") for s in settings if isinstance(s, dict)}
        currency = values.get("woocommerce_currency")
        country = str(values.get("woocommerce_default_country") or "").split(":")[0] or None
    except (NotFound, BadRequest):
        warnings.append("Could not read store settings (currency/country); the key owner may lack the Shop manager or Administrator role.")
    perms = c.creds.key_permissions
    if perms == "unknown":
        warnings.append("Key permissions are unknown (a pasted key). Confirm it is Read-only in WooCommerce > Settings > Advanced > REST API.")
    elif perms != "read":
        warnings.append(f"This key has '{perms}' permissions. The connector only reads, but a Read-only key is safer.")
    return shaping.drop_empty(
        {
            "store_name": shaping.clean_text(root.get("name") if isinstance(root, dict) else None, 120),
            "url": c.store_url,
            "currency": currency,
            "country": country,
            "key_permissions": perms,
            "pii_mode": runtime.pii,
            "read_only": True,
            "warnings": warnings,
        }
    )  # type: ignore[return-value]


@guarded
async def get_store_overview() -> StoreOverview:
    """What needs attention right now: counts of on-hold, failed, pending and processing orders, plus orders in the last 24 hours.

    Uses five cheap count queries. Use `list_orders` with a status filter to see the orders behind a number.
    """
    c = await runtime.client()
    since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

    async def count(**params: Any) -> int:
        resp = await c.get("wc/v3/orders", {"per_page": 1, **params})
        try:
            return int(resp.headers.get("X-WP-Total", len(resp.data) if isinstance(resp.data, list) else 0))
        except ValueError:
            return 0

    on_hold, failed, pending, processing, last_24h = await gather_strict(
        count(status="on-hold"),
        count(status="failed"),
        count(status="pending"),
        count(status="processing"),
        count(after=since, dates_are_gmt=True),
    )
    attention: list[str] = []
    if on_hold:
        attention.append(f"{on_hold} order(s) are on hold (often awaiting payment confirmation, a bank transfer or a stock check).")
    if failed:
        attention.append(f"{failed} order(s) have failed payment.")
    if pending:
        attention.append(f"{pending} order(s) are pending payment.")
    if processing:
        attention.append(f"{processing} order(s) are paid and waiting to be fulfilled.")
    if not attention:
        attention.append("Nothing needs attention: no on-hold, failed, pending or processing orders.")
    return {
        "counts": {"on_hold": on_hold, "failed": failed, "pending": pending, "processing": processing, "last_24h": last_24h},
        "needs_attention": attention,
        "admin_url": shaping.orders_admin_url(c.store_url),
    }
