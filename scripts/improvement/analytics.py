"""Module 9 — analytics surface for the performance dashboard.

A thin, presentation-oriented layer over `improvement/aggregate.py`. Aggregate already merges the
two data sources the system produces — the telemetry CENSUS (every turn) and the feedback SAMPLE
(reviewed turns) — per intent. This module re-frames that same data by **pipeline module** so the
dashboard can show how each cost-saving module (triage, FAQ, semantic cache, tier router, the rules
engine, the HITL gate, the eval harness) is actually pulling its weight, and where the next
optimization lives.

The headline optimization metric is honest and self-explaining: against a naive baseline of two LLM
calls per turn (one classify + one respond), how many calls did the cascade + cache actually avoid.

Everything is a pure function over row lists so it is trivially testable; `build_dashboard()` is the
convenience wrapper that reads the live stores (and an optional cached eval snapshot).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from scripts.budget import telemetry_store
from scripts.budget.router import _TIER_ORDER
from scripts.feedback import store as feedback_store
from scripts.improvement import aggregate

# The top routing tier(s) are the expensive ones — used to size the router's "premium" share.
_TOP_TIER_RANK = max(_TIER_ORDER.values()) if _TIER_ORDER else 0
_PREMIUM_TIERS = {t for t, rank in _TIER_ORDER.items() if rank >= _TOP_TIER_RANK}

# A turn that never reaches an LLM is served by one of these classify modes.
_ZERO_LLM_MODES = {"faq"}
_EVAL_SNAPSHOT_PATH = Path(__file__).resolve().parent / "eval_snapshot.json"

# Naive cost floor we measure savings against: classify + respond = 2 LLM calls per turn.
NAIVE_CALLS_PER_TURN = 2


def _rate(num: float, denom: float) -> float:
    return round(num / denom, 4) if denom else 0.0


def _pct(num: float, denom: float) -> float:
    return round(100 * num / denom, 1) if denom else 0.0


def kpis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline performance + cost-efficiency numbers over the telemetry census."""
    turns = len(rows)
    total_tokens = sum(int(r.get("est_tokens") or 0) for r in rows)
    total_llm = sum(int(r.get("llm_calls") or 0) for r in rows)
    zero_llm = sum(1 for r in rows if int(r.get("llm_calls") or 0) == 0)
    # semantic-cache hits only — telemetry conflates FAQ + cache under cache_hit, so exclude FAQ.
    cache_hits = sum(1 for r in rows if r.get("cache_hit") and r.get("classify_mode") != "faq")
    escalations = sum(1 for r in rows if r.get("needs_human"))
    # classify deflection: triage or FAQ resolved intent with no LLM classifier call.
    classify_deflected = sum(1 for r in rows if r.get("classify_mode") in ("triage", "faq"))

    naive_calls = NAIVE_CALLS_PER_TURN * turns
    calls_avoided = max(0, naive_calls - total_llm)

    return {
        "turns": turns,
        "total_tokens": total_tokens,
        "avg_tokens_per_turn": round(total_tokens / turns, 1) if turns else 0.0,
        "total_llm_calls": total_llm,
        "avg_llm_calls_per_turn": round(total_llm / turns, 2) if turns else 0.0,
        "zero_llm_rate": _rate(zero_llm, turns),
        "classify_deflection_rate": _rate(classify_deflected, turns),
        "cache_hit_rate": _rate(cache_hits, turns),
        "escalation_rate": _rate(escalations, turns),
        # The optimization story: calls the cascade + cache removed vs the naive 2-per-turn floor.
        "naive_llm_calls": naive_calls,
        "llm_calls_avoided": calls_avoided,
        "llm_reduction_rate": _rate(calls_avoided, naive_calls),
    }


