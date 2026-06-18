"""Seller commission rule (Allegro "prowizje"): per-category rate on GMV + a flat
transaction fee. All money math uses Decimal, quantized to cents (half-up).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from scripts.config.loader import RulesConfig
from scripts.rules.models import OrderView

_CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass
class ItemCommission:
    category: str
    gmv: Decimal
    rate: Decimal
    commission: Decimal


@dataclass
class CommissionBreakdown:
    currency: str
    items: list[ItemCommission]
    items_commission: Decimal
    transaction_fee: Decimal
    total_commission: Decimal
    rule_version: str


def compute_commission(order: OrderView, config: RulesConfig) -> CommissionBreakdown:
    c = config.commission
    items: list[ItemCommission] = []
    running = Decimal("0.00")
    for li in order.line_items:
        rate = Decimal(str(c.per_category_pct.get(li.category, c.default_pct)))
        gmv = li.unit_amount * li.quantity
        commission = _money(gmv * rate)
        items.append(ItemCommission(li.category, _money(gmv), rate, commission))
        running += commission
    fee = _money(Decimal(str(c.transaction_fee_pln)))
    return CommissionBreakdown(order.currency, items, _money(running), fee,
                               _money(running + fee), config.version)
