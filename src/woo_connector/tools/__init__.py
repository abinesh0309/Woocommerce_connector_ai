"""All tools, in the order they are registered. Every one is read-only."""

from woo_connector.tools.customers import get_customer, list_customers, search_customers
from woo_connector.tools.orders import get_order, get_order_tracking, list_orders, search_orders
from woo_connector.tools.products import get_product, list_low_stock, list_product_categories, list_products, search_products
from woo_connector.tools.store import get_store_info, get_store_overview

ALL_TOOLS = [
    get_store_info,
    get_store_overview,
    list_orders,
    search_orders,
    get_order,
    get_order_tracking,
    list_products,
    search_products,
    get_product,
    list_low_stock,
    list_product_categories,
    list_customers,
    search_customers,
    get_customer,
]

__all__ = ["ALL_TOOLS"]
