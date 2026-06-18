"""Deterministic triage — the cheap first rung of the classify cascade.

Returns an intent + order_id + a GRADED confidence with zero LLM calls. When confidence is
high enough (clear intent keyword), the graph trusts it and skips the LLM classify call;
otherwise it escalates to the LLM classifier. This is the token win: the easy majority of
turns never pay for an LLM classification.
"""

from __future__ import annotations

import re

_ORDER_RE = re.compile(r"\bORD-[A-Za-z0-9]+\b", re.IGNORECASE)

# Intent keyword table. Order matters: more specific / higher-stakes intents first.
_KEYWORDS = [
    ("commission_query", ("commission", "prowizj", "seller fee", "seller's fee")),
    ("refund_eligibility", ("refund", "money back", "zwrot pieni")),
    ("dispute_eligibility", ("dispute", "spór", "claim against")),
    ("buyer_protection", ("buyer protection", "allegro protect", "ochron")),
    ("return_check", ("return", "send back", "zwróc", "zwrot")),
    ("order_status", ("status", "where is", "track", "gdzie", "kiedy", "delivered yet")),
]


def find_order_id(message: str) -> str | None:
    m = _ORDER_RE.search(message)
    return m.group(0).upper() if m else None


def deterministic_classify(message: str) -> dict:
    """{intent, order_id, confidence}. confidence is graded so the cascade can decide
    whether to trust this or escalate to the LLM."""
    text = message.lower()
    order_id = find_order_id(message)

    for intent, keys in _KEYWORDS:
        if any(k in text for k in keys):
            return {"intent": intent, "order_id": order_id, "confidence": 0.85}

    if order_id:
        # An order id with no recognizable verb — probably a status check, but unsure.
        return {"intent": "order_status", "order_id": order_id, "confidence": 0.6}

    # No keyword, no order id — let the LLM decide (likely an FAQ or something novel).
    return {"intent": "faq", "order_id": None, "confidence": 0.5}
