"""Module 6 — Phase 3: Track A auto-tuning + eval-gated promotion (the loop closing).

Track A is the *automatic* improvement track: it proposes config-only changes (candidates), evaluates
each against the frozen baseline, and promotes one ONLY if it beats the current config on quality
without blowing the token budget. No weight training — promotion is a `config.yaml` write, so a tuning
change is auditable and reversible.

Candidate generators here propose changes to **config-driven knobs only**. The shipped generator
regenerates the classifier's few-shot exemplars from `rating=+1` feedback turns (joining feedback →
history for the message). Business RULE thresholds (refund window, commission %) are deliberately NOT
auto-nudged — changing those automatically is a correctness/compliance risk; they stay human-gated.

Eval runs the real agent, which persists telemetry/feedback/history, so every eval here runs inside
`isolated_stores()` — a throwaway store set — to keep the production census unskewed.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import yaml

from scripts.agent.graph import build_graph
from scripts.agent.tools import AgentTools
from scripts.budget import telemetry_store
from scripts.config.loader import Config, load_config
from scripts.feedback import store as feedback_store
from scripts.improvement import eval as ev
from scripts.logger import get_logger
from scripts.profile import store as profile_store

logger = get_logger("improvement")

_CONFIG_YAML = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_CONFIG_EXAMPLE = _CONFIG_YAML.parent / "config.example.yaml"


# ─── patch helpers ───
def _deep_merge(base: dict, patch: dict) -> dict:
    out = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def apply_patch(cfg: Config, patch: dict) -> Config:
    """Return a new Config with the patch deep-merged onto it (for evaluating a candidate)."""
    return Config(**_deep_merge(cfg.model_dump(), patch))


# ─── candidate generators (config-driven knobs only) ───
def generate_fewshot_candidate(feedback_rows: list[dict], *, max_examples: int = 6) -> Optional[dict]:
    """Build a few-shot candidate from approved (`rating=+1`) feedback turns: join each to its
    history message and use (message → intent) as a classifier exemplar. None if no material."""
    examples: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for fb in feedback_rows:
        if fb.get("rating") != 1:
            continue
        turn_id, intent = fb.get("turn_id"), fb.get("intent")
        if not turn_id or not intent or intent == "unknown":
            continue
        turn = profile_store.get_turn(turn_id)
        if not turn or not turn.get("message"):
            continue
        key = (turn["message"], intent)
        if key in seen:
            continue
        seen.add(key)
        examples.append({"message": turn["message"], "intent": intent})
        if len(examples) >= max_examples:
            break
    if not examples:
        return None
    return {"label": "fewshot-from-feedback", "patch": {"classify": {"fewshot": examples}}}


# ─── eval isolation ───
@contextmanager
def isolated_stores(base_dir: str) -> Iterator[None]:
    """Point the profile/feedback/telemetry stores at a throwaway dir for the duration, then restore.
    Keeps eval traffic (the real agent runs) out of the production census."""
    saved = (str(profile_store._db_path), str(feedback_store._db_path), str(telemetry_store._db_path))
    try:
        profile_store.configure(str(Path(base_dir) / "profiles.db")); profile_store.init_db(force=True)
        feedback_store.configure(str(Path(base_dir) / "feedback.db")); feedback_store.init_db(force=True)
        telemetry_store.configure(str(Path(base_dir) / "telemetry.db")); telemetry_store.init_db(force=True)
        yield
    finally:
        profile_store.configure(saved[0])
        feedback_store.configure(saved[1])
        telemetry_store.configure(saved[2])


def _answer_with_cfg(cfg: Config, tools: AgentTools) -> Callable[..., dict]:
    """An answer_fn bound to a specific Config (so we can eval a candidate, not the default config)."""
    graph = build_graph(tools, cfg)

    def answer_fn(message: str, buyer_id: Optional[str] = None, tools: Any = None) -> dict:
        return graph.invoke({"message": message, "buyer_id": buyer_id})

    return answer_fn


# ─── promotion write ───
def write_promotion(patch: dict, config_path: Optional[str] = None) -> str:
    """Deep-merge the promoted patch into config.yaml (seeding from config.example.yaml if absent),
    preserving unmodeled sections (improvement, privacy, …) that the pydantic model doesn't round-trip."""
    path = Path(config_path) if config_path else _CONFIG_YAML
    base: dict = {}
    if path.exists():
        base = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    elif _CONFIG_EXAMPLE.exists():
        base = yaml.safe_load(_CONFIG_EXAMPLE.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(base, patch)
    path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    logger.info(f"promotion written to {path}")
    return str(path)


# ─── the cycle ───
def run_cycle(
    *,
    tools: Optional[AgentTools] = None,
    cases: Optional[list[dict]] = None,
    candidates: Optional[list[dict]] = None,
    min_delta: float = ev.DEFAULT_MIN_DELTA,
    max_avg_tokens: Optional[float] = None,
    write: bool = False,
    config_path: Optional[str] = None,
    eval_cfg_fn: Optional[Callable[[Config], dict]] = None,
) -> dict[str, Any]:
    """One Track-A cycle: baseline-eval the current config, eval each candidate, promote the best
    candidate that clears the gate (and write config.yaml if `write`). `eval_cfg_fn` is injectable
    for tests; by default it runs the real eval inside isolated stores.

    Returns a report: baseline metrics, each candidate's metrics + gate decision, and what (if
    anything) was promoted.
    """
    current = load_config()
    feedback_rows = feedback_store.all_records()

    if candidates is None:
        candidates = [c for c in (generate_fewshot_candidate(feedback_rows),) if c]

    if eval_cfg_fn is None:
        tools = tools or AgentTools()
        cases = cases or ev.load_baseline()

        def eval_cfg_fn(cfg: Config) -> dict:
            with isolated_stores(tempfile.mkdtemp(prefix="evalcfg_")):
                return ev.run_eval(cases, tools=tools, answer_fn=_answer_with_cfg(cfg, tools))

    baseline = eval_cfg_fn(current)
    results: list[dict] = []
    best: Optional[dict] = None
    for cand in candidates:
        metrics = eval_cfg_fn(apply_patch(current, cand["patch"]))
        decision = ev.compare(baseline, metrics, min_delta=min_delta, max_avg_tokens=max_avg_tokens)
        row = {"label": cand["label"], "patch": cand["patch"], "metrics": metrics, "decision": decision}
        results.append(row)
        logger.info(f"candidate '{cand['label']}': {decision['reason']} → "
                    f"{'PROMOTE' if decision['promote'] else 'reject'}")
        if decision["promote"] and (
            best is None or (metrics.get("overall_accuracy") or 0) > (best["metrics"].get("overall_accuracy") or 0)
        ):
            best = row

    promoted = None
    if best and write:
        write_promotion(best["patch"], config_path)
        promoted = best["label"]

    return {"baseline": baseline, "candidates": results, "promoted": promoted,
            "feedback_rows": len(feedback_rows)}