def classify_path(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How intent was resolved: deterministic triage / static FAQ / LLM classifier."""
    out: dict[str, int] = {"triage": 0, "faq": 0, "llm": 0, "other": 0}
    for r in rows:
        mode = r.get("classify_mode")
        out[mode if mode in out else "other"] += 1
    return out


def tool_usage(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        for tool in r.get("tools_used") or []:
            counts[tool] = counts.get(tool, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def module_breakdown(tel_rows: list[dict[str, Any]], fb_rows: list[dict[str, Any]],
                     k: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-module operational view — what each module did and the optimization read on it.

    Each card: stable display shape so the page renders them uniformly.
      {key, name, layer, stat:{value,unit,label}, detail:[str], insight:str}
    """
    turns = k["turns"]
    paths = classify_path(tel_rows)
    tools = tool_usage(tel_rows)
    quality = aggregate.aggregate_quality(fb_rows)
    cost = aggregate.aggregate_costs(tel_rows)

    triage_n, faq_n, llm_n = paths["triage"], paths["faq"], paths["llm"]
    cache_n = sum(1 for r in tel_rows if r.get("cache_hit") and r.get("classify_mode") != "faq")
    rule_tools = {"check_return", "check_eligibility", "compute_order_commission",
                  "fetch_order", "list_buyer_orders"}
    rule_turns = sum(1 for r in tel_rows if set(r.get("tools_used") or []) & rule_tools)
    tier_mix = cost.get("tier_mix", {})
    # Premium share is measured against turns that actually picked a respond tier (exclude "none":
    # FAQ/cache turns that never reached the responder), so it answers "of LLM responses, how many
    # went premium" rather than being diluted by deflected turns.
    premium = sum(v for t, v in tier_mix.items() if t in _PREMIUM_TIERS)
    tier_total = sum(v for t, v in tier_mix.items() if t and t != "none")

    cards: list[dict[str, Any]] = [
        {
            "key": "triage",
            "name": "Triage classifier",
            "layer": "agent · deterministic",
            "stat": {"value": _pct(triage_n, turns), "unit": "%", "label": "of turns"},
            "detail": [f"{triage_n} turns classified with zero LLM",
                       "skips the LLM classifier when confident"],
            "insight": (f"Saved {triage_n} classifier calls. "
                        + ("Healthy first rung." if _rate(triage_n, turns) >= 0.3
                           else "Few turns deflected — tune triage patterns / confidence floor.")),
        },
        {
            "key": "faq",
            "name": "FAQ short-circuit",
            "layer": "agent · static",
            "stat": {"value": _pct(faq_n, turns), "unit": "%", "label": "of turns"},
            "detail": [f"{faq_n} turns answered from canned policy text",
                       "zero LLM calls — no classify, no respond"],
            "insight": (f"{faq_n} fully free turns. "
                        + ("Add more canned policy Qs to widen deflection." if faq_n <= turns * 0.1
                           else "Carrying real policy volume.")),
        },
        {
            "key": "cache",
            "name": "Semantic cache",
            "layer": "budget · embeddings",
            "stat": {"value": round(100 * k["cache_hit_rate"], 1), "unit": "%", "label": "hit rate"},
            "detail": [f"{cache_n} repeat/paraphrased questions served from cache",
                       "each hit skips the respond LLM call"],
            "insight": ("Cache is warming — hit rate climbs as repeat questions accrue."
                        if k["cache_hit_rate"] < 0.15
                        else "Strong reuse — cache is absorbing repeat traffic."),
        },
        {
            "key": "classify_llm",
            "name": "LLM classifier",
            "layer": "agent · LLM",
            "stat": {"value": _pct(llm_n, turns), "unit": "%", "label": "of turns"},
            "detail": [f"{llm_n} ambiguous turns escalated to the LLM classifier",
                       "the only turns that pay for classification"],
            "insight": ("Lean — most intent resolved deterministically."
                        if _rate(llm_n, turns) <= 0.4
                        else "High LLM-classify share — add few-shot exemplars to push work back to triage."),
        },
        {
            "key": "rules",
            "name": "Rules engine + tools",
            "layer": "rules · GraphQL",
            "stat": {"value": _pct(rule_turns, turns), "unit": "%", "label": "tool-backed"},
            "detail": [f"{rule_turns} turns grounded by a versioned rule/tool call"]
                      + ([f"top tool: {next(iter(tools))} ({tools[next(iter(tools))]}×)"] if tools else []),
            "insight": ("Most decisions are grounded in deterministic rules, not model guesses — "
                        "the correctness backbone."),
        },
        {
            "key": "router",
            "name": "Tier router",
            "layer": "budget · routing",
            "stat": {"value": _pct(premium, tier_total), "unit": "%", "label": "premium tier"},
            "detail": [f"tier mix: " + (", ".join(f"{t}:{v}" for t, v in tier_mix.items()) or "—")],
            "insight": ("Cheap-tier-dominant — premium reserved for high-stakes turns."
                        if _rate(premium, tier_total) <= 0.4
                        else "Premium-heavy — check whether low-stakes intents are over-tiered."),
        },
        {
            "key": "hitl",
            "name": "HITL gate + reviewer",
            "layer": "feedback · human-in-the-loop",
            "stat": {"value": round(100 * k["escalation_rate"], 1), "unit": "%", "label": "escalation rate"},
            "detail": [f"{quality['reviewed']} turns reviewed "
                       f"(approval {round(100*quality['approval_rate'],1)}%, "
                       f"correction {round(100*quality['correction_rate'],1)}%)",
                       "high-stakes + low-confidence turns auto-flagged for review"],
            "insight": ("Escalation in a healthy band." if 0.05 <= k["escalation_rate"] <= 0.4
                        else ("Very low escalation — confirm the stakes gate isn't under-flagging."
                              if k["escalation_rate"] < 0.05
                              else "High escalation — corrections here are the richest tuning signal.")),
        },
    ]
    return cards


def load_eval_snapshot(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    p = Path(path) if path else _EVAL_SNAPSHOT_PATH
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build(tel_rows: list[dict[str, Any]], fb_rows: list[dict[str, Any]],
          eval_snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Assemble the full dashboard payload from row lists (pure — no I/O)."""
    k = kpis(tel_rows)
    merged = aggregate.summarize(tel_rows, fb_rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": k,
        "modules": module_breakdown(tel_rows, fb_rows, k),
        "classify_path": classify_path(tel_rows),
        "tier_mix": merged["cost"]["tier_mix"],
        "tools": tool_usage(tel_rows),
        "per_intent": merged["per_intent"],
        "quality": merged["quality"],
        "coverage": merged["coverage"],
        "eval": eval_snapshot,
    }


def build_dashboard(eval_snapshot_path: Optional[str] = None) -> dict[str, Any]:
    """Read the live telemetry + feedback stores (and a cached eval snapshot, if any)."""
    return build(
        telemetry_store.all_records(),
        feedback_store.all_records(),
        load_eval_snapshot(eval_snapshot_path),
    )
