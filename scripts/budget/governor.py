"""Module 7 — token ceilings + graceful degradation ladder.

The cost governor's enforcement half. It meters cumulative spend (from the telemetry census) against
the configured ceilings and, as pressure rises, applies the configured degradation ladder in order
rather than failing:

    drop_to_cheaper_tier → shrink_context → cached_or_canned_only → handoff_to_human

Pressure = max(spent/ceiling) across the enabled token ceilings (per-conversation, daily) plus the
optional USD/day cap (when a token→USD rate is configured). The number of ladder steps applied scales
with how far over budget we are, so the agent gets progressively cheaper before it ever hard-stops.

Ceilings default to 0 (= unlimited), so with no config the governor is a no-op — existing behaviour
is unchanged until real caps are set (config.example.yaml sets them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from scripts.budget import telemetry_store
from scripts.config.loader import Config

LADDER_DEFAULT = ["drop_to_cheaper_tier", "shrink_context", "cached_or_canned_only", "handoff_to_human"]


@dataclass
class Directive:
    level: int = 0
    steps: list[str] = field(default_factory=list)
    cap_tier: Optional[str] = None      # max model tier allowed this turn (e.g. "fast")
    shrink_context: bool = False        # inject minimal/no profile+history
    canned_only: bool = False           # skip the respond LLM; serve cache/canned/templated
    force_human: bool = False           # route to human handoff
    spend: dict = field(default_factory=dict)
    reason: str = "within budget"


def _today_start_iso() -> str:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _pressure(cfg: Config, buyer_id: Optional[str]) -> tuple[float, dict]:
    b = cfg.budget
    conv = telemetry_store.tokens_for_buyer(buyer_id) if buyer_id else 0
    daily = telemetry_store.tokens_since(_today_start_iso())
    ratios = []
    if b.per_conversation_tokens > 0:
        ratios.append(conv / b.per_conversation_tokens)
    if b.daily_tokens > 0:
        ratios.append(daily / b.daily_tokens)
    usd = (daily / 1000.0) * b.usd_per_1k_tokens if b.usd_per_1k_tokens > 0 else 0.0
    if b.currency_cap_usd_per_day > 0 and b.usd_per_1k_tokens > 0:
        ratios.append(usd / b.currency_cap_usd_per_day)
    pressure = max(ratios) if ratios else 0.0
    return pressure, {
        "conversation_tokens": conv, "per_conversation_tokens": b.per_conversation_tokens,
        "daily_tokens": daily, "daily_tokens_cap": b.daily_tokens,
        "est_usd_today": round(usd, 4), "usd_cap": b.currency_cap_usd_per_day,
        "pressure": round(pressure, 3),
    }


def _level_for(pressure: float) -> int:
    """How many ladder steps to apply. Starts degrading proactively at 80% of a ceiling."""
    if pressure < 0.8:
        return 0
    if pressure < 1.0:
        return 1
    if pressure < 1.5:
        return 2
    if pressure < 2.0:
        return 3
    return 4


def decide(buyer_id: Optional[str], cfg: Config) -> Directive:
    pressure, spend = _pressure(cfg, buyer_id)
    level = _level_for(pressure)
    ladder = cfg.budget.degradation_when_over_budget or LADDER_DEFAULT
    steps = list(ladder[:level])
    d = Directive(level=level, steps=steps, spend=spend)
    d.cap_tier = "fast" if "drop_to_cheaper_tier" in steps else None
    d.shrink_context = "shrink_context" in steps
    d.canned_only = "cached_or_canned_only" in steps
    d.force_human = "handoff_to_human" in steps
    d.reason = f"pressure {pressure:.2f} → level {level} ({', '.join(steps) or 'none'})"
    return d
