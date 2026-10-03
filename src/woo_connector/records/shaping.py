"""Turn raw WooCommerce JSON into compact, model-friendly records.

Rules: drop empty fields; money as strings with the order's decimals; UTC timestamps ending in ``Z``;
drop ``_links``/``meta_data``/tax arrays/payment tokens; mask PII by default; sanitise untrusted text.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from typing import Any

TEXT_LIMIT = 500
_SKIP_CONTENT = {"script", "style"}


# ------------------------------------------------------------------ generic helpers


def drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Remove None, empty strings and empty containers. Keeps 0 and False (they carry meaning)."""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT:
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def clean_text(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    """Untrusted text -> plain text: strip HTML, collapse whitespace, truncate. Returns None if empty."""
    if value is None:
        return None
    raw = str(value)
    if "<" in raw:
        stripper = _Stripper()
        stripper.feed(raw)
        stripper.close()
        raw = "".join(stripper.parts)
    raw = unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    return raw if len(raw) <= limit else raw[: limit - 1].rstrip() + "…"


def gmt(value: Any) -> str | None:
    """``2026-09-01T10:00:00`` (WooCommerce *_gmt) -> ``2026-09-01T10:00:00Z``."""
    if not value:
        return None
    s = str(value)
    return s if s.endswith("Z") or "+" in s[10:] else s + "Z"


def decimals_of(amount: Any, default: int = 2) -> int:
    """Decimal places the *store* used for this amount ("41.10" -> 2, "4100" -> 0 for zero-decimal currencies like JPY)."""
    s = str(amount).strip() if amount is not None else ""
    if not s:
        return default
    frac = s.split(".", 1)[1] if "." in s else ""
    return len(frac) if frac.isdigit() or not frac else default


def money(value: Any, dp: int = 2) -> str | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return str(d.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------ PII


def mask_email(email: Any) -> str | None:
    if not email or "@" not in str(email):
        return None
    local, _, domain = str(email).partition("@")
    return f"{local[:1]}***@{domain}"


def mask_phone(phone: Any) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if not digits:
        return None
    if len(digits) <= 4:
        return "****"
    return "*" * (len(digits) - 4) + digits[-4:]


def address(raw: dict | None, pii: str) -> dict[str, Any] | None:
    if not raw:
        return None
    full = pii == "full"
    out = {
        "first_name": raw.get("first_name"),
        "last_name": raw.get("last_name"),
        "company": raw.get("company") if full else None,
        "address_1": raw.get("address_1") if full else None,
        "address_2": raw.get("address_2") if full else None,
        "city": raw.get("city"),
        "state": raw.get("state"),
        "postcode": raw.get("postcode"),
        "country": raw.get("country"),
        "email": (raw.get("email") if full else mask_email(raw.get("email"))),
        "phone": (raw.get("phone") if full else mask_phone(raw.get("phone"))),
    }
    return drop_empty(out) or None


def person_name(*addresses: dict | None) -> str | None:
    for a in addresses:
        if a:
            name = f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
            if name:
                return name
    return None


def email_for(raw: Any, pii: str) -> str | None:
    return (raw or None) if pii == "full" else mask_email(raw)


# ------------------------------------------------------------------ links


def order_admin_url(store_url: str, order_id: int) -> str:
    return f"{store_url}/wp-admin/admin.php?page=wc-orders&action=edit&id={order_id}"


def orders_admin_url(store_url: str) -> str:
    return f"{store_url}/wp-admin/admin.php?page=wc-orders"


def product_admin_url(store_url: str, product_id: int) -> str:
    return f"{store_url}/wp-admin/post.php?post={product_id}&action=edit"


# ------------------------------------------------------------------ orders


def _items_summary(line_items: list[dict], max_items: int = 5) -> list[str]:
    rows = [f"{li.get('quantity', 1)} x {clean_text(li.get('name'), 60) or 'item'}" for li in line_items[:max_items]]
    if len(line_items) > max_items:
        rows.append(f"+{len(line_items) - max_items} more")
    return rows


def order_row(o: dict, pii: str) -> dict[str, Any]:
    billing = o.get("billing") or {}
    return drop_empty(
        {
            "id": o["id"],
            "number": str(o.get("number") or o["id"]),
            "status": o.get("status"),
            "created_at": gmt(o.get("date_created_gmt")),
            "total": money(o.get("total"), decimals_of(o.get("total"))),
            "currency": o.get("currency"),
            "customer_id": o.get("customer_id") or None,
            "customer_name": person_name(billing, o.get("shipping")),
            "customer_email": email_for(billing.get("email"), pii),
            "item_count": sum(int(li.get("quantity", 0) or 0) for li in o.get("line_items", [])) or None,
            "items": _items_summary(o.get("line_items", [])),
            "payment_method": clean_text(o.get("payment_method_title"), 60),
        }
    )


def _variation_label(li: dict) -> str | None:
    parts = []
    for m in li.get("meta_data", []) or []:
        key = str(m.get("display_key") or m.get("key") or "")
        if not key or key.startswith("_"):
            continue
        val = m.get("display_value", m.get("value"))
        if isinstance(val, (dict, list)):
            continue
        parts.append(f"{clean_text(key, 40)}: {clean_text(val, 60)}")
    return ", ".join(parts) or None


def order_detail(o: dict, pii: str, store_url: str, notes: list[dict] | None, notes_mode: str) -> dict[str, Any]:
    dp = decimals_of(o.get("total"))
    return drop_empty(
        {
            "id": o["id"],
            "number": str(o.get("number") or o["id"]),
            "status": o.get("status"),
            "created_at": gmt(o.get("date_created_gmt")),
            "modified_at": gmt(o.get("date_modified_gmt")),
            "paid_at": gmt(o.get("date_paid_gmt")),
            "completed_at": gmt(o.get("date_completed_gmt")),
            "currency": o.get("currency"),
            "discount_total": money(o.get("discount_total"), dp),
            "shipping_total": money(o.get("shipping_total"), dp),
            "tax_total": money(o.get("total_tax"), dp),
            "total": money(o.get("total"), dp),
            "payment_method": clean_text(o.get("payment_method_title"), 60),
            "transaction_id": clean_text(o.get("transaction_id"), 80),
            "customer_id": o.get("customer_id") or None,
            "customer_note_text": clean_text(o.get("customer_note")),
            "billing": address(o.get("billing"), pii),
            "shipping": address(o.get("shipping"), pii),
            "line_items": [
                drop_empty(
                    {
                        "product_id": li.get("product_id") or None,
                        "variation_id": li.get("variation_id") or None,
                        "sku": li.get("sku"),
                        "name": clean_text(li.get("name"), 120),
                        "variation": _variation_label(li),
                        "quantity": li.get("quantity"),
                        "unit_price": money(li.get("price"), dp),
                        "total": money(li.get("total"), dp),
                    }
                )
                for li in o.get("line_items", [])
            ],
            "shipping_lines": [
                drop_empty({"method": clean_text(s.get("method_title"), 80), "total": money(s.get("total"), dp)}) for s in o.get("shipping_lines", [])
            ],
            "coupons": [drop_empty({"code": c.get("code"), "discount": money(c.get("discount"), dp)}) for c in o.get("coupon_lines", [])],
            "refunds": [
                drop_empty({"id": r.get("id"), "amount": money(abs(Decimal(str(r.get("total", 0) or 0))), dp), "reason_text": clean_text(r.get("reason"))})
                for r in o.get("refunds", [])
            ],
            "notes": [order_note(n) for n in notes] if notes is not None else None,
            "notes_included": notes_mode,
            "admin_url": order_admin_url(store_url, o["id"]),
        }
    )


def order_note(n: dict) -> dict[str, Any]:
    return drop_empty(
        {
            "created_at": gmt(n.get("date_created_gmt")),
            "note_text": clean_text(n.get("note")),
            "visible_to_customer": bool(n.get("customer_note")),
            "added_by": "system" if str(n.get("author", "")).lower() == "system" else "staff",
        }
    )


# ------------------------------------------------------------------ tracking

_NUMBER_KEYS = {
    "tracking_number",
    "tracking_no",
    "tracking_id",
    "awb",
    "awb_number",
    "awb_code",
    "shiprocket_awb_code",
    "consignment_number",
    "waybill",
    "waybill_number",
}
_CARRIER_KEYS = {"tracking_provider", "tracking_carrier", "carrier", "courier", "courier_name", "shipping_carrier", "shipping_provider", "shipment_provider"}
_LINK_KEYS = {"tracking_url", "tracking_link", "custom_tracking_link", "track_url", "tracking_page"}
_DATE_KEYS = {"date_shipped", "shipped_date", "shipment_date"}


def _norm_key(key: Any) -> str:
    return str(key).lower().lstrip("_").replace("-", "_")


def shipment(item: dict) -> dict[str, Any]:
    carrier = item.get("tracking_provider") or item.get("custom_tracking_provider") or item.get("carrier")
    return drop_empty(
        {
            "carrier": clean_text(carrier, 80),
            "tracking_number": clean_text(item.get("tracking_number"), 80),
            "tracking_url": _safe_url(item.get("tracking_link") or item.get("custom_tracking_link") or item.get("tracking_url")),
            "shipped_date": _date(item.get("date_shipped")),
        }
    )


def _safe_url(value: Any) -> str | None:
    s = str(value or "").strip()
    return s if s.lower().startswith(("http://", "https://")) and len(s) < 500 else None


def _date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        from datetime import UTC, datetime

        try:
            return datetime.fromtimestamp(int(value), UTC).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return clean_text(value, 40)


def tracking_from_meta(meta: list[dict]) -> list[dict[str, Any]]:
    """Find shipment info in order ``meta_data`` (Shiprocket, AfterShip-style, custom fields, plugin items)."""
    shipments: list[dict[str, Any]] = []
    flat: dict[str, Any] = {}
    for m in meta or []:
        key, value = _norm_key(m.get("key", "")), m.get("value")
        if key == "wc_shipment_tracking_items" and isinstance(value, list):
            shipments.extend(s for s in (shipment(i) for i in value if isinstance(i, dict)) if s.get("tracking_number"))
        elif isinstance(value, (str, int, float)):
            flat[key] = value
    number = next((flat[k] for k in _NUMBER_KEYS if flat.get(k) not in (None, "")), None)
    if number is not None and not any(s.get("tracking_number") == str(number) for s in shipments):
        shipments.append(
            drop_empty(
                {
                    "carrier": clean_text(next((flat[k] for k in _CARRIER_KEYS if flat.get(k)), None), 80),
                    "tracking_number": clean_text(number, 80),
                    "tracking_url": _safe_url(next((flat[k] for k in _LINK_KEYS if flat.get(k)), None)),
                    "shipped_date": _date(next((flat[k] for k in _DATE_KEYS if flat.get(k)), None)),
                }
            )
        )
    return shipments


# ------------------------------------------------------------------ products


def stock(p: dict) -> dict[str, Any]:
    ms = p.get("manage_stock")
    qty = p.get("stock_quantity")
    return drop_empty(
        {
            "managed": ms is True,
            "status": p.get("stock_status"),
            "quantity": qty if isinstance(qty, int) and not isinstance(qty, bool) else None,
            "low_stock_threshold": p.get("low_stock_amount"),
            "backorders": p.get("backorders"),
            "managed_at_parent": True if ms == "parent" else None,
        }
    )


def _attr_label(attrs: list[dict]) -> str | None:
    parts = [f"{clean_text(a.get('name'), 40)}: {clean_text(a.get('option'), 60)}" for a in attrs or [] if a.get("option")]
    return ", ".join(parts) or None


def _prices(p: dict) -> dict[str, Any]:
    return {
        "price": money(p.get("price"), decimals_of(p.get("price"))) if p.get("price") not in (None, "") else None,
        "regular_price": p.get("regular_price") or None,
        "sale_price": p.get("sale_price") or None,
        "on_sale": p.get("on_sale"),
    }


def product_row(p: dict) -> dict[str, Any]:
    is_variation = p.get("type") == "variation" or bool(p.get("parent_id"))
    return drop_empty(
        {
            "id": p["id"],
            "name": clean_text(p.get("name"), 160) or "(unnamed)",
            "sku": p.get("sku"),
            "type": p.get("type"),
            "status": p.get("status"),
            **_prices(p),
            "stock": stock(p),
            "categories": [clean_text(c.get("name"), 60) for c in p.get("categories", []) if c.get("name")],
            "parent_id": p.get("parent_id") if is_variation else None,
        }
    )


def variation_row(v: dict) -> dict[str, Any]:
    return drop_empty({"id": v["id"], "sku": v.get("sku"), "attributes": _attr_label(v.get("attributes", [])), **_prices(v), "stock": stock(v)})


def product_detail(p: dict, variations: list[dict] | None, variations_truncated: bool, store_url: str) -> dict[str, Any]:
    return drop_empty(
        {
            "id": p["id"],
            "name": clean_text(p.get("name"), 160) or "(unnamed)",
            "sku": p.get("sku"),
            "type": p.get("type"),
            "status": p.get("status"),
            "permalink": _safe_url(p.get("permalink")),
            **_prices(p),
            "stock": stock(p),
            "categories": [clean_text(c.get("name"), 60) for c in p.get("categories", []) if c.get("name")],
            "attributes": [
                drop_empty({"name": clean_text(a.get("name"), 60), "options": [clean_text(o, 60) for o in a.get("options", [])][:30]})
                for a in p.get("attributes", [])
            ],
            "description_text": clean_text(p.get("description")),
            "short_description_text": clean_text(p.get("short_description")),
            "variations": [variation_row(v) for v in variations] if variations else None,
            "variations_truncated": True if variations_truncated else None,
            "modified_at": gmt(p.get("date_modified_gmt")),
            "admin_url": product_admin_url(store_url, p["id"]),
        }
    )


def category(c: dict) -> dict[str, Any]:
    return drop_empty(
        {"id": c["id"], "name": clean_text(c.get("name"), 80) or "(unnamed)", "parent_id": c.get("parent") or None, "product_count": c.get("count")}
    )


# ------------------------------------------------------------------ customers


def customer_row(c: dict, pii: str) -> dict[str, Any]:
    name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or person_name(c.get("billing"))
    return drop_empty(
        {
            "id": c["id"],
            "name": name,
            "email": email_for(c.get("email"), pii),
            "created_at": gmt(c.get("date_created_gmt")),
            "orders_count": c.get("orders_count"),
        }
    )


def customer_detail(c: dict, pii: str, recent_orders: list[dict], order_count: int) -> dict[str, Any]:
    billing = c.get("billing") or {}
    return drop_empty(
        {
            "id": c["id"],
            "name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or person_name(billing),
            "email": email_for(c.get("email"), pii),
            "phone": (billing.get("phone") or None) if pii == "full" else mask_phone(billing.get("phone")),
            "created_at": gmt(c.get("date_created_gmt")),
            "billing": address(billing, pii),
            "shipping": address(c.get("shipping"), pii),
            "order_count": order_count,
            "recent_orders": [order_row(o, pii) for o in recent_orders],
        }
    )
