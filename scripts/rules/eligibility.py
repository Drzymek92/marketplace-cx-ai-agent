"""Process eligibility rule (Allegro "kwalifikowalność procesów"): can this order enter
a given customer process — refund, dispute, or buyer protection (Allegro Protect)?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from scripts.config.loader import RulesConfig
from scripts.rules.models import OrderView
from scripts.rules.returns import is_returnable

PROCESSES = ("refund", "dispute", "buyer_protection")


@dataclass
class EligibilityDecision:
    process: str
    eligible: bool
    reasons: list[str]
    rule_version: str


def qualifies_for(process: str, order: OrderView, config: RulesConfig,
                  now: Optional[datetime] = None) -> EligibilityDecision:
    now = now or datetime.now(timezone.utc)
    days = (now.date() - order.placed_at.date()).days
    reasons: list[str] = []

    if process == "refund":
        ret = is_returnable(order, config, now=now)
        eligible = ret.returnable or order.status == "CANCELLED"
        reasons.append("refund requires a valid return or a cancelled order")
        reasons.extend(ret.reasons)
    elif process == "dispute":
        within = days <= config.eligibility.dispute_window_days
        status_ok = order.status in {"PAID", "SHIPPED", "DELIVERED"}
        eligible = status_ok and within
        reasons.append(f"dispute window {config.eligibility.dispute_window_days}d ({days}d elapsed)")
        if not status_ok:
            reasons.append(f"order status {order.status} not eligible for dispute")
    elif process == "buyer_protection":
        eligible = order.status in {"PAID", "PROCESSING", "SHIPPED", "DELIVERED"}
        reasons.append("Allegro Protect covers paid orders")
    else:
        raise ValueError(f"unknown process {process!r}; expected one of {PROCESSES}")

    return EligibilityDecision(process, eligible, reasons, config.version)
