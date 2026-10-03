import re
from typing import Annotated

from pydantic import Field

from woo_connector.records import shaping
from woo_connector.records.schemas import CustomerDetail, CustomerPage
from woo_connector.tools.common import PageNo, PerPage, gather_strict, guarded, not_found, page_out, runtime
from woo_connector.woocommerce.errors import NotFound

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIND_HINT = "Use search_customers to find the customer by email or name, or search_orders if they checked out as a guest."


def _shoppers(items: list[dict]) -> list[dict]:
    # WooCommerce's customers endpoint can surface other WordPress users (shop owner, staff). Shoppers only.
    return [c for c in items if c.get("role") == "customer"]


@guarded
async def list_customers(page: PageNo = 1, per_page: PerPage = 20) -> CustomerPage:
    """List registered shoppers (role 'customer'), newest first. Staff and admin accounts are never shown.

    Guest checkouts have no customer record: find those with search_orders.
    """
    c = await runtime.client()
    result = await c.get_page("wc/v3/customers", {"role": "customer", "orderby": "registered_date", "order": "desc"}, page=page, per_page=per_page)
    return page_out(result, [shaping.customer_row(x, runtime.pii) for x in _shoppers(result.items)])  # type: ignore[return-value]


@guarded
async def search_customers(
    query: Annotated[str, Field(min_length=1, max_length=200, description="An exact email address, or a name.")],
    page: PageNo = 1,
    per_page: PerPage = 20,
) -> CustomerPage:
    """Find registered shoppers by exact email (if the query looks like one) or by name. No fuzzy matching; shoppers only.

    Customers who checked out as guests are not here: use search_orders with their email instead.
    """
    c = await runtime.client()
    q = query.strip()
    params = {"role": "customer", **({"email": q} if _EMAIL.match(q) else {"search": q})}
    result = await c.get_page("wc/v3/customers", params, page=page, per_page=per_page)
    return page_out(result, [shaping.customer_row(x, runtime.pii) for x in _shoppers(result.items)])  # type: ignore[return-value]


@guarded
async def get_customer(
    customer_id: Annotated[int, Field(ge=1, description="WooCommerce customer ID from search_customers or an order's customer_id.")],
) -> CustomerDetail:
    """Customer profile (masked by default), their order count and their five most recent orders.

    Only registered shoppers exist here; any other ID is reported as not found.
    """
    c = await runtime.client()
    try:
        customer, orders = await gather_strict(
            c.get_json(f"wc/v3/customers/{customer_id}"),
            c.get_page("wc/v3/orders", {"customer": customer_id, "orderby": "date", "order": "desc"}, page=1, per_page=5),
        )
    except NotFound as exc:
        if exc.route_missing:
            raise
        not_found("Customer", customer_id, FIND_HINT)
    if not isinstance(customer, dict) or customer.get("role") != "customer":
        not_found("Customer", customer_id, FIND_HINT)
    return shaping.customer_detail(customer, runtime.pii, orders.items, orders.total)  # type: ignore[return-value]
