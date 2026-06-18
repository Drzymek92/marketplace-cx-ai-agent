"""Module 7 — token ceilings + degradation ladder.

Unit tests drive the governor's pressure→level→directive logic on a seeded telemetry census; an
end-to-end test confirms a low ceiling degrades a real turn (cheaper tier / canned / handoff).
"""

from __future__ import annotations

import pytest

from scripts.agent.graph import answer, build_graph
from scripts.agent.tools import AgentTools
from scripts.budget import governor, telemetry_store
from scripts.budget.router import cap_tier
from scripts.config.loader import Config
from tests.test_agent import FakeClient


def _cfg(**budget):
    cfg = Config()
    for k, v in budget.items():
        setattr(cfg.budget, k, v)
    return cfg


# --- tier clamp --------------------------------------------------------------------
def test_cap_tier_clamps_down_only():
    assert cap_tier("reason", "fast") == "fast"   # clamp expensive → cheap
    assert cap_tier("fast", "reason") == "fast"   # never upgrade
    assert cap_tier("bulk", None) == "bulk"       # no cap → unchanged


# --- pressure → level → directive --------------------------------------------------
def test_governor_no_caps_is_noop():
    d = governor.decide("BUY-1001", Config())   # default ceilings = 0 (unlimited)
    assert d.level == 0 and not d.canned_only and d.cap_tier is None


def test_governor_ladder_escalates_with_spend():
    telemetry_store.record({"intent": "x", "est_tokens": 950}, buyer_id="BUY-1001")
    # per-conversation cap 1000 → pressure 0.95 → level 1 → drop_to_cheaper_tier
    d1 = governor.decide("BUY-1001", _cfg(per_conversation_tokens=1000))
    assert d1.level == 1 and d1.cap_tier == "fast" and not d1.shrink_context

    # cap 500 → pressure 1.9 → level 3 → through cached_or_canned_only
    d3 = governor.decide("BUY-1001", _cfg(per_conversation_tokens=500))
    assert d3.level == 3 and d3.shrink_context and d3.canned_only and not d3.force_human

    # cap 400 → pressure 2.375 → level 4 → handoff_to_human
    d4 = governor.decide("BUY-1001", _cfg(per_conversation_tokens=400))
    assert d4.level == 4 and d4.force_human


def test_governor_daily_ceiling():
    telemetry_store.record({"intent": "x", "est_tokens": 300}, buyer_id="BUY-1001")
    telemetry_store.record({"intent": "y", "est_tokens": 300}, buyer_id="BUY-1002")
    d = governor.decide("BUY-1003", _cfg(daily_tokens=500))  # 600 today vs 500 → pressure 1.2 → level 2
    assert d.level == 2 and d.shrink_context and not d.canned_only


# --- end-to-end: a low ceiling degrades a real turn --------------------------------
@pytest.fixture()
def tools():
    return AgentTools(client=FakeClient())


def test_high_pressure_turn_goes_canned(fresh_profile_db, tools, monkeypatch):
    # pre-load spend so the conversation is already way over a tiny ceiling
    telemetry_store.record({"intent": "return_check", "est_tokens": 5000}, buyer_id="BUY-1001")
    called = {"respond": 0}
    monkeypatch.setattr("scripts.llm_client.llm_json", lambda *a, **k: {"intent": "return_check", "order_id": "ORD-RET", "confidence": 0.9})
    monkeypatch.setattr("scripts.llm_client.llm_call", lambda *a, **k: called.__setitem__("respond", called["respond"] + 1) or "live answer")

    cfg = _cfg(per_conversation_tokens=1000)  # 5000/1000 = 5.0 → level 4 (canned + handoff)
    graph = build_graph(tools, cfg)
    state = graph.invoke({"message": "Can I return ORD-RET?", "buyer_id": "BUY-1001"})

    assert state["governor"]["level"] == 4
    assert state["governor"]["canned_only"] is True
    assert state["meta"].get("degraded") == "canned_only"
    assert state["meta"]["responded_with_llm"] is False
    assert called["respond"] == 0            # respond LLM was skipped under degradation
    assert state["needs_human"] is True      # handoff_to_human step fired
