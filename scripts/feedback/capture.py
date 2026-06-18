"""Feedback capture orchestration (Module 5).

Two capture channels:
  * `capture_turn(state)` — AUTOMATIC. Called from the graph's finalize node. When feedback capture
    is enabled and the turn was flagged `needs_human`, it runs the simulated reviewer and persists
    a record. Best-effort by design (the caller wraps it) — a feedback failure must never break a turn.
  * `submit(...)` — EXTERNAL. The `/feedback` API channel for an end-user signal (thumbs/correction)
    on a turn that already happened.

Both produce a `FeedbackRecord` that snapshots the turn's telemetry (model tier, tokens, tools,
rule version) so Module 6 can aggregate without joining back to history.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from scripts.config.loader import Config, load_config
from scripts.feedback import reviewer, store
from scripts.feedback.models import FeedbackRecord


def _telemetry(state: dict[str, Any]) -> dict[str, Any]:
    return (state.get("meta") or {}).get("telemetry") or {}


def _should_audit(rate: float) -> bool:
    """Unbiased random-sample decision for a non-flagged turn. Isolated so tests can stub it."""
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return random.random() < rate


def capture_turn(state: dict[str, Any], cfg: Optional[Config] = None) -> Optional[int]:
    """Auto-capture a simulated review for a completed turn. Reviews every human-flagged turn, plus a
    random `feedback.audit_sample_rate` of NON-flagged turns (unbiased audit — COPC CX 7.0 / AWS A2I:
    review must not be limited to self-flagged cases). Returns the feedback row id, or None when
    nothing was captured."""
    cfg = cfg or load_config()
    if not cfg.modules.feedback_capture or not cfg.feedback.simulate_reviewer:
        return None

    flagged = bool(state.get("needs_human"))
    reviewer_id = "sim"
    if not flagged:
        if not _should_audit(cfg.feedback.audit_sample_rate):
            return None
        reviewer_id = "sim-audit"  # distinguishes proactive audit picks from flagged reviews

    signal = reviewer.simulate(state)
    tel = _telemetry(state)
    meta = state.get("meta") or {}
    rec = FeedbackRecord(
        intent=state.get("intent") or tel.get("intent"),
        signal_type=signal["signal_type"],
        reviewer=reviewer_id,
        turn_id=meta.get("turn_id"),
        buyer_id=state.get("buyer_id"),
        rating=signal.get("rating"),
        correction=signal.get("correction"),
        edit=signal.get("edit"),
        note=signal.get("note"),
        model_tier=tel.get("respond_tier"),
        tokens=int(tel.get("est_tokens") or 0),
        tools_used=tel.get("tools_used") or state.get("tool_calls") or [],
        rule_version=tel.get("rule_version"),
    )
    return store.record(rec)


def submit(
    signal_type: str,
    *,
    turn_id: Optional[int] = None,
    buyer_id: Optional[str] = None,
    intent: Optional[str] = None,
    reviewer_id: str = "user",
    rating: Optional[int] = None,
    correction: Optional[str] = None,
    edit: Optional[str] = None,
    model_tier: Optional[str] = None,
    tokens: int = 0,
    tools_used: Optional[list[str]] = None,
    rule_version: Optional[str] = None,
) -> int:
    """Persist an externally-supplied signal (e.g. an end-user thumbs/correction via /feedback)."""
    rec = FeedbackRecord(
        intent=intent, signal_type=signal_type, reviewer=reviewer_id, turn_id=turn_id,
        buyer_id=buyer_id, rating=rating, correction=correction, edit=edit,
        model_tier=model_tier, tokens=tokens, tools_used=tools_used or [], rule_version=rule_version,
    )
    return store.record(rec)
