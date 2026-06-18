"""In-process schema tests — exercise resolvers + SQLite without starting a server.

Resolvers are async (DataLoader-based), so we drive the async schema.execute via asyncio.run.
Loaders are built INSIDE the coroutine so each DataLoader binds to the running event loop.
"""

import asyncio

from scripts.graphql_server.schema import schema
from scripts.graphql_server.loaders import make_loaders


def _execute(query, variables=None):
    async def _go():
        return await schema.execute(query, variable_values=variables or {},
                                    context_value={"loaders": make_loaders()})
    return asyncio.run(_go())


def _run(query, variables=None):
    result = _execute(query, variables)
    assert result.errors is None, result.errors
    return result.data


def test_order_query_returns_nested_graph(fresh_db):
    data = _run("""
        query($id: ID!) {
          order(id: $id) {
            status
            placedAt
            total { amount currency }
            delivery { method cost { amount } }
            buyer { login smart }
            lineItems { quantity unitPrice { amount } totalPrice { amount } offer { name category } }
          }
        }
    """, {"id": "ORD-4001"})
    order = data["order"]
    assert order["status"] == "DELIVERED"          # enum -> member name
    assert order["buyer"]["smart"] is True
    assert order["delivery"]["method"] == "COURIER"
    # Smart! member -> free delivery
    assert order["delivery"]["cost"]["amount"] == "0.00"
    # 1 x 199.00 + 2 x 89.00 + 0.00 delivery = 377.00, as a Decimal string with PLN
    assert order["total"]["amount"] == "377.00"
    assert order["total"]["currency"] == "PLN"
    assert len(order["lineItems"]) == 2


def test_money_amount_is_decimal_string(fresh_db):
    data = _run('query { offer(id: "OFR-3001") { price { amount currency } } }')
    assert data["offer"]["price"] == {"amount": "199.00", "currency": "PLN"}


def test_non_smart_buyer_pays_delivery(fresh_db):
    data = _run('query { order(id: "ORD-4002") { delivery { method cost { amount } } } }')
    # BUY-1002 is not Smart!, PARCEL_LOCKER base rate applies
    assert data["order"]["delivery"]["cost"]["amount"] == "9.99"


def test_unknown_order_is_null(fresh_db):
    data = _run('query { order(id: "ORD-9999") { status } }')
    assert data["order"] is None


def test_orders_connection_paginates(fresh_db):
    # BUY-1001 has two orders (ORD-4001 newest, ORD-4004 older)
    page1 = _run("""
        query($b: ID!) {
          orders(buyerId: $b, first: 1) {
            totalCount
            pageInfo { hasNextPage endCursor }
            edges { cursor node { id } }
          }
        }
    """, {"b": "BUY-1001"})["orders"]
    assert page1["totalCount"] == 2
    assert page1["pageInfo"]["hasNextPage"] is True
    assert page1["edges"][0]["node"]["id"] == "ORD-4001"

    page2 = _run("""
        query($b: ID!, $a: String!) {
          orders(buyerId: $b, first: 1, after: $a) {
            pageInfo { hasNextPage }
            edges { node { id } }
          }
        }
    """, {"b": "BUY-1001", "a": page1["pageInfo"]["endCursor"]})["orders"]
    assert page2["pageInfo"]["hasNextPage"] is False
    assert page2["edges"][0]["node"]["id"] == "ORD-4004"


def test_node_resolves_by_global_id(fresh_db):
    data = _run("""
        query {
          o: node(id: "ORD-4001") { __typename id ... on Order { status } }
          f: node(id: "OFR-3001") { __typename id ... on Offer { name } }
          b: node(id: "BUY-1001") { __typename id ... on Buyer { login } }
        }
    """)
    assert data["o"]["__typename"] == "Order" and data["o"]["status"] == "DELIVERED"
    assert data["f"]["__typename"] == "Offer" and data["f"]["name"] == "Wireless Headphones"
    assert data["b"]["__typename"] == "Buyer" and data["b"]["login"] == "anna_k"


