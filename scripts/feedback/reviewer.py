"""Simulated human reviewer (Module 5).

The user chose a *simulated* reviewer over a real review queue/UI: the demo must run unattended
while still producing real-shaped HITL feedback for the improvement loop (Module 6). This is a
deterministic stand-in — no LLM call, so it's free and reproducible — that inspects a turn the
stakes gate flagged `needs_human` and emits the signal a sensible human reviewer would:

  * order-specific question but no order id was resolved  -> CORRECTION (ask for ORD-#### first)
  * the answer rests on a grounded rule-engine decision   -> RATING +1 (approve; the math is sound)
  * the responder fell back to a template (LLM failed)    -> ESCALATION (a human must actually answer)
  * otherwise                                              -> RATING +1 (approve)

It is intentionally conservative and explainable: every decision carries a `note` so the
aggregation in Module 6 (and a human auditing the demo) can see *why*.
"""

from __future__ import annotations

from typing import Any


def simulate(state: dict[str, Any]) -> dict[str, Any]:
    """Return the signal fields {signal_type, rating, correction, edit, note} for a flagged turn."""
    retrieved = state.get("retrieved", {}) or {}
    meta = state.get("meta", {}) or {}

    has_rule_decision = any(
        isinstance(d, dict) and d.get("rule_version") for d in retrieved.values()
    )
    missing_order = "_missing" in retrieved
    responded_with_llm = bool(meta.get("responded_with_llm", True))

    if missing_order:
        return {
            "signal_type": "correction",
            "rating": -1,
            "correction": "Ask the buyer for the order number (ORD-####) before escalating — the "
                          "question can't be decided without it.",
            "edit": None,
            "note": "no order id resolved for an order-specific, high-stakes intent",
        }

    if not responded_with_llm:
        return {
            "signal_type": "escalation",
            "rating": -1,
            "correction": None,
            "edit": None,
            "note": "responder fell back to a template (LLM unavailable) — needs a real human reply",
        }

    if has_rule_decision:
        return {
            "signal_type": "rating",
            "rating": 1,
            "correction": None,
            "edit": None,
            "note": "answer grounded in a versioned rule-engine decision — approved",
        }

    return {
        "signal_type": "rating",
        "rating": 1,
        "correction": None,
        "edit": None,
        "note": "no issues detected by the simulated reviewer — approved",
    }
