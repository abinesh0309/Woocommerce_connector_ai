import asyncio

import httpx
import pytest
import respx

from woo_connector.auth.credentials import Credentials
from woo_connector.tools import common
from woo_connector.woocommerce.client import ClientSettings, WooClient

STORE = "https://shop.test"
API = f"{STORE}/wp-json"


class FakeClock:
    """Monotonic clock + sleep that advances it. Concurrent sleepers share time (like real sleeping)."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        target = self.t + seconds
        await asyncio.sleep(0)
        self.t = max(self.t, target)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


def make_client(clock: FakeClock, store: str = STORE, **overrides) -> WooClient:
    settings = ClientSettings(max_rps=1000, burst=1000, **overrides)
    creds = Credentials(store, "ck_test", "cs_secret", "read")
    return WooClient(creds, settings, clock=clock, sleep=clock.sleep, jitter=lambda: 1.0)


@pytest.fixture
def client(clock, router):
    return make_client(clock)


@pytest.fixture
def tools_ready(client):
    """Install the client + masked PII into the tool runtime; reset after."""
    common.configure(client=client, store_url=STORE, pii_mode="masked")
    yield client
    common.configure()


def page_response(items, total=None, pages=1):
    return httpx.Response(200, json=items, headers={"X-WP-Total": str(len(items) if total is None else total), "X-WP-TotalPages": str(pages)})


# ---------------------------------------------------------------- WooCommerce-shaped sample data


def order(id=1042, status="processing", email="dana@example.com", phone="0000000002", **kw):
    base = {
        "id": id,
        "number": str(id),
        "status": status,
        "currency": "USD",
        "date_created_gmt": "2026-09-01T10:00:00",
        "date_modified_gmt": "2026-09-01T11:00:00",
        "date_paid_gmt": "2026-09-01T10:05:00",
        "discount_total": "5.00",
        "shipping_total": "4.00",
        "total_tax": "2.10",
        "total": "41.10",
        "customer_id": 7,
        "customer_note": "<p>Please leave at the <b>door</b>.</p>",
        "payment_method_title": "Credit card",
        "transaction_id": "ch_123",
        "billing": {
            "first_name": "Dana",
            "last_name": "Lee",
            "address_1": "12 Secret Street",
            "city": "Austin",
            "state": "TX",
            "postcode": "73301",
            "country": "US",
            "email": email,
            "phone": phone,
        },
        "shipping": {
            "first_name": "Dana",
            "last_name": "Lee",
            "address_1": "12 Secret Street",
            "city": "Austin",
            "state": "TX",
            "postcode": "73301",
            "country": "US",
        },
        "line_items": [
            {"product_id": 11, "variation_id": 0, "sku": "TEE-1", "name": "Blue T-shirt", "quantity": 2, "price": 18.5, "total": "37.00", "meta_data": []},
            {
                "product_id": 12,
                "variation_id": 121,
                "sku": "HAT-M",
                "name": "Hat - M",
                "quantity": 1,
                "price": 9,
                "total": "9.00",
                "meta_data": [{"key": "pa_size", "display_key": "Size", "display_value": "M"}],
            },
        ],
        "shipping_lines": [{"method_title": "Flat rate", "total": "4.00"}],
        "coupon_lines": [{"code": "SAVE5", "discount": "5.00"}],
        "refunds": [],
        "meta_data": [{"id": 1, "key": "_secret_gateway_token", "value": "tok_live_zzz"}],
        "_links": {"self": [{"href": "x"}]},
    }
    base.update(kw)
    return base


def product(id=11, **kw):
    base = {
        "id": id,
        "name": "Blue T-shirt",
        "sku": "TEE-1",
        "type": "simple",
        "status": "publish",
        "price": "18.50",
        "regular_price": "18.50",
        "sale_price": "",
        "on_sale": False,
        "manage_stock": True,
        "stock_quantity": 10,
        "stock_status": "instock",
        "backorders": "no",
        "categories": [{"id": 3, "name": "Shirts"}],
        "attributes": [],
        "description": "<p>Soft cotton.</p>",
        "short_description": "",
        "permalink": "https://shop.test/p/blue",
        "date_modified_gmt": "2026-09-02T00:00:00",
    }
    base.update(kw)
    return base


def customer(id=7, **kw):
    base = {
        "id": id,
        "role": "customer",
        "email": "dana@example.com",
        "first_name": "Dana",
        "last_name": "Lee",
        "date_created_gmt": "2026-01-01T00:00:00",
        "billing": {
            "first_name": "Dana",
            "last_name": "Lee",
            "address_1": "12 Secret Street",
            "city": "Austin",
            "state": "TX",
            "postcode": "73301",
            "country": "US",
            "phone": "0000000002",
            "email": "dana@example.com",
        },
        "shipping": {},
        "orders_count": 3,
    }
    base.update(kw)
    return base