def test_request_return_mutation_persists(fresh_db):
    data = _run('mutation($id: ID!, $r: String!) { requestReturn(orderId: $id, reason: $r) { status orderId openedAt } }',
                {"id": "ORD-4001", "r": "Changed my mind"})
    assert data["requestReturn"]["status"] == "REQUESTED"
    assert data["requestReturn"]["orderId"] == "ORD-4001"
    assert data["requestReturn"]["openedAt"] is not None


def test_request_return_on_unknown_order_is_null(fresh_db):
    data = _run('mutation { requestReturn(orderId: "ORD-9999", reason: "x") { id } }')
    assert data["requestReturn"] is None


def test_cursor_is_stable_under_insert(fresh_db):
    from scripts.graphql_server import db

    page1 = _run('query($b: ID!){ orders(buyerId: $b, first: 1) { edges { node { id } } pageInfo { endCursor } } }',
                 {"b": "BUY-1001"})["orders"]
    assert page1["edges"][0]["node"]["id"] == "ORD-4001"
    cursor = page1["pageInfo"]["endCursor"]

    # Insert a NEWER order for the same buyer — this would shift an offset-based cursor and
    # cause ORD-4001 to be returned again on page 2. A keyset cursor is immune.
    conn = db.get_conn()
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",
                 ("ORD-4005", "BUY-1001", "SEL-2001", "DELIVERED", "2026-06-20T10:00:00+00:00", "COURIER"))
    conn.execute("INSERT INTO order_items VALUES (?,?,?,?)", ("ORD-4005", "OFR-3001", 1, "199.00"))
    conn.commit()
    conn.close()

    page2 = _run('query($b: ID!, $a: String!){ orders(buyerId: $b, first: 1, after: $a) { edges { node { id } } } }',
                 {"b": "BUY-1001", "a": cursor})["orders"]
    assert page2["edges"][0]["node"]["id"] == "ORD-4004"   # the item that truly follows ORD-4001, no dup


def test_invalid_cursor_is_rejected(fresh_db):
    res = _execute('query { orders(buyerId: "BUY-1001", first: 1, after: "not-a-cursor") { totalCount } }')
    assert res.errors is not None


def test_first_out_of_bounds_is_rejected(fresh_db):
    assert _execute('query { orders(buyerId: "BUY-1001", first: 0) { totalCount } }').errors is not None
    assert _execute('query { orders(buyerId: "BUY-1001", first: 9999) { totalCount } }').errors is not None


def test_dataloader_batches_nested_fetches(fresh_db):
    from scripts.graphql_server import db

    db.reset_counts()
    _run("""
        query($b: ID!) {
          orders(buyerId: $b, first: 10) {
            edges { node {
              id
              buyer { login }
              seller { name }
              lineItems { quantity offer { name seller { name } } }
            } }
          }
        }
    """, {"b": "BUY-1001"})
    counts = db.get_counts()
    # BUY-1001 has 2 orders with 3 line items total. Without batching that would be many queries;
    # with DataLoaders each entity type is fetched in a single batched (+ cached) query.
    assert counts["buyers"] == 1
    assert counts["order_items"] == 1
    assert counts["offers"] == 1
    assert counts["sellers"] <= 2   # Order.seller + Offer.seller waves; cache collapses repeats


def test_page_info_is_relay_complete(fresh_db):
    p = _run('query($b: ID!){ orders(buyerId: $b, first: 1) { pageInfo { hasNextPage hasPreviousPage startCursor endCursor } } }',
             {"b": "BUY-1001"})["orders"]["pageInfo"]
    assert p["hasNextPage"] is True
    assert p["hasPreviousPage"] is False
    assert p["startCursor"] is not None and p["startCursor"] == p["endCursor"]   # single-item page
