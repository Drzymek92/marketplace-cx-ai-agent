"""Normalized order view consumed by the rules engine.

Rules operate on this small view rather than raw GraphQL types so they stay pure and
unit-testable. from_graphql_order() adapts the GraphQL response shape — i.e. exactly what
the agent receives from the MarketplaceClient.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class LineItemView:
    offer_id: str
    name: str
    category: str
    quantity: int
    unit_amount: Decimal


@dataclass
class OrderView:
    id: str
    status: str
    placed_at: datetime
    buyer_smart: bool
    currency: str
    line_items: list[LineItemView]

    @property
    def categories(self) -> set[str]:
        return {li.category for li in self.line_items}

    @classmethod
    def from_graphql_order(cls, d: dict) -> "OrderView":
        # A partial/malformed payload becomes a clean ValueError naming the missing field, which
        # the tool layer turns into ToolResult(ok=False) — never a raw KeyError up the stack.
        try:
            items = [
                LineItemView(
                    offer_id=li["offer"]["id"],
                    name=li["offer"]["name"],
                    category=li["offer"]["category"],
                    quantity=li["quantity"],
                    unit_amount=Decimal(str(li["unitPrice"]["amount"])),
                )
                for li in d["lineItems"]
            ]
            currency = (d.get("total") or {}).get("currency") \
                or (d["lineItems"][0]["unitPrice"].get("currency") if d["lineItems"] else None) \
                or "PLN"
            return cls(
                id=d["id"],
                status=d["status"],
                placed_at=datetime.fromisoformat(d["placedAt"]),
                buyer_smart=bool(d["buyer"]["smart"]),
                currency=currency,
                line_items=items,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed order payload ({exc})") from exc
