"""Module 4 tests — profile store, budget-aware context builder, and graph integration.

The store + context layer are tested directly; graph integration asserts that the personalization
context actually reaches the LLM prompts and that every turn is recorded to history.
"""

from __future__ import annotations

import pytest

from scripts.agent.graph import answer
from scripts.agent.tools import AgentTools
from scripts.config.loader import Config
from scripts.profile import context as ctx
from scripts.profile import store


# --- store -------------------------------------------------------------------------
def test_seed_profiles_present(fresh_profile_db):
    p = store.get_profile("BUY-1001")
    assert p and p["smart_status"] is True and p["locale"] == "pl"
    assert p["recent_issues"] == ["late_delivery"]
    assert store.get_profile("BUY-9999") is None


def test_upsert_and_summary(fresh_profile_db):
    store.upsert_profile("BUY-1002", tone_pref="formal", recent_issues=["refund_delay"])
    assert store.get_profile("BUY-1002")["recent_issues"] == ["refund_delay"]
    store.set_summary("BUY-1002", "Asked twice about a delayed refund.")
    assert "delayed refund" in store.get_profile("BUY-1002")["summary"]


def test_history_append_and_recent(fresh_profile_db):
    for i in range(3):
        store.append_turn("BUY-1001", f"msg {i}", "order_status", f"ans {i}")
    assert store.count_turns("BUY-1001") == 3
    recent = store.recent_turns("BUY-1001", 2)
    assert [t["message"] for t in recent] == ["msg 1", "msg 2"]  # chronological, last 2


# --- context builder respects the budget -------------------------------------------
def _cfg(**budget_ctx) -> Config:
    cfg = Config()
    for k, v in budget_ctx.items():
        setattr(cfg.budget.context, k, v)
    return cfg


def test_profile_fields_filtered_to_config(fresh_profile_db):
    cfg = _cfg(profile_fields=["locale"])  # only locale allowed
    c = ctx.build_context("BUY-1001", cfg)
    assert c["profile"] == {"locale": "pl"}  # smart_status / recent_issues dropped


def test_history_capped_to_max_turns(fresh_profile_db):
    for i in range(10):
        store.append_turn("BUY-1003", f"m{i}", "faq", "a")
    c = ctx.build_context("BUY-1003", _cfg(max_history_turns=3))
    assert len(c["history_turns"]) == 3
    assert [t["message"] for t in c["history_turns"]] == ["m7", "m8", "m9"]


def test_no_buyer_id_yields_empty_context(fresh_profile_db):
    c = ctx.build_context(None)
    assert c == {"profile": None, "history_turns": [], "history_summary": None}


def test_modules_toggle_off_disables_personalization(fresh_profile_db):
    cfg = Config()
    cfg.modules.user_profile = False
    cfg.modules.conversation_history = False
    c = ctx.build_context("BUY-1001", cfg)
    assert c["profile"] is None and c["history_turns"] == []


def test_rolling_summary_triggers_over_threshold(fresh_profile_db, monkeypatch):
    # deterministic fallback summary (no LLM): force the import to fail inside _summarize
    monkeypatch.setattr("scripts.llm_client.llm_call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    cfg = _cfg(max_history_turns=2, summarize_history_above_turns=3)
    for i in range(5):
        ctx.record_turn("BUY-1002", f"m{i}", "return_check", "a", cfg)
    prof = store.get_profile("BUY-1002")
    assert prof["summary"] is not None
    assert "return_check" in prof["summary"]  # older turns folded into the tally


def test_summary_is_batched_not_run_every_turn(fresh_profile_db, monkeypatch):
    """Cost guarantee: the summarizer fires roughly once per batch, not on every turn past the
    threshold, and each aged-out turn is folded exactly once (watermark advances)."""
    calls = {"n": 0}
    real_summarize = ctx._summarize

    def spy(turns, profile):
        calls["n"] += 1
        return real_summarize(turns, profile)

    monkeypatch.setattr(ctx, "_summarize", spy)
    # deterministic fallback (no live LLM) so the test is offline and fast
    monkeypatch.setattr("scripts.llm_client.llm_call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))

    cfg = _cfg(max_history_turns=2, summarize_history_above_turns=3)
    for i in range(12):
        ctx.record_turn("BUY-1002", f"m{i}", "faq", "a", cfg)

    # 12 turns, window=2 → 10 age out, folded in batches of 3 → at most ceil(10/3)=4 summary
    # calls. The old every-turn code would have summarized on all 9 turns past the threshold.
    assert calls["n"] <= 4
    prof = store.get_profile("BUY-1002")
    assert prof["summary"] is not None
    assert prof["summary_through"] > 0  # watermark advanced; folded turns won't be re-summarized


def test_record_turn_returns_id_with_summarization_off(fresh_profile_db):
    # Regression: with summarization disabled, record_turn must still return the persisted turn id
    # (it once returned None here), or feedback can't be linked back to its turn for the loop.
    cfg = _cfg(summarize_history_above_turns=0)
    tid = ctx.record_turn("BUY-1002", "hello", "faq", "hi", cfg)
    assert tid is not None
    assert store.recent_turns("BUY-1002", 5)[0]["turn_id"] == tid


def test_format_for_prompt_compact(fresh_profile_db):
    block = ctx.format_for_prompt({
        "profile": {"locale": "pl", "tone_pref": "casual"},
        "history_summary": "Prior refund issue.",
        "history_turns": [{"intent": "order_status", "message": "where is it"}],
    })
    assert "USER PROFILE: locale=pl" in block
    assert "EARLIER CONVERSATION" in block
    assert "RECENT TURNS" in block

    assert ctx.format_for_prompt({}) == ""  # nothing to inject -> no tokens


# --- graph integration -------------------------------------------------------------
@pytest.fixture()
def tools():
    from tests.test_agent import FakeClient
    return AgentTools(client=FakeClient())


def test_context_reaches_respond_prompt(fresh_profile_db, tools, monkeypatch):
    seen = {}

    def fake_json(prompt, system=None, model=None, **kw):
        return {"intent": "order_status", "order_id": "ORD-RET", "confidence": 0.9}

    def fake_call(prompt, system=None, model=None, **kw):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr("scripts.llm_client.llm_json", fake_json)
    monkeypatch.setattr("scripts.llm_client.llm_call", fake_call)

    answer("where is my order ORD-RET", buyer_id="BUY-1001", tools=tools)
    assert "USER PROFILE" in seen["prompt"]
    assert "smart_status=True" in seen["prompt"]


def test_turn_recorded_for_every_path(fresh_profile_db, tools, monkeypatch):
    # FAQ path takes zero LLM calls but must still be recorded.
    answer("What is your return policy?", buyer_id="BUY-1003", tools=tools)
    turns = store.recent_turns("BUY-1003", 5)
    assert len(turns) == 1
    assert turns[0]["intent"] == "faq"
