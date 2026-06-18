"""Shared state passed between graph nodes.

LangGraph threads one dict through every node; each node returns a partial dict that is
merged in. total=False so nodes only need to return the keys they set.
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # --- input ---
    message: str
    buyer_id: Optional[str]

    # --- governor (Module 7: ceilings + degradation directive) ---
    governor: dict[str, Any]    # {level, steps, cap_tier, shrink_context, canned_only, force_human, spend}

    # --- context (Module 4: profile + history, budget-trimmed) ---
    context: dict[str, Any]     # {profile, history_turns, history_summary}

    # --- classify ---
    intent: str
    order_id: Optional[str]
    process: Optional[str]
    confidence: float

    # --- retrieve ---
    retrieved: dict[str, Any]   # tool name -> ToolResult.data (or {"error": ...})
    tool_calls: list[str]       # telemetry: which tools fired, in order

    # --- stakes gate ---
    needs_human: bool
    hitl_reason: Optional[str]

    # --- respond ---
    answer: str
    meta: dict[str, Any]        # tiers used, rule_version, classifier mode, etc.
