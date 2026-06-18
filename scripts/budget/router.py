"""Model-tier routing + escalation policy (Module 7).

Mirrors config.budget.model_tiers / escalation. Kept here (not in the agent graph) so the
agent stays about *flow* and the governor owns *what it costs*. Module 7's later half (context
budgeter, caching, ceilings) plugs in around this once Module 4 exists.
"""

from __future__ import annotations

# Model per tier (any OpenAI-compatible gateway). Swap these ids for whatever your gateway serves;
# the routing logic only cares about the cheap→capable ordering, not the specific model names.
TIER_MODELS = {
    "fast": "gpt-4o-mini",
    "bulk": "gpt-4o-mini",
    "reason": "gpt-4o",
}

CLASSIFY_TIER = "fast"

# config.budget.escalation
HIGH_STAKES = {"refund_eligibility", "dispute_eligibility", "commission_query"}
CONFIDENCE_FLOOR = 0.55          # below this, escalate respond tier + flag for human review
TRIAGE_CONFIDENCE_FLOOR = 0.8    # at/above this, the cheap deterministic triage may skip LLM classify


_TIER_ORDER = {"fast": 0, "bulk": 1, "reason": 2}


def model_for(tier: str) -> str:
    return TIER_MODELS.get(tier, TIER_MODELS["bulk"])


def cap_tier(tier: str, max_tier: str | None) -> str:
    """Clamp a tier to a cheaper ceiling (governor degradation). No-op if max_tier is None."""
    if not max_tier:
        return tier
    return tier if _TIER_ORDER.get(tier, 1) <= _TIER_ORDER.get(max_tier, 2) else max_tier


def respond_tier(intent: str | None, needs_human: bool) -> str:
    """Cheapest tier that clears the task; escalate only on stakes or low confidence."""
    if needs_human or intent in HIGH_STAKES:
        return "reason"
    if intent in ("order_status", "faq"):
        return "fast"
    return "bulk"
