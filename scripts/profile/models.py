"""Typed shapes for the profile/history layer.

Kept deliberately small: the profile is the slow-changing, curated set of fields worth
injecting into every turn; a turn is one user message + the agent's classified intent + reply.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class UserProfile(TypedDict, total=False):
    buyer_id: str
    smart_status: bool        # Smart! membership — drives free-return logic
    locale: str               # "pl" | "en"
    tone_pref: str            # "formal" | "casual" | ...
    recent_issues: list[str]  # durable issue tags promoted from history
    summary: Optional[str]    # rolling summary of older conversation turns
    updated_at: str


class HistoryTurn(TypedDict, total=False):
    turn_id: int
    buyer_id: str
    ts: str
    message: str
    intent: str
    answer: str
