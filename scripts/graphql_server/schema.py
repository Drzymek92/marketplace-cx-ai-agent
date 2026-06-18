"""GraphQL schema (Strawberry) for an Allegro-style marketplace.

Production patterns on show here:
- Enums for state (OrderStatus / ReturnStatus / DeliveryMethod) instead of free-text strings.
- A Money value type (Decimal amount + currency) — money is never a float.
- DateTime scalar (ISO 8601) for timestamps.
- A Node interface + node(id:) root field for global object identification (Relay).
- Relay cursor connection for orders (stable keyset cursor) — no unbounded lists.
- Lazy relationship resolution via per-request DataLoaders (loaders.py) — no N+1. Types hold their
  own columns plus foreign-key ids (strawberry.Private) and resolve relations through loaders.

Strawberry maps snake_case fields to camelCase in the API (line_items -> lineItems, etc.).
"""

import base64
import json
from decimal import Decimal
from datetime import datetime
from enum import Enum
from typing import Optional

import strawberry
from strawberry.extensions import QueryDepthLimiter

from scripts.graphql_server import db

MAX_PAGE_SIZE = 100
_DELIVERY_BASE = {"COURIER": "12.99", "PARCEL_LOCKER": "9.99", "PICKUP_POINT": "6.99"}


# --- enums -----------------------------------------------------------------
@strawberry.enum
class OrderStatus(Enum):
    NEW = "NEW"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


