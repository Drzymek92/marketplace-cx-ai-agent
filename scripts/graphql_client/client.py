"""GraphQL client wrappers over the marketplace endpoint.

These typed functions are the "consume" side and will be exposed as agent tools later.
Transport is httpx (already a dependency); each method sends one GraphQL operation.
"""

from typing import Any, Optional

import httpx

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/graphql"

_MONEY = "{ amount currency }"

_ORDER_FIELDS = f"""
    id
    status
    placedAt
    total {_MONEY}
    delivery {{ method cost {_MONEY} }}
    buyer {{ id login smart locale }}
    seller {{ id name rating }}
    lineItems {{
      quantity
      unitPrice {_MONEY}
      totalPrice {_MONEY}
      offer {{ id name category price {_MONEY} }}
    }}
"""


class MarketplaceClient:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 10.0):
        self.endpoint = endpoint
        self.timeout = timeout

    def _execute(self, query: str, variables: Optional[dict] = None) -> dict[str, Any]:
        resp = httpx.post(self.endpoint, json={"query": query, "variables": variables or {}},
                          timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return payload["data"]

    def get_order(self, order_id: str) -> Optional[dict]:
        query = "query($id: ID!) { order(id: $id) {" + _ORDER_FIELDS + "} }"
        return self._execute(query, {"id": order_id})["order"]

    def list_buyer_orders(self, buyer_id: str, first: int = 10, after: Optional[str] = None) -> dict:
        """Returns the raw connection: {edges:[{cursor,node}], pageInfo, totalCount}."""
        query = ("query($b: ID!, $n: Int!, $a: String) { orders(buyerId: $b, first: $n, after: $a) {"
                 " totalCount pageInfo { hasNextPage endCursor }"
                 " edges { cursor node {" + _ORDER_FIELDS + "} } } }")
        return self._execute(query, {"b": buyer_id, "n": first, "a": after})["orders"]

    def request_return(self, order_id: str, reason: str) -> Optional[dict]:
        query = ("mutation($id: ID!, $r: String!) { requestReturn(orderId: $id, reason: $r) "
                 "{ id orderId reason status openedAt } }")
        return self._execute(query, {"id": order_id, "r": reason})["requestReturn"]

    def get_node(self, node_id: str) -> Optional[dict]:
        """Fetch any entity by its global id via the Relay node field."""
        query = ("query($id: ID!) { node(id: $id) { __typename id"
                 " ... on Order { status } ... on Offer { name } ... on Buyer { login } } }")
        return self._execute(query, {"id": node_id})["node"]
