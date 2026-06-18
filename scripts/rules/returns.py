"""Returns eligibility rule.

Demo simplification: the 14-day window is measured from placed_at (we don't track a
separate delivered_at). Thresholds come from config so the improvement loop can tune them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from scripts.config.loader import RulesConfig
from scripts.rules.models import OrderView


@dataclass
class ReturnDecision:
    returnable: bool
    free_return: bool
    window_days: int
    days_since_order: int
    excluded_categories: list[str]
    reasons: list[str]
    rule_version: str


def is_returnable(order: OrderView, config: RulesConfig, now: Optional[datetime] = None) -> ReturnDecision:
    now = now or datetime.now(timezone.utc)
    r = config.returns
    blocked = set(r.non_returnable_categories)

    days = (now.date() - order.placed_at.date()).days
    delivered = order.status == "DELIVERED"
    within = days <= r.window_days
    excluded = sorted(order.categories & blocked)
    has_returnable_item = any(li.category not in blocked for li in order.line_items)

    returnable = delivered and within and has_returnable_item
    free = bool(r.smart_free_return and order.buyer_smart and returnable)

    reasons = [
        "order delivered" if delivered else f"order status {order.status} (must be DELIVERED)",
        f"within {r.window_days}-day window ({days}d elapsed)" if within
        else f"outside {r.window_days}-day window ({days}d elapsed)",
    ]
    if excluded:
        reasons.append("non-returnable categories: " + ", ".join(excluded))
    if returnable:
        reasons.append("free return (Smart! member)" if free else "standard paid return")

    return ReturnDecision(returnable, free, r.window_days, days, excluded, reasons, config.version)
