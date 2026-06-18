"""Module 3 tests — tool registry + LangGraph flow.

The LLM is mocked (classify + respond) so the graph runs deterministically offline. Tool
data comes from a FakeClient instead of a live GraphQL server, so the rules/decision logic
is what's under test, not the network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.agent import graph as graph_mod
from scripts.agent.graph import answer
from scripts.agent.tools import AgentTools


def _order(order_id, *, status, days_ago, smart, category, amount, qty=1):
    placed = datetime.now(timezone.utc).replace(microsecond=0)
    placed = placed.fromordinal(placed.toordinal() - days_ago)
    return {
        "id": order_id,
        "status": status,
        "placedAt": placed.isoformat(),
        "total": {"amount": str(amount * qty), "currency": "PLN"},
        "buyer": {"id": "BUY-1001", "login": "anna_k", "smart": smart, "locale": "pl"},
        "lineItems": [{
            "quantity": qty,
            "unitPrice": {"amount": str(amount), "currency": "PLN"},
            "offer": {"id": "OFR-1", "name": "Thing", "category": category},
        }],
    }


class FakeClient:
    def __init__(self):
        self.orders = {
            "ORD-RET": _order("ORD-RET", status="DELIVERED", days_ago=3, smart=True, category="electronics", amount="100.00"),
            "ORD-OLD": _order("ORD-OLD", status="DELIVERED", days_ago=40, smart=False, category="electronics", amount="100.00"),
        }
        self.returns = []

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def list_buyer_orders(self, buyer_id, first=5, after=None):
        nodes = [{"cursor": "c", "node": o} for o in self.orders.values()]
        return {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": nodes}

    def request_return(self, order_id, reason):
        self.returns.append((order_id, reason))
        return {"id": "RET-9", "orderId": order_id, "reason": reason, "status": "REQUESTED", "openedAt": "now"}


@pytest.fixture()
def tools():
    return AgentTools(client=FakeClient())


@pytest.fixture()
def mock_llm(monkeypatch):
    """Returns a setter that fixes the classify result and echoes facts for respond."""
    def setup(intent, order_id=None, confidence=0.9):
        def fake_json(prompt, system=None, model=None, **kw):
            return {"intent": intent, "order_id": order_id, "confidence": confidence}
        def fake_call(prompt, system=None, model=None, **kw):
            return f"[answer for {intent}]"
        monkeypatch.setattr("scripts.llm_client.llm_json", fake_json)
        monkeypatch.setattr("scripts.llm_client.llm_call", fake_call)
    return setup


# --- tool registry -----------------------------------------------------------------
def test_check_return_returnable(tools):
    r = tools.check_return("ORD-RET")
    assert r.ok
    assert r.data["returnable"] is True
    assert r.data["free_return"] is True  # Smart! member
    assert r.data["rule_version"] == "v1"


def test_check_return_outside_window(tools):
    r = tools.check_return("ORD-OLD")
    assert r.ok and r.data["returnable"] is False


def test_commission_math(tools):
    r = tools.compute_order_commission("ORD-RET")
    assert r.ok
    # electronics 8% on 100.00 GMV + 1.00 fee (from config.example.yaml) = 9.00
    assert r.data["items_commission"] == "8.00"
    assert r.data["total_commission"] == "9.00"


def test_eligibility_refund_outside_window(tools):
    r = tools.check_eligibility("ORD-OLD", "refund")
    assert r.ok and r.data["eligible"] is False


def test_unknown_tool_and_missing_order(tools):
    assert tools.call("nope").ok is False
    assert tools.fetch_order("ORD-NONE").ok is False


# --- graph flow --------------------------------------------------------------------
def test_graph_routes_return_check(tools, mock_llm):
    mock_llm("return_check", order_id="ORD-RET")
    s = answer("Can I return ORD-RET?", tools=tools)
    assert s["intent"] == "return_check"
    assert s["tool_calls"] == ["check_return"]
    assert s["needs_human"] is False
    assert s["meta"]["rule_version"] == "v1"
    assert s["answer"]


def test_graph_high_stakes_flags_hitl(tools, mock_llm):
    mock_llm("refund_eligibility", order_id="ORD-OLD")
    s = answer("Refund on ORD-OLD?", tools=tools)
    assert s["needs_human"] is True
    assert s["tool_calls"] == ["check_eligibility"]
    assert "review" in s["answer"].lower()


def test_graph_low_confidence_flags_hitl(tools, mock_llm):
    mock_llm("order_status", order_id="ORD-RET", confidence=0.2)
    s = answer("hmm ORD-RET", tools=tools)
    assert s["needs_human"] is True


def test_graph_missing_order_id(tools, mock_llm):
    mock_llm("return_check", order_id=None)
    s = answer("can I return something?", tools=tools)
    assert s["retrieved"].get("_missing")
    assert s["tool_calls"] == []


def test_order_id_regex_backstop(tools, mock_llm):
    # LLM misses the id; the literal ORD-#### in the message must still be picked up.
    mock_llm("commission_query", order_id=None)
    s = answer("commission on order ORD-RET please", tools=tools)
    assert s["order_id"] == "ORD-RET"
    assert s["tool_calls"] == ["compute_order_commission"]


# --- deterministic triage classifier (no LLM) --------------------------------------
def test_deterministic_classify_keywords():
    from scripts.agent.triage import deterministic_classify as dc
    assert dc("Can I return ORD-4001?")["intent"] == "return_check"
    assert dc("seller commission?")["intent"] == "commission_query"
    assert dc("where is my stuff")["intent"] == "order_status"
    assert dc("ORD-4002")["order_id"] == "ORD-4002"
    # explicit keyword -> high confidence (cascade trusts it); bare order id -> lower
    assert dc("refund please")["confidence"] >= 0.8
    assert dc("ORD-4002")["confidence"] < 0.8


# --- token cascade + telemetry (Module 7 slice) ------------------------------------
@pytest.fixture()
def llm_spy(monkeypatch):
    """Counts LLM calls so tests can assert the cascade/cache actually avoided them."""
    counts = {"classify": 0, "respond": 0}

    def spy_json(prompt, system=None, model=None, **kw):
        counts["classify"] += 1
        return {"intent": "unknown", "order_id": None, "confidence": 0.5}

    def spy_call(prompt, system=None, model=None, **kw):
        counts["respond"] += 1
        return "[composed answer]"

    monkeypatch.setattr("scripts.llm_client.llm_json", spy_json)
    monkeypatch.setattr("scripts.llm_client.llm_call", spy_call)
    return counts


def test_faq_short_circuit_zero_llm_calls(tools, llm_spy):
    s = answer("What is your return policy?", tools=tools)
    assert s["intent"] == "faq"
    assert llm_spy == {"classify": 0, "respond": 0}      # no LLM at all
    assert "14 days" in s["answer"]
    assert s["tool_calls"] == []
    assert s["meta"]["telemetry"]["llm_calls"] == 0
    assert s["meta"]["telemetry"]["cache_hit"] is True


def test_confident_triage_skips_llm_classify(tools, llm_spy):
    s = answer("Can I return ORD-RET?", tools=tools)
    assert s["intent"] == "return_check"
    assert s["meta"]["classify_mode"] == "triage"
    assert llm_spy["classify"] == 0                      # classify LLM skipped
    assert llm_spy["respond"] == 1                       # only respond paid
    assert s["meta"]["telemetry"]["llm_calls"] == 1
    assert s["tool_calls"] == ["check_return"]


def test_high_stakes_still_gated_after_triage(tools, llm_spy):
    s = answer("What is the commission on ORD-RET?", tools=tools)
    assert s["meta"]["classify_mode"] == "triage"        # triage was confident
    assert llm_spy["classify"] == 0
    assert s["needs_human"] is True                      # but the stakes gate still fired
    assert s["tool_calls"] == ["compute_order_commission"]


def test_ambiguous_message_escalates_to_llm_classify(tools, llm_spy):
    # No keyword, no order id, not an FAQ phrase -> deterministic triage is unsure -> LLM.
    s = answer("hey can you help me with something weird", tools=tools)
    assert s["meta"]["classify_mode"] == "llm"
    assert llm_spy["classify"] == 1
    assert s["meta"]["telemetry"]["llm_calls"] == 2
