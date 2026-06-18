"""Module 6 — Phase 4: Track B dataset curation (export only).

Turns captured HITL feedback into preference/instruction datasets, joining each feedback row back to
its turn (message + the agent's original answer) via profile.store.get_turn:

  * DPO preference pairs  — from `correction` signals: {prompt, chosen=reviewer correction,
    rejected=agent's original answer}. The shape DPO/RLHF tuning consumes.
  * SFT instruction pairs — from approved (`rating=+1`) turns: {prompt, completion=agent answer}.

This is **export only** (Option B): the actual weight fine-tune is gated by `improvement.sft.enabled`
(off by default) and runs externally on open weights — the gateway is inference-only. The export itself
always runs; it produces the artifacts a human would review before any external tuning.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from scripts.feedback import store as feedback_store
from scripts.logger import get_logger
from scripts.profile import store as profile_store

logger = get_logger("improvement")

_OUTPUTS = Path(__file__).resolve().parents[2] / "scripts" / "outputs"


def build_dpo_pairs(feedback_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preference pairs from correction signals: the reviewer's correction is `chosen`, the agent's
    original answer is `rejected`."""
    pairs: list[dict[str, Any]] = []
    for fb in feedback_rows:
        if fb.get("signal_type") != "correction" or not fb.get("correction"):
            continue
        turn = profile_store.get_turn(fb["turn_id"]) if fb.get("turn_id") else None
        if not turn or not turn.get("message"):
            continue
        pairs.append({
            "prompt": turn["message"],
            "chosen": fb["correction"],
            "rejected": turn.get("answer") or "",
            "intent": fb.get("intent"),
        })
    return pairs


def build_sft_pairs(feedback_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Instruction pairs from approved turns: the agent's approved answer is the target completion."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for fb in feedback_rows:
        if fb.get("rating") != 1:
            continue
        turn = profile_store.get_turn(fb["turn_id"]) if fb.get("turn_id") else None
        if not turn or not turn.get("message") or not turn.get("answer"):
            continue
        key = (turn["message"], turn["answer"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"prompt": turn["message"], "completion": turn["answer"], "intent": fb.get("intent")})
    return out


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def export_datasets(out_dir: Optional[str] = None,
                    feedback_rows: Optional[list[dict[str, Any]]] = None,
                    timestamp: Optional[str] = None) -> dict[str, Any]:
    """Build + write the DPO and SFT JSONL artifacts to scripts/outputs/ (timestamped). Returns the
    built rows, the file paths, and counts. Export always runs; the fine-tune is gated elsewhere."""
    rows = feedback_rows if feedback_rows is not None else feedback_store.all_records()
    dpo = build_dpo_pairs(rows)
    sft = build_sft_pairs(rows)

    out = Path(out_dir) if out_dir else _OUTPUTS
    out.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    dpo_path = out / f"dpo_pairs_{ts}.jsonl"
    sft_path = out / f"sft_dataset_{ts}.jsonl"
    _write_jsonl(dpo_path, dpo)
    _write_jsonl(sft_path, sft)

    logger.info(f"Track B export: {len(dpo)} DPO pairs → {dpo_path.name}; {len(sft)} SFT pairs → {sft_path.name}")
    return {
        "dpo": dpo, "sft": sft,
        "paths": {"dpo": str(dpo_path), "sft": str(sft_path)},
        "counts": {"dpo": len(dpo), "sft": len(sft)},
    }
