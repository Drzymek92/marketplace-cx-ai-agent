"""Deterministic triage — the cheap first rung of the classify cascade.

Returns an intent + order_id + a GRADED confidence with zero LLM calls. When confidence is
high enough (clear intent keyword), the graph trusts it and skips the LLM classify call;
otherwise it escalates to the LLM classifier. This is the token win: the easy majority of
turns never pay for an LLM classification.
"""

from __future__ import annotations

import re

_ORDER_RE = re.compile(r"\bORD-[A-Za-z0-9]+\b", re.IGNORECASE)

# STRONG intent keywords — specific enough that a hit is trusted at high confidence and the
# LLM classify call is skipped. Order matters: more specific / higher-stakes intents first, so
# "zwrot pieni" (refund) is decided before bare "zwrot" (return) and the two don't collide.
_KEYWORDS = [
    ("commission_query", ("commission", "prowizj", "seller fee", "seller's fee")),
    ("refund_eligibility", ("refund", "money back", "zwrot pieni")),
    ("dispute_eligibility", ("dispute", "spór", "claim against")),
    ("buyer_protection", ("buyer protection", "allegro protect", "ochron")),
    ("return_check", ("return", "send back", "zwróc", "zwrot")),
    ("order_status", ("where is my", "track my", "gdzie jest", "delivered yet")),
]

# WEAK signals — suggestive of an intent but far too common to trust on their own. "kiedy"
# ("when") and bare "status"/"gdzie"/"track" appear in plenty of unrelated questions ("kiedy
# dostanę odpowiedź?"), so matching one at 0.85 would confidently mis-route and starve the LLM.
# They earn the trust floor ONLY when a concrete order id grounds them; otherwise they stay
# deliberately below it so the cascade escalates to the LLM rather than guessing. Precision on
# the deflection path matters more than the deflection rate — a confident wrong answer costs a
# re-contact, which is far more expensive than the LLM call we saved.
_WEAK_SIGNALS = [
    ("order_status", ("status", "kiedy", "when ", "gdzie", "track", "where is")),
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

    for intent, keys in _WEAK_SIGNALS:
        if any(k in text for k in keys):
            # Grounded by an order id → trust it; bare weak signal → escalate to the LLM.
            return {"intent": intent, "order_id": order_id, "confidence": 0.8 if order_id else 0.55}

    if order_id:
        # An order id with no recognizable verb — probably a status check, but unsure.
        return {"intent": "order_status", "order_id": order_id, "confidence": 0.6}

    # No keyword, no order id — let the LLM decide (likely an FAQ or something novel).
    return {"intent": "faq", "order_id": None, "confidence": 0.5}
