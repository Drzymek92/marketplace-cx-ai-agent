# schema.py

## Purpose
The Strawberry GraphQL schema for the Allegro-style marketplace (Module 1 serve side). Defines the
types, queries, and the `requestReturn` mutation, showcasing production schema-design patterns
(enums, Money value type, Relay node + cursor connection, DataLoader-resolved relationships).

## Inputs
- Per-request `info.context["loaders"]` (the DataLoaders from `loaders.py`) for relationship resolution.
- Plain row dicts from `db.py` (batch `fetch_*_by_ids`, `fetch_orders_page`, `fetch_return`, `insert_return`).

## Outputs
- A `strawberry.Schema` object (`schema`) consumed by `app.py`'s `GraphQLRouter`.
- Resolved GraphQL types; SDL is exported separately to `schema.graphql`.

## Key Functions / Types
| Symbol | What it does |
|---|---|
| `OrderStatus` / `ReturnStatus` / `DeliveryMethod` | State enums (no free-text status) |
| `Money` | Decimal `amount` + `currency` value type — money is never a float |
| `Node` (interface) + `Query.node(id)` | Relay global object identification; routes by id prefix |
| `Order` / `Buyer` / `Seller` / `Offer` / `LineItem` / `Delivery` / `CustomerReturn` | Domain types; relations resolved lazily via loaders |
| `OrderConnection` / `OrderEdge` / `PageInfo` | Relay cursor connection for orders |
| `_encode_cursor` / `_decode_cursor` | Opaque but STABLE cursor over the sort key `(placed_at, id)` |
| `_delivery_cost` | Free for Smart! buyers, else method base rate |
| `_order` / `_buyer` / `_seller` / `_offer` / `_line_item` / `_to_return` | row dict → GraphQL type |
| `Query.order/orders/offer/seller/node`, `Mutation.request_return` | Root operations |

## Dependencies
- Internal: `graphql_server.db`.
- External: `strawberry`, `strawberry.extensions.QueryDepthLimiter`; stdlib `base64`, `json`, `decimal`, `datetime`, `enum`.

## Known Gotchas
- Strawberry maps snake_case → camelCase in the API (`line_items` → `lineItems`).
- Relationship fields are `async` and resolve through per-request DataLoaders — never query `db`
  directly in a relation resolver (that reintroduces N+1). FK ids are held as `strawberry.Private`.
- `node(id:)` routes by id PREFIX (`ORD`/`OFR`/`BUY`/`SEL`/`RET`); a new entity type needs a branch here.
- `orders` bounds `first` to 1..`MAX_PAGE_SIZE` (100); a malformed cursor raises a clean `ValueError`.
- `QueryDepthLimiter` is passed as a FACTORY (`lambda: ...`) so each request gets a fresh extension.
- All `Node` implementers must be listed in `Schema(types=[...])` to be registered.

## Open Work
- Error masking for unexpected (non-validation) errors; auth / rate-limiting / persisted queries
  are out of demo scope (noted for the README).
