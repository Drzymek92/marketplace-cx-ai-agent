"""Module 6 — Phase 1: aggregate the metrics surface the improvement loop optimizes.

Two sources, deliberately kept separate because they have different coverage:

  * telemetry_store  — EVERY turn (the census). Source of truth for COST/VOLUME metrics: tokens,
    LLM calls, tier mix, cache-hit rate, escalation rate. Unbiased.
  * feedback_store   — only the REVIEWED subset (flagged turns + the ~5% audit sample). Source of
    QUALITY metrics: approval / correction / escalation rates. This sample is biased toward flagged
    (high-stakes) turns, so quality rates are reported with the reviewed count alongside, and
    `reviewer` is broken out so the audit slice (`sim-audit`) can be read separately from flagged
    reviews (`sim`) and real user signals (`user`).

Pure functions over lists of rows so they're trivially testable; `from_stores()` is the convenience
wrapper that reads the live DBs. Track A (tuning) and Track B (dataset export) build on this.
"""

from __future__ import annotations

from typing import Any

from scripts.budget import telemetry_store
from scripts.feedback import store as feedback_store


def _rate(num: int, denom: int) -> float:
    return round(num / denom, 4) if denom else 0.0


def aggregate_costs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost/volume metrics from telemetry rows (the full population)."""
    by_intent: dict[str, dict[str, Any]] = {}
    tier_mix: dict[str, int] = {}
    total_tokens = total_llm = cache_hits = escalations = 0

    for r in rows:
        intent = r.get("intent") or "unknown"
        tier = r.get("respond_tier") or "none"
        tokens = int(r.get("est_tokens") or 0)
        llm = int(r.get("llm_calls") or 0)

        b = by_intent.setdefault(intent, {"turns": 0, "tokens": 0, "llm_calls": 0,
                                          "cache_hits": 0, "needs_human": 0})
        b["turns"] += 1
        b["tokens"] += tokens
        b["llm_calls"] += llm
        b["cache_hits"] += int(bool(r.get("cache_hit")))
        b["needs_human"] += int(bool(r.get("needs_human")))

        tier_mix[tier] = tier_mix.get(tier, 0) + 1
        total_tokens += tokens
        total_llm += llm
        cache_hits += int(bool(r.get("cache_hit")))
        escalations += int(bool(r.get("needs_human")))

    for intent, b in by_intent.items():
        b["avg_tokens"] = round(b["tokens"] / b["turns"], 1) if b["turns"] else 0.0
        b["avg_llm_calls"] = round(b["llm_calls"] / b["turns"], 2) if b["turns"] else 0.0
        b["cache_hit_rate"] = _rate(b["cache_hits"], b["turns"])
        b["escalation_rate"] = _rate(b["needs_human"], b["turns"])

    turns = len(rows)
    return {
        "turns": turns,
        "total_tokens": total_tokens,
        "avg_tokens_per_turn": round(total_tokens / turns, 1) if turns else 0.0,
        "total_llm_calls": total_llm,
        "avg_llm_calls_per_turn": round(total_llm / turns, 2) if turns else 0.0,
        "cache_hit_rate": _rate(cache_hits, turns),
        "escalation_rate": _rate(escalations, turns),
        "tier_mix": tier_mix,
        "by_intent": by_intent,
    }


def aggregate_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Quality metrics from feedback rows (the reviewed sample only)."""
    by_intent: dict[str, dict[str, Any]] = {}
    by_reviewer: dict[str, int] = {}
    approvals = corrections = escalations = 0

    for r in rows:
        intent = r.get("intent") or "unknown"
        sig = r.get("signal_type")
        rating = r.get("rating")

        b = by_intent.setdefault(intent, {"reviewed": 0, "approvals": 0, "corrections": 0,
                                          "escalations": 0})
        b["reviewed"] += 1
        if rating == 1:
            b["approvals"] += 1
            approvals += 1
        if sig == "correction":
            b["corrections"] += 1
            corrections += 1
        if sig == "escalation":
            b["escalations"] += 1
            escalations += 1

        by_reviewer[r.get("reviewer") or "unknown"] = by_reviewer.get(r.get("reviewer") or "unknown", 0) + 1

    for intent, b in by_intent.items():
        b["approval_rate"] = _rate(b["approvals"], b["reviewed"])
        b["correction_rate"] = _rate(b["corrections"], b["reviewed"])

    reviewed = len(rows)
    return {
        "reviewed": reviewed,
        "approval_rate": _rate(approvals, reviewed),
        "correction_rate": _rate(corrections, reviewed),
        "escalation_rate": _rate(escalations, reviewed),
        "by_reviewer": by_reviewer,
        "by_intent": by_intent,
    }


def summarize(telemetry_rows: list[dict[str, Any]], feedback_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Combined view: cost (census) + quality (sample), with per-intent merge and a coverage note."""
    cost = aggregate_costs(telemetry_rows)
    quality = aggregate_quality(feedback_rows)

    per_intent: dict[str, Any] = {}
    for intent in set(cost["by_intent"]) | set(quality["by_intent"]):
        per_intent[intent] = {
            "cost": cost["by_intent"].get(intent),
            "quality": quality["by_intent"].get(intent),
        }

    return {
        "cost": cost,
        "quality": quality,
        "per_intent": per_intent,
        "coverage": {
            "turns": cost["turns"],
            "reviewed": quality["reviewed"],
            "review_rate": _rate(quality["reviewed"], cost["turns"]),
            "note": "cost = all turns (telemetry census); quality = reviewed sample only "
                    "(flagged + audit), biased toward flagged turns.",
        },
    }


def from_stores() -> dict[str, Any]:
    """Aggregate over the live telemetry + feedback DBs."""
    return summarize(telemetry_store.all_records(), feedback_store.all_records())
