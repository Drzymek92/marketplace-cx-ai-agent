"""Rules-engine tests. Order views are built from real schema output so the rule input
contract is exactly what the agent will pass from the GraphQL client.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.config.loader import load_config
from scripts.graphql_server.schema import schema
from scripts.graphql_server.loaders import make_loaders
from scripts.rules import compute_commission, is_returnable, qualifies_for, OrderView

NOW = datetime(2026, 6, 17, tzinfo=timezone.utc)

_Q = """
query($id: ID!) {
  order(id: $id) {
    id status placedAt
    total { amount currency }
    buyer { smart }
    lineItems { quantity unitPrice { amount currency } offer { id name category } }
  }
}
"""


def _order_view(order_id: str) -> OrderView:
    async def _go():
        return await schema.execute(_Q, variable_values={"id": order_id},
                                    context_value={"loaders": make_loaders()})
    res = asyncio.run(_go())
    assert res.errors is None, res.errors
    return OrderView.from_graphql_order(res.data["order"])


def _rules():
    return load_config().rules


def test_config_loads_thresholds():
    r = _rules()
    assert r.returns.window_days == 14
    assert r.commission.per_category_pct["electronics"] == 0.08
    assert r.eligibility.dispute_window_days == 30


def test_returnable_in_window_for_smart_member(fresh_db):
    d = is_returnable(_order_view("ORD-4001"), _rules(), now=NOW)
    assert d.returnable is True
    assert d.free_return is True            # Smart! member -> free return
    assert d.days_since_order == 7
    assert d.rule_version == "v1"


def test_not_returnable_out_of_window(fresh_db):
    d = is_returnable(_order_view("ORD-4002"), _rules(), now=NOW)
    assert d.returnable is False
    assert d.days_since_order == 47


def test_not_returnable_when_not_delivered_and_excluded(fresh_db):
    d = is_returnable(_order_view("ORD-4003"), _rules(), now=NOW)   # SHIPPED + personalized/perishable
    assert d.returnable is False
    assert d.excluded_categories == ["perishable", "personalized"]


def test_commission_uses_category_rate(fresh_db):
    b = compute_commission(_order_view("ORD-4001"), _rules())       # electronics @ 8%
    assert b.items_commission == Decimal("30.16")
    assert b.transaction_fee == Decimal("1.00")
    assert b.total_commission == Decimal("31.16")
    assert b.currency == "PLN"


def test_commission_falls_back_to_default_rate(fresh_db):
    b = compute_commission(_order_view("ORD-4003"), _rules())       # categories not in map -> default 10%
    assert b.total_commission == Decimal("11.80")


def test_eligibility_refund_follows_returnability(fresh_db):
    assert qualifies_for("refund", _order_view("ORD-4001"), _rules(), now=NOW).eligible is True
    assert qualifies_for("refund", _order_view("ORD-4002"), _rules(), now=NOW).eligible is False


def test_eligibility_dispute_respects_window(fresh_db):
    assert qualifies_for("dispute", _order_view("ORD-4001"), _rules(), now=NOW).eligible is True    # 7d
    assert qualifies_for("dispute", _order_view("ORD-4002"), _rules(), now=NOW).eligible is False   # 47d


def test_eligibility_unknown_process_raises(fresh_db):
    with pytest.raises(ValueError):
        qualifies_for("teleport", _order_view("ORD-4001"), _rules(), now=NOW)