@strawberry.enum
class ReturnStatus(Enum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"


@strawberry.enum
class DeliveryMethod(Enum):
    COURIER = "COURIER"
    PARCEL_LOCKER = "PARCEL_LOCKER"
    PICKUP_POINT = "PICKUP_POINT"


# --- value types -----------------------------------------------------------
@strawberry.type
class Money:
    amount: Decimal
    currency: str = "PLN"


@strawberry.interface
class Node:
    id: strawberry.ID


# --- leaf domain types -----------------------------------------------------
@strawberry.type
class Seller(Node):
    name: str
    rating: float


@strawberry.type
class Buyer(Node):
    login: str
    smart: bool
    locale: str


@strawberry.type
class Offer(Node):
    name: str
    category: str
    price: Money
    seller_id: strawberry.Private[str]

    @strawberry.field
    async def seller(self, info: strawberry.Info) -> Optional[Seller]:
        return _seller(await info.context["loaders"]["seller"].load(self.seller_id))


@strawberry.type
class LineItem:
    quantity: int
    unit_price: Money
    offer_id: strawberry.Private[str]

    @strawberry.field
    async def offer(self, info: strawberry.Info) -> Optional[Offer]:
        return _offer(await info.context["loaders"]["offer"].load(self.offer_id))

    @strawberry.field
    def total_price(self) -> Money:
        return Money(amount=self.unit_price.amount * self.quantity)


@strawberry.type
class Delivery:
    method: DeliveryMethod
    cost: Money


@strawberry.type
class Order(Node):
    status: OrderStatus
    placed_at: datetime
    buyer_id: strawberry.Private[str]
    seller_id: strawberry.Private[str]
    delivery_method: strawberry.Private[str]

    @strawberry.field
    async def buyer(self, info: strawberry.Info) -> Optional[Buyer]:
        return _buyer(await info.context["loaders"]["buyer"].load(self.buyer_id))

    @strawberry.field
    async def seller(self, info: strawberry.Info) -> Optional[Seller]:
        return _seller(await info.context["loaders"]["seller"].load(self.seller_id))

    @strawberry.field
    async def line_items(self, info: strawberry.Info) -> list[LineItem]:
        rows = await info.context["loaders"]["order_items"].load(self.id)
        return [_line_item(r) for r in rows]

    @strawberry.field
    async def delivery(self, info: strawberry.Info) -> Delivery:
        buyer = await info.context["loaders"]["buyer"].load(self.buyer_id)
        cost = _delivery_cost(self.delivery_method, bool(buyer and buyer["smart"]))
        return Delivery(method=DeliveryMethod(self.delivery_method), cost=Money(amount=cost))

    @strawberry.field
    async def total(self, info: strawberry.Info) -> Money:
        loaders = info.context["loaders"]
        items = await loaders["order_items"].load(self.id)
        buyer = await loaders["buyer"].load(self.buyer_id)
        items_total = sum((Decimal(str(i["unit_price"])) * i["quantity"] for i in items), Decimal("0.00"))
        return Money(amount=items_total + _delivery_cost(self.delivery_method, bool(buyer and buyer["smart"])))


@strawberry.type
class CustomerReturn(Node):
    order_id: strawberry.ID
    reason: str
    status: ReturnStatus
    opened_at: datetime


# --- Relay connection for orders -------------------------------------------
@strawberry.type
class PageInfo:
    has_next_page: bool
    has_previous_page: bool
    start_cursor: Optional[str]
    end_cursor: Optional[str]


@strawberry.type
class OrderEdge:
    node: Order
    cursor: str


@strawberry.type
class OrderConnection:
    edges: list[OrderEdge]
    page_info: PageInfo
    total_count: int


def _encode_cursor(placed_at: str, order_id: str) -> str:
    # Opaque but STABLE: encodes the sort key (placed_at, id), not a positional offset.
    return base64.urlsafe_b64encode(json.dumps({"p": placed_at, "i": order_id}).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        d = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return d["p"], d["i"]
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


# --- row -> type builders --------------------------------------------------
def _delivery_cost(method: str, smart: bool) -> Decimal:
    return Decimal("0.00") if smart else Decimal(_DELIVERY_BASE[method])


def _seller(row: Optional[dict]) -> Optional[Seller]:
    return Seller(id=row["id"], name=row["name"], rating=row["rating"]) if row else None


def _buyer(row: Optional[dict]) -> Optional[Buyer]:
    return Buyer(id=row["id"], login=row["login"], smart=row["smart"], locale=row["locale"]) if row else None


def _offer(row: Optional[dict]) -> Optional[Offer]:
    if not row:
        return None
    return Offer(id=row["id"], name=row["name"], category=row["category"],
                 price=Money(amount=Decimal(str(row["price"]))), seller_id=row["seller_id"])


def _line_item(row: dict) -> LineItem:
    return LineItem(quantity=row["quantity"], unit_price=Money(amount=Decimal(str(row["unit_price"]))),
                    offer_id=row["offer_id"])


def _order(row: Optional[dict]) -> Optional[Order]:
    if not row:
        return None
    return Order(id=row["id"], status=OrderStatus(row["status"]),
                 placed_at=datetime.fromisoformat(row["placed_at"]),
                 buyer_id=row["buyer_id"], seller_id=row["seller_id"], delivery_method=row["delivery_method"])


def _to_return(row: dict) -> CustomerReturn:
    return CustomerReturn(id=row["id"], order_id=row["order_id"], reason=row["reason"],
                          status=ReturnStatus(row["status"]), opened_at=datetime.fromisoformat(row["opened_at"]))


# --- root operations -------------------------------------------------------
@strawberry.type
class Query:
    @strawberry.field
    async def order(self, info: strawberry.Info, id: strawberry.ID) -> Optional[Order]:
        return _order(await info.context["loaders"]["order"].load(str(id)))

    @strawberry.field
    def orders(self, buyer_id: strawberry.ID, first: int = 10, after: Optional[str] = None) -> OrderConnection:
        if not 1 <= first <= MAX_PAGE_SIZE:
            raise ValueError(f"'first' must be between 1 and {MAX_PAGE_SIZE}")
        after_key = _decode_cursor(after) if after else None
        page = db.fetch_orders_page(str(buyer_id), first, after_key)
        edges = [OrderEdge(node=_order(r), cursor=_encode_cursor(r["placed_at"], r["id"]))
                 for r in page["orders"]]
        return OrderConnection(
            edges=edges,
            page_info=PageInfo(
                has_next_page=page["has_next"],
                has_previous_page=after is not None,
                start_cursor=edges[0].cursor if edges else None,
                end_cursor=edges[-1].cursor if edges else None,
            ),
            total_count=page["total"],
        )

    @strawberry.field
    async def offer(self, info: strawberry.Info, id: strawberry.ID) -> Optional[Offer]:
        return _offer(await info.context["loaders"]["offer"].load(str(id)))

    @strawberry.field
    async def seller(self, info: strawberry.Info, id: strawberry.ID) -> Optional[Seller]:
        return _seller(await info.context["loaders"]["seller"].load(str(id)))

    @strawberry.field
    async def node(self, info: strawberry.Info, id: strawberry.ID) -> Optional[Node]:
        sid = str(id)
        loaders = info.context["loaders"]
        prefix = sid.split("-")[0]
        if prefix == "ORD":
            return _order(await loaders["order"].load(sid))
        if prefix == "OFR":
            return _offer(await loaders["offer"].load(sid))
        if prefix == "BUY":
            return _buyer(await loaders["buyer"].load(sid))
        if prefix == "SEL":
            return _seller(await loaders["seller"].load(sid))
        if prefix == "RET":
            row = db.fetch_return(sid)
            return _to_return(row) if row else None
        return None


@strawberry.type
class Mutation:
    @strawberry.mutation
    def request_return(self, order_id: strawberry.ID, reason: str) -> Optional[CustomerReturn]:
        row = db.insert_return(str(order_id), reason)
        return _to_return(row) if row else None


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    types=[Buyer, Seller, Offer, Order, CustomerReturn],  # ensure all Node implementers are registered
    extensions=[lambda: QueryDepthLimiter(max_depth=10)],  # factory: a fresh extension per request
)
