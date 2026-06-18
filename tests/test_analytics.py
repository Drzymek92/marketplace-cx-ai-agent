"""Module 9 analytics tests — module-attribution + the LLM-savings headline.

Pure functions over synthetic telemetry/feedback rows (no I/O); one shape test on the full
`build()` payload so the dashboard contract doesn't drift.
"""

from __future__ import annotations

from scripts.improvement import analytics


def _tel(mode, llm, tier, tokens=50, cache=False, human=False, intent="order_status", tools=None):
    return {"classify_mode": mode, "llm_calls": llm, "respond_tier": tier, "est_tokens": tokens,
            "cache_hit": cache, "needs_human": human, "intent": intent, "tools_used": tools or []}


def _rows():
    return [
        _tel("faq", 0, None, cache=True, intent="faq"),                       # zero LLM, FAQ
        _tel("triage", 1, "bulk", tools=["check_return"], intent="return_check"),
        _tel("triage", 1, "fast", tools=["fetch_order"]),
        _tel("triage", 0, None, cache=True, intent="return_check"),           # semantic cache hit
        _tel("llm", 2, "reason", human=True, intent="refund_eligibility",
             tools=["check_eligibility"]),                                    # escalation, premium
    ]


def test_kpis_headline_savings():
    k = analytics.kpis(_rows())
    assert k["turns"] == 5
    assert k["total_llm_calls"] == 4
    # naive baseline = 2 calls/turn = 10; actual = 4; avoided = 6.
    assert k["naive_llm_calls"] == 10
    assert k["llm_calls_avoided"] == 6
    assert k["llm_reduction_rate"] == round(6 / 10, 4)
    # zero-LLM = the FAQ turn + the cache hit = 2/5.
    assert k["zero_llm_rate"] == round(2 / 5, 4)
    # classify deflection = triage(3) + faq(1) = 4/5.
    assert k["classify_deflection_rate"] == round(4 / 5, 4)
    # cache hit excludes FAQ → only the one semantic hit.
    assert k["cache_hit_rate"] == round(1 / 5, 4)
    assert k["escalation_rate"] == round(1 / 5, 4)


def test_kpis_empty():
    k = analytics.kpis([])
    assert k["turns"] == 0
    assert k["llm_calls_avoided"] == 0
    assert k["llm_reduction_rate"] == 0.0


def test_classify_path_counts():
    p = analytics.classify_path(_rows())
    assert p == {"triage": 3, "faq": 1, "llm": 1, "other": 0}


def test_cache_hit_excludes_faq():
    # A FAQ turn carries cache_hit=True in telemetry, but it's a canned answer, not the semantic
    # cache — the cache module's hit rate must not double-count it.
    k = analytics.kpis([_tel("faq", 0, None, cache=True, intent="faq")])
    assert k["cache_hit_rate"] == 0.0


def test_premium_share_only_top_tier():
    rows = _rows()
    cards = {c["key"]: c for c in analytics.module_breakdown(rows, [], analytics.kpis(rows))}
    # responded turns: bulk, fast, reason → premium (reason) = 1 of 3 = 33.3%.
    assert cards["router"]["stat"]["value"] == 33.3


def test_build_payload_shape():
    fb = [{"intent": "refund_eligibility", "signal_type": "correction", "reviewer": "sim", "rating": None},
          {"intent": "return_check", "signal_type": "rating", "reviewer": "user", "rating": 1}]
    d = analytics.build(_rows(), fb, eval_snapshot={"overall_accuracy": 0.8, "n": 4})
    assert set(d) >= {"kpis", "modules", "classify_path", "tier_mix", "per_intent",
                      "quality", "coverage", "eval", "generated_at"}
    assert len(d["modules"]) == 7
    assert all({"key", "name", "layer", "stat", "detail", "insight"} <= set(m) for m in d["modules"])
    assert d["eval"]["overall_accuracy"] == 0.8
    assert d["coverage"]["reviewed"] == 2
