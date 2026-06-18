"""Budget-aware personalization context (Module 4).

This is where personalization meets the token governor. Two jobs:

  build_context(buyer_id, cfg)  -> what to INJECT this turn, already trimmed to the budget:
      * profile filtered to `budget.context.profile_fields` (nothing the config didn't ask for)
      * the last `max_history_turns` turns verbatim
      * the rolling `summary` standing in for everything older

  record_turn(buyer_id, ...)    -> persist this turn; once at least
      `summarize_history_above_turns` turns have aged out of the verbatim window *since the last
      fold*, batch-fold exactly those new turns into the rolling summary. A per-profile watermark
      (`summary_through`) guarantees each turn is summarized once, so the summarizer LLM call fires
      roughly once per batch — not on every turn — keeping per-turn token cost bounded no matter
      how long the conversation runs.

Module toggles (`modules.user_profile`, `modules.conversation_history`) gate each half, so a
cheap/minimal run can switch personalization off entirely.
"""

from __future__ import annotations

from typing import Any, Optional

from scripts.config.loader import Config, load_config
from scripts.logger import get_logger
from scripts.profile import store

logger = get_logger("profile")

# config key (snake_case) -> profile dict key. They line up today, but keep the indirection
# so renaming a config-facing name doesn't silently drop a field.
_FIELD_MAP = {
    "smart_status": "smart_status",
    "locale": "locale",
    "tone_pref": "tone_pref",
    "recent_issues": "recent_issues",
}


def build_context(buyer_id: Optional[str], cfg: Optional[Config] = None) -> dict[str, Any]:
    """Assemble the (already budget-trimmed) personalization context for one turn."""
    cfg = cfg or load_config()
    ctx: dict[str, Any] = {"profile": None, "history_turns": [], "history_summary": None}
    if not buyer_id:
        return ctx

    if cfg.modules.user_profile:
        profile = store.get_profile(buyer_id)
        if profile:
            allowed = cfg.budget.context.profile_fields
            ctx["profile"] = {
                key: profile[src]
                for key, src in _FIELD_MAP.items()
                if key in allowed and profile.get(src) not in (None, [], "")
            }
            ctx["history_summary"] = profile.get("summary")

    if cfg.modules.conversation_history:
        ctx["history_turns"] = store.recent_turns(buyer_id, cfg.budget.context.max_history_turns)

    return ctx


def format_for_prompt(ctx: dict[str, Any]) -> str:
    """Compact text block for injection. Empty string when there's nothing to add (no tokens spent)."""
    parts: list[str] = []
    if ctx.get("profile"):
        fields = ", ".join(f"{k}={v}" for k, v in ctx["profile"].items())
        parts.append(f"USER PROFILE: {fields}")
    if ctx.get("history_summary"):
        parts.append(f"EARLIER CONVERSATION (summary): {ctx['history_summary']}")
    if ctx.get("history_turns"):
        lines = [f"- [{t['intent']}] user: {t['message']}" for t in ctx["history_turns"]]
        parts.append("RECENT TURNS:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def record_turn(
    buyer_id: Optional[str],
    message: str,
    intent: str,
    answer: str,
    cfg: Optional[Config] = None,
) -> Optional[int]:
    """Persist this turn and roll older turns into the summary once over the threshold.

    Returns the new history turn_id (so callers can link feedback to it), or None when history is
    off or the turn is anonymous.
    """
    cfg = cfg or load_config()
    if not buyer_id or not cfg.modules.conversation_history:
        return None

    turn_id = store.append_turn(buyer_id, message, intent, answer)

    batch = cfg.budget.context.summarize_history_above_turns
    if not batch:
        return
    keep = cfg.budget.context.max_history_turns
    profile = store.get_profile(buyer_id)
    watermark = int((profile or {}).get("summary_through") or 0)
    # only turns that have aged out of the verbatim window AND haven't been folded yet
    pending = store.turns_after(buyer_id, after_id=watermark, keep_recent=keep)
    if len(pending) >= batch:
        summary = _summarize(pending, profile)
        store.set_summary(buyer_id, summary, through_turn_id=pending[-1]["turn_id"])
        logger.info(
            f"profile[{buyer_id}]: folded {len(pending)} aged-out turns into summary "
            f"(watermark→{pending[-1]['turn_id']})"
        )
    return turn_id


def _summarize(turns: list[dict], profile: Optional[dict]) -> str:
    """Summarize older turns. LLM when available; deterministic intent-tally fallback otherwise.

    Cost note: this runs once per batch (when `summarize_history_above_turns` turns have aged out
    since the last fold), never on every turn — and only over the new, not-yet-folded turns, which
    it merges into the prior summary. The summary then replaces those turns in every future prompt,
    so it pays for itself.
    """
    prior = (profile or {}).get("summary")
    transcript = "\n".join(f"[{t['intent']}] {t['message']} -> {t['answer']}" for t in turns)
    try:
        from scripts.llm_client import llm_call

        sys = (
            "Summarize this customer's earlier support conversation in 2-3 sentences. "
            "Keep durable facts (recurring issues, preferences, unresolved problems); drop pleasantries. "
            "If a prior summary is given, merge it in rather than repeating it."
        )
        prompt = (f"PRIOR SUMMARY:\n{prior}\n\n" if prior else "") + f"OLDER TURNS:\n{transcript}"
        return llm_call(prompt, system=sys).strip()
    except Exception as exc:
        logger.warning(f"summary LLM failed ({exc}); using deterministic fallback")
        counts: dict[str, int] = {}
        for t in turns:
            counts[t["intent"]] = counts.get(t["intent"], 0) + 1
        tally = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        base = f"{len(turns)} earlier turns. Topics: {tally}."
        return f"{prior} {base}".strip() if prior else base
