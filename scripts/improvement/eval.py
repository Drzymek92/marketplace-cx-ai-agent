"""Module 6 — Phase 2: eval harness against a frozen baseline.

Scores the agent on a fixed set of cases so a Track A candidate (tuned prompts / thresholds /
few-shot / routing) can be promoted ONLY if it beats the current config on quality without blowing
the token budget (the architecture's eval gate). Three correctness axes per case — intent, the
rule-engine outcome, and whether the stakes gate fired appropriately — plus token cost.

Coverage note: the committed baseline uses time-STABLE expectations (see eval_baseline.json). The
harness runs the real `answer()` path, so it persists telemetry/feedback/history like any turn —
the orchestrator (Phase 3) should point those stores at a throwaway location before an eval run so
the production census isn't skewed by eval traffic. Tests isolate the stores via fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from scripts.agent.tools import AgentTools

_BASELINE_PATH = Path(__file__).resolve().parent / "eval_baseline.json"

# Primary quality metric the promotion gate compares on.
PRIMARY_METRIC = "overall_accuracy"
DEFAULT_MIN_DELTA = 0.02


def load_baseline(path: Optional[str] = None) -> list[dict[str, Any]]:
    p = Path(path) if path else _BASELINE_PATH
    return json.loads(p.read_text(encoding="utf-8"))["cases"]


def _match_rule(retrieved: dict[str, Any], expected: dict[str, Any]) -> bool:
    """True if any retrieved decision dict contains every expected field=value."""
    for data in (retrieved or {}).values():
        if isinstance(data, dict) and all(data.get(k) == v for k, v in expected.items()):
            return True
    return False


def score_case(state: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Score one finished turn against its expectations. Absent expectations are not scored (None)."""
    res: dict[str, Any] = {"id": case.get("id")}
    if "expected_intent" in case:
        res["intent_correct"] = state.get("intent") == case["expected_intent"]
    if "expected_needs_human" in case:
        res["needs_human_correct"] = bool(state.get("needs_human")) == bool(case["expected_needs_human"])
    if case.get("expected_rule"):
        res["rule_correct"] = _match_rule(state.get("retrieved", {}) or {}, case["expected_rule"])
    res["tokens"] = ((state.get("meta") or {}).get("telemetry") or {}).get("est_tokens", 0)
    return res


def aggregate_eval(per_case: list[dict[str, Any]]) -> dict[str, Any]:
    def axis(key: str) -> Optional[float]:
        vals = [c[key] for c in per_case if c.get(key) is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None

    flags: list[bool] = []
    for c in per_case:
        for k in ("intent_correct", "needs_human_correct", "rule_correct"):
            if c.get(k) is not None:
                flags.append(bool(c[k]))
    tokens = [int(c.get("tokens") or 0) for c in per_case]
    n = len(per_case)
    return {
        "n": n,
        "intent_accuracy": axis("intent_correct"),
        "needs_human_accuracy": axis("needs_human_correct"),
        "rule_accuracy": axis("rule_correct"),
        "overall_accuracy": round(sum(flags) / len(flags), 4) if flags else None,
        "avg_tokens": round(sum(tokens) / n, 1) if n else 0.0,
        "total_tokens": sum(tokens),
        "failures": [c["id"] for c in per_case
                     if any(c.get(k) is False for k in ("intent_correct", "needs_human_correct", "rule_correct"))],
        "per_case": per_case,
    }


def run_eval(cases: Optional[list[dict[str, Any]]] = None, tools: Optional[AgentTools] = None,
             answer_fn: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    """Run every baseline case through the agent and aggregate the scores."""
    if cases is None:
        cases = load_baseline()
    if answer_fn is None:
        from scripts.agent.graph import answer as answer_fn  # local import: avoid import cycle at module load
    tools = tools or AgentTools()
    per_case = [score_case(answer_fn(c["message"], buyer_id=c.get("buyer_id"), tools=tools), c) for c in cases]
    return aggregate_eval(per_case)


def compare(baseline: dict[str, Any], candidate: dict[str, Any], *,
            min_delta: float = DEFAULT_MIN_DELTA, max_avg_tokens: Optional[float] = None) -> dict[str, Any]:
    """The promotion gate: a candidate is promoted ONLY if it beats baseline on the primary metric
    by >= min_delta AND stays within the token budget (if one is given)."""
    base = baseline.get(PRIMARY_METRIC) or 0.0
    cand = candidate.get(PRIMARY_METRIC) or 0.0
    delta = round(cand - base, 4)
    within_budget = max_avg_tokens is None or candidate.get("avg_tokens", 0) <= max_avg_tokens
    beats = delta >= min_delta
    promote = beats and within_budget
    reasons = []
    reasons.append(f"{PRIMARY_METRIC} {base}→{cand} (Δ{delta:+}, need ≥{min_delta})")
    if max_avg_tokens is not None:
        reasons.append(f"avg_tokens {candidate.get('avg_tokens')} vs budget {max_avg_tokens} "
                       f"({'ok' if within_budget else 'OVER'})")
    return {
        "promote": promote,
        "accuracy_delta": delta,
        "beats_baseline": beats,
        "within_budget": within_budget,
        "reason": "; ".join(reasons),
    }
