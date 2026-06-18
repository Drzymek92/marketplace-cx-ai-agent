"""Module 5 tests — HITL feedback: simulated reviewer, store, capture orchestration, and the
graph wiring that records a review for every human-flagged turn.

`fresh_feedback_db` is autouse (see conftest), so each test gets an isolated feedback.db.
"""

from __future__ import annotations

import pytest

from scripts.agent.graph import answer
from scripts.agent.tools import AgentTools
from scripts.config.loader import Config
from scripts.feedback import capture, reviewer
from scripts.feedback import store as feedback_store
from tests.test_agent import FakeClient


# --- simulated reviewer (deterministic, no LLM) ------------------------------------
def test_reviewer_corrects_missing_order():
    sig = reviewer.simulate({"retrieved": {"_missing": "no order_id supplied"}, "meta": {}})
    assert sig["signal_type"] == "correction"
    assert sig["rating"] == -1
    assert "ORD-" in sig["correction"]


def test_reviewer_escalates_on_llm_fallback():
    sig = reviewer.simulate({"retrieved": {}, "meta": {"responded_with_llm": False}})
    assert sig["signal_type"] == "escalation"
    assert sig["rating"] == -1
    assert sig["correction"] is None


def test_reviewer_approves_grounded_decision():
    sig = reviewer.simulate({
        "retrieved": {"check_eligibility": {"rule_version": "v1", "eligible": True}},
        "meta": {"responded_with_llm": True},
    })
    assert sig["signal_type"] == "rating"
    assert sig["rating"] == 1


# --- store -------------------------------------------------------------------------
def test_store_record_and_read():
    fid = capture.submit("rating", turn_id=7, buyer_id="BUY-1003", intent="faq", rating=1)
    assert fid > 0
    recs = feedback_store.list_for_turn(7)
    assert len(recs) == 1
    assert recs[0]["reviewer"] == "user" and recs[0]["rating"] == 1
    assert recs[0]["tools_used"] == []  # round-trips through JSON
    assert feedback_store.count() == 1


# --- capture orchestration ---------------------------------------------------------
def _flagged_state():
    return {
        "intent": "commission_query",
        "buyer_id": "BUY-1001",
        "needs_human": True,
        "retrieved": {"compute_order_commission": {"rule_version": "v1", "total_commission": "9.00"}},
        "tool_calls": ["compute_order_commission"],
        "meta": {"turn_id": 42, "telemetry": {
            "respond_tier": "reason", "est_tokens": 120,
            "tools_used": ["compute_order_commission"], "rule_version": "v1",
            "intent": "commission_query",
        }},
    }


def test_capture_turn_snapshots_telemetry():
    fid = capture.capture_turn(_flagged_state(), Config())
    assert fid is not None
    rec = feedback_store.list_for_turn(42)[0]
    assert rec["reviewer"] == "sim" and rec["signal_type"] == "rating" and rec["rating"] == 1
    assert rec["model_tier"] == "reason" and rec["tokens"] == 120
    assert rec["rule_version"] == "v1" and rec["tools_used"] == ["compute_order_commission"]


def test_capture_turn_skips_routine_turn_when_audit_off():
    cfg = Config()
    cfg.feedback.audit_sample_rate = 0.0   # no random audit → confident turn isn't reviewed
    state = {**_flagged_state(), "needs_human": False}
    assert capture.capture_turn(state, cfg) is None
    assert feedback_store.count() == 0


def test_capture_turn_respects_module_toggle():
    cfg = Config()
    cfg.modules.feedback_capture = False
    assert capture.capture_turn(_flagged_state(), cfg) is None
    assert feedback_store.count() == 0


# --- P0: unbiased random-sample audit of NON-flagged turns (COPC / AWS A2I) --------
def test_should_audit_bounds():
    from scripts.feedback.capture import _should_audit
    assert _should_audit(0.0) is False
    assert _should_audit(1.0) is True


def test_audit_reviews_unflagged_turn():
    cfg = Config()
    cfg.feedback.audit_sample_rate = 1.0   # rate=1 → always audit (deterministic)
    state = {**_flagged_state(), "needs_human": False}
    fid = capture.capture_turn(state, cfg)
    assert fid is not None
    rec = feedback_store.all_records()[0]
    assert rec["reviewer"] == "sim-audit"   # tagged as a proactive audit pick, not a flagged review


# --- graph integration -------------------------------------------------------------
@pytest.fixture()
def tools():
    return AgentTools(client=FakeClient())


@pytest.fixture()
def mock_llm(monkeypatch):
    def setup(intent, order_id=None, confidence=0.9):
        monkeypatch.setattr("scripts.llm_client.llm_json",
                            lambda *a, **k: {"intent": intent, "order_id": order_id, "confidence": confidence})
        monkeypatch.setattr("scripts.llm_client.llm_call", lambda *a, **k: f"[answer for {intent}]")
    return setup


def test_feedback_recorded_for_flagged_turn(fresh_profile_db, tools, mock_llm):
    mock_llm("refund_eligibility", order_id="ORD-OLD")
    s = answer("Refund on ORD-OLD?", buyer_id="BUY-1001", tools=tools)
    assert s["needs_human"] is True
    assert s["meta"].get("feedback_id")
    recs = feedback_store.all_records()
    assert len(recs) == 1
    assert recs[0]["reviewer"] == "sim"
    assert recs[0]["intent"] == "refund_eligibility"
    assert recs[0]["turn_id"] == s["meta"]["turn_id"]   # linked to the persisted history turn


def test_no_feedback_for_routine_turn(fresh_profile_db, tools, mock_llm, monkeypatch):
    monkeypatch.setattr("scripts.feedback.capture._should_audit", lambda rate: False)  # not in the audit sample
    mock_llm("return_check", order_id="ORD-RET")
    s = answer("Can I return ORD-RET?", buyer_id="BUY-1001", tools=tools)
    assert s["needs_human"] is False
    assert "feedback_id" not in s["meta"]
    assert feedback_store.count() == 0


def test_audit_captures_confident_turn_through_graph(fresh_profile_db, tools, mock_llm, monkeypatch):
    monkeypatch.setattr("scripts.feedback.capture._should_audit", lambda rate: True)  # in the audit sample
    mock_llm("return_check", order_id="ORD-RET")
    s = answer("Can I return ORD-RET?", buyer_id="BUY-1001", tools=tools)
    assert s["needs_human"] is False                 # confident, not flagged…
    assert s["meta"].get("feedback_id")              # …but still audited
    rec = feedback_store.all_records()[0]
    assert rec["reviewer"] == "sim-audit"
    assert rec["turn_id"] == s["meta"]["turn_id"]
