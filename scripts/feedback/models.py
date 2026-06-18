"""Typed shapes for HITL feedback (Module 5).

A feedback record links one signal back to the turn it concerns AND snapshots that turn's
telemetry (tokens, model tier, tools, rule version) — so Module 6 can attribute a good/bad outcome
to a specific intent, tool, or rule without a join. The snapshot is deliberate: telemetry is an
estimate captured at turn time and we want the feedback to reflect what the turn actually cost then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# Canonical signal kinds (map onto the config `feedback.channels`):
#   rating     <- thumbs               (+1 / -1)
#   correction <- free_text_correction (the answer the reviewer would have given)
#   edit       <- agent_edit           (a tweak to the agent's wording)
#   escalation <- escalation_flag      (a confirmed needs-human flag, no auto-fix)
SignalType = Literal["rating", "correction", "edit", "escalation"]


@dataclass
class FeedbackRecord:
    intent: Optional[str]
    signal_type: SignalType
    reviewer: str                       # "sim" | "user" | a real reviewer id
    turn_id: Optional[int] = None       # profile.history.turn_id (None for anonymous turns)
    buyer_id: Optional[str] = None
    rating: Optional[int] = None        # +1 / -1, for signal_type="rating"
    correction: Optional[str] = None
    edit: Optional[str] = None
    note: Optional[str] = None          # why the (simulated) reviewer decided this
    # telemetry snapshot (so aggregation needs no join back to the turn)
    model_tier: Optional[str] = None
    tokens: int = 0
    tools_used: list[str] = field(default_factory=list)
    rule_version: Optional[str] = None
