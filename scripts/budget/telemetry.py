"""Per-turn telemetry (Module 7 + Module 9).

Captures what a turn cost and how it was served. This is a hard dependency for Module 5
(feedback records carry tokens + model_tier) and Module 6 (the improvement loop optimizes
for cost as well as quality), so it is captured from the first agent turn onward.

Token figures are estimates (no per-call usage from the gateway here) — good enough to compare
turns and prove the cascade/cache savings; a production build would read real usage.
"""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    # ~4 chars per token; floor at word count. Same heuristic as prompt_compressor.
    if not text:
        return 0
    return max(len(text.split()), len(text) // 4)


def build_record(state: dict[str, Any]) -> dict[str, Any]:
    meta = state.get("meta", {}) or {}
    classify_mode = meta.get("classify_mode")        # "triage" | "llm" | "faq"
    responded_with_llm = bool(meta.get("responded_with_llm"))

    llm_calls = 0
    if classify_mode == "llm":
        llm_calls += 1
    if responded_with_llm:
        llm_calls += 1

    cache_hit = classify_mode == "faq" or bool(meta.get("cache"))  # static FAQ or semantic-cache hit
    prompt_tokens = estimate_tokens(state.get("message", ""))
    answer_tokens = estimate_tokens(state.get("answer", ""))

    return {
        "intent": state.get("intent"),
        "llm_calls": llm_calls,
        "classify_mode": classify_mode,
        "classify_tier": meta.get("classify_tier"),
        "respond_tier": meta.get("respond_tier"),
        "cache_hit": cache_hit,
        "tools_used": state.get("tool_calls", []),
        "rule_version": meta.get("rule_version"),
        "needs_human": state.get("needs_human", False),
        "est_tokens": prompt_tokens + answer_tokens,
    }
