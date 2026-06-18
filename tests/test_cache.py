"""Module 7 — semantic response cache (embeddings + local fallback).

Unit tests exercise both match paths (injected fake embeddings for the cosine path; the built-in
normalized-exact fallback when no embedder), eligibility, and rule_version invalidation. An
end-to-end test proves a repeat order-independent turn is served from cache with no respond LLM call.
"""

from __future__ import annotations

import pytest

from scripts.agent.graph import build_graph
from scripts.agent.tools import AgentTools
from scripts.budget import cache
from scripts.config.loader import Config
from tests.test_agent import FakeClient


# --- eligibility (mirrors FAQ safety: order-independent only) -----------------------
def test_eligibility_excludes_order_specific():
    assert cache.is_eligible("what is your return policy?") is True
    assert cache.is_eligible("can I return ORD-4001?") is False          # literal order id
    assert cache.is_eligible("how do returns work", order_id="ORD-1") is False


# --- normalized-exact fallback (no embedder) ---------------------------------------
def test_normalized_exact_fallback():
    cache.store("How do returns work?", "Within 14 days.", embed_fn=lambda t: None)
    # punctuation/case/whitespace differences still hit via normalization
    assert cache.lookup("how do   returns work", embed_fn=lambda t: None) == "Within 14 days."
    assert cache.lookup("what about commissions", embed_fn=lambda t: None) is None


# --- semantic (cosine) path with injected embeddings -------------------------------
def test_semantic_match_with_embeddings():
    vecs = {
        "how long to return something": [1.0, 0.0, 0.0],
        "what's the window for sending an item back": [0.99, 0.01, 0.0],  # near-parallel → high cosine
        "how are seller fees calculated": [0.0, 1.0, 0.0],                # orthogonal → miss
    }
    embed = lambda t: vecs[t]
    cache.store("how long to return something", "14 days.", embed_fn=embed)
    assert cache.lookup("what's the window for sending an item back", embed_fn=embed) == "14 days."
    assert cache.lookup("how are seller fees calculated", embed_fn=embed) is None


def test_rule_version_invalidates_stale_entry():
    cache.store("return policy", "old answer", rule_version="v1", embed_fn=lambda t: None)
    assert cache.lookup("return policy", rule_version="v1", embed_fn=lambda t: None) == "old answer"
    assert cache.lookup("return policy", rule_version="v2", embed_fn=lambda t: None) is None  # policy changed


# --- end-to-end: a repeat order-independent turn skips the respond LLM --------------
@pytest.fixture()
def tools():
    return AgentTools(client=FakeClient())


def test_repeat_question_served_from_cache(fresh_profile_db, tools, monkeypatch):
    calls = {"respond": 0}
    # ambiguous, order-independent message → not keyword-trusted → LLM classify says faq-ish unknown,
    # but we route it through respond by classifying as a non-faq intent with no order.
    monkeypatch.setattr("scripts.llm_client.llm_json",
                        lambda *a, **k: {"intent": "unknown", "order_id": None, "confidence": 0.9})
    monkeypatch.setattr("scripts.llm_client.llm_call",
                        lambda *a, **k: calls.__setitem__("respond", calls["respond"] + 1) or "Here is the policy.")
    # force the local (normalized-exact) cache path deterministically
    monkeypatch.setattr("scripts.budget.cache._default_embed", lambda t: None)

    cfg = Config()  # semantic_cache on by default; no buyer_id → no context block → cache-eligible
    graph = build_graph(tools, cfg)

    msg = "do you deliver to small towns"
    s1 = graph.invoke({"message": msg, "buyer_id": None})
    s2 = graph.invoke({"message": "do you   deliver to small towns!", "buyer_id": None})  # paraphrase/format

    assert calls["respond"] == 1                       # only the first turn paid for the LLM
    assert s2["meta"]["cache"] == "semantic"
    assert s2["meta"]["responded_with_llm"] is False
    assert s2["meta"]["telemetry"]["cache_hit"] is True
    assert s2["answer"] == "Here is the policy."
