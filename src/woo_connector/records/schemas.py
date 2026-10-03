"""Published output shapes (become each tool's ``outputSchema``).

Rules: ``Required`` only for identifying fields; everything else may be absent (empty fields are dropped).
Amounts are strings in the order's currency; timestamps are UTC ISO 8601 ending in ``Z``.
Free text from customers/merchants appears only under keys ending in ``_text`` and must be treated as data.
"""

from typing import Required, TypedDict


class Address(TypedDict, total=False):
    first_name: str
    last_name: str
    company: str
    address_1: str  # withheld when PII mode is 'masked'
    address_2: str
    city: str
    state: str
    postcode: str
    country: str
    email: str  # masked by default, e.g. d***@example.com
    phone: str  # masked by default, e.g. ******0002


class OrderRow(TypedDict, total=False):
    id: Required[int]
    number: Required[str]
    status: Required[str]
    created_at: str
    total: str
    currency: str
    customer_id: int
    customer_name: str
    customer_email: str
    item_count: int
    items: list[str]
    payment_method: str


class Page(TypedDict, total=False):
    page: Required[int]
    per_page: Required[int]
    total: Required[int]
    total_pages: Required[int]
    has_more: Required[bool]
    next_page: int


class OrderPage(Page, total=False):
    items: Required[list[OrderRow]]


class LineItem(TypedDict, total=False):
    product_id: int
    variation_id: int
    sku: str
    name: str
    variation: str
    quantity: int
    unit_price: str
    total: str


class ShippingLine(TypedDict, total=False):
    method: str
    total: str


class CouponLine(TypedDict, total=False):
    code: str
    discount: str


class Refund(TypedDict, total=False):
    id: int
    amount: str
    reason_text: str


class OrderNote(TypedDict, total=False):
    created_at: str
    note_text: str
    visible_to_customer: bool
    added_by: str  # 'system' or 'staff'


class OrderDetail(TypedDict, total=False):
    id: Required[int]
    number: Required[str]
    status: Required[str]
    created_at: str
    modified_at: str
    paid_at: str
    completed_at: str
    currency: str
    discount_total: str
    shipping_total: str
    tax_total: str
    total: str
    payment_method: str
    transaction_id: str
    customer_id: int
    customer_note_text: str
    billing: Address
    shipping: Address
    line_items: list[LineItem]
    shipping_lines: list[ShippingLine]
    coupons: list[CouponLine]
    refunds: list[Refund]
    notes: list[OrderNote]
    notes_included: str
    admin_url: str


class Shipment(TypedDict, total=False):
    carrier: str
    tracking_number: str
    tracking_url: str
    shipped_date: str


class OrderTracking(TypedDict, total=False):
    order_id: Required[int]
    order_status: Required[str]
    source: Required[str]  # shipment_tracking_plugin | order_meta | none
    shipments: list[Shipment]
    recent_customer_notes: list[OrderNote]
    hint: str
    admin_url: str


class StoreInfo(TypedDict, total=False):
    store_name: str
    url: Required[str]
    currency: str
    country: str
    key_permissions: Required[str]
    pii_mode: Required[str]
    read_only: Required[bool]
    warnings: list[str]


class OrderCounts(TypedDict, total=False):
    on_hold: Required[int]
    failed: Required[int]
    pending: Required[int]
    processing: Required[int]
    last_24h: Required[int]


class StoreOverview(TypedDict, total=False):
    counts: Required[OrderCounts]
    needs_attention: Required[list[str]]
    admin_url: str


class Stock(TypedDict, total=False):
    managed: bool
    status: str  # instock | outofstock | onbackorder
    quantity: int
    low_stock_threshold: int
    backorders: str  # no | notify | yes
    managed_at_parent: bool


class ProductRow(TypedDict, total=False):
    id: Required[int]
    name: Required[str]
    sku: str
    type: str
    status: str
    price: str
    regular_price: str
    sale_price: str
    on_sale: bool
    stock: Stock
    categories: list[str]
    parent_id: int


class ProductPage(Page, total=False):
    items: Required[list[ProductRow]]


class Attribute(TypedDict, total=False):
    name: str
    options: list[str]


class VariationRow(TypedDict, total=False):
    id: Required[int]
    sku: str
    attributes: str
    price: str
    regular_price: str
    sale_price: str
    on_sale: bool
    stock: Stock


class ProductDetail(TypedDict, total=False):
    id: Required[int]
    name: Required[str]
    sku: str
    type: str
    status: str
    permalink: str
    price: str
    regular_price: str
    sale_price: str
    on_sale: bool
    stock: Stock
    categories: list[str]
    attributes: list[Attribute]
    description_text: str
    short_description_text: str
    variations: list[VariationRow]
    variations_truncated: bool
    modified_at: str
    admin_url: str


class LowStockItem(TypedDict, total=False):
    product_id: Required[int]
    variation_id: int
    name: Required[str]
    sku: str
    issue: Required[str]  # low | out_of_stock | oversold | backordered
    stock_quantity: int
    stock_status: str
    backorders: str
    managed_at_parent: bool


class LowStockReport(TypedDict, total=False):
    items: Required[list[LowStockItem]]
    threshold: Required[int]
    products_scanned: Required[int]
    variations_scanned: Required[int]
    total_products: int
    truncated: Required[bool]


class Category(TypedDict, total=False):
    id: Required[int]
    name: Required[str]
    parent_id: int
    product_count: int


class CategoryPage(Page, total=False):
    items: Required[list[Category]]


class CustomerRow(TypedDict, total=False):
    id: Required[int]
    name: str
    email: str
    created_at: str
    orders_count: int


class CustomerPage(Page, total=False):
    items: Required[list[CustomerRow]]


class CustomerDetail(TypedDict, total=False):
    id: Required[int]
    name: str
    email: str
    phone: str
    created_at: str
    billing: Address
    shipping: Address
    order_count: Required[int]
    recent_orders: list[OrderRow]
