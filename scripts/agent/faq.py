"""Static FAQ canned answers — order-independent policy questions.

Matched BEFORE any LLM call. A hit returns a curated answer with zero LLM calls, which both
saves tokens on repeat policy traffic and gives a better reply than the LLM asking for an
order id on a general question.

Safe to build now (deferred general/semantic cache is not): these answers depend on neither
the buyer nor any order, so there is no personalization or staleness/invalidation concern.
Matching is intentionally conservative — multi-word phrases, and never when an ORD-#### is
present (that is an order-specific question, not policy).
"""

from __future__ import annotations

from scripts.agent.triage import find_order_id

# Each entry: canned answer + the phrases that should trigger it (all lowercased).
_FAQS = [
    {
        "answer": (
            "You can return most items within 14 days of delivery, as long as they're unused and "
            "in original condition. Smart! members get free returns. A few categories (perishable, "
            "personalized, and unsealed digital items) can't be returned."
        ),
        "phrases": ["return policy", "how do returns work", "how long to return",
                    "how long do i have to return", "can i return things", "what is your return"],
    },
    {
        "answer": (
            "Seller commission is a per-category percentage of the sale value plus a small flat "
            "transaction fee. For example electronics is 8% and fashion is 12%, with a 1.00 PLN fee "
            "per transaction. Give me an order number (ORD-####) for an exact breakdown."
        ),
        "phrases": ["how is commission", "how is the commission", "how are fees", "commission rates",
                    "what are the seller fees", "how do commissions work"],
    },
    {
        "answer": (
            "Smart! is the membership that gives free delivery and free returns on eligible orders. "
            "Order status and eligibility may differ for Smart! vs non-Smart! buyers."
        ),
        "phrases": ["what is smart", "what's smart", "smart membership", "how does smart work"],
    },
    {
        "answer": (
            "If something's wrong with an order you can open a dispute (within 30 days) or rely on "
            "buyer protection on paid orders. Tell me the order number (ORD-####) and what happened "
            "and I'll check eligibility."
        ),
        "phrases": ["how do disputes work", "what is buyer protection", "how does buyer protection",
                    "what protections do i have"],
    },
]


def lookup(message: str) -> str | None:
    """Return a canned answer if the message is a policy FAQ with no specific order, else None."""
    if find_order_id(message):
        return None  # order-specific question — not a policy FAQ
    text = message.lower()
    for entry in _FAQS:
        if any(phrase in text for phrase in entry["phrases"]):
            return entry["answer"]
    return None
