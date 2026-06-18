"""Populate the telemetry + feedback stores with a realistic spread of turns so the analytics
dashboard (Module 9) shows believable, non-empty numbers on first load.

Every turn is driven through the REAL agent (`graph.answer`), so the telemetry census, the HITL
feedback sample, and the per-module attribution are all produced by the live pipeline — not
synthesized. The message mix deliberately exercises every classify path:

  * FAQ short-circuits   (zero LLM)            — "what's your return policy"
  * deterministic triage (order-specific)      — "can I return ORD-4001"
  * high-stakes intents  (auto-flagged → HITL) — refund / dispute / buyer-protection
  * commission + status  across several buyers

The agent falls back to deterministic/templated answers if the LLM gateway is unreachable, so
this runs offline too (LLM-call counts simply reflect whichever path each turn took).

Usage:
  python scripts/improvement/seed_demo.py            # reset census + feedback, seed a fresh run
  python scripts/improvement/seed_demo.py --keep     # append to existing data
  python scripts/improvement/seed_demo.py --eval      # also cache an eval_snapshot.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Local `scripts` package must win over the site-packages one (known shadowing gotcha).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.agent.graph import answer
from scripts.agent.tools import AgentTools
from scripts.budget import telemetry_store
from scripts.feedback import capture as feedback_capture
from scripts.feedback import store as feedback_store
from scripts.graphql_server import db
from scripts.logger import get_logger
from scripts.profile import store as profile_store

logger = get_logger("seed_demo")

# (message, buyer_id, repeats) — repeats build volume and let the cache/FAQ paths recur.
_SCRIPT: list[tuple[str, str | None, int]] = [
    # FAQ short-circuits — zero LLM, order-independent policy questions.
    ("What is your return policy?", "BUY-1001", 4),
    ("How do commissions work?", None, 3),
    ("What is Smart?", "BUY-1003", 2),
    ("How does buyer protection work?", "BUY-1002", 2),
    # Deterministic triage — order-specific, low stakes.
    ("Can I return order ORD-4001?", "BUY-1001", 3),
    ("Where is my order ORD-4003?", "BUY-1003", 3),
    ("What is the seller commission on ORD-4001?", "BUY-1001", 2),
    ("Is ORD-4004 still returnable?", "BUY-1001", 2),
    ("Status of ORD-4002?", "BUY-1002", 2),
    # High-stakes — should auto-flag for human review (feeds the HITL sample).
    ("I want a refund on ORD-4002", "BUY-1002", 3),
    ("I'd like to open a dispute for ORD-4003", "BUY-1003", 2),
    ("Am I covered by buyer protection on ORD-4001?", "BUY-1001", 2),
    ("Refund eligibility for ORD-4004 please", "BUY-1001", 2),
    # Ambiguous — may escalate to the LLM classifier.
    ("hey, something's off with my recent purchase", "BUY-1002", 2),
    ("the thing I bought doesn't work, help", "BUY-1003", 2),
]

# A few external end-user signals (the /feedback channel) so the reviewer mix isn't all simulated.
_USER_SIGNALS = [
    {"signal_type": "rating", "intent": "order_status", "rating": 1, "buyer_id": "BUY-1003"},
    {"signal_type": "rating", "intent": "return_check", "rating": 1, "buyer_id": "BUY-1001"},
    {"signal_type": "correction", "intent": "commission_query", "buyer_id": "BUY-1001",
     "correction": "buyer asked for the fee in PLN, not %"},
    {"signal_type": "rating", "intent": "refund_eligibility", "rating": -1, "buyer_id": "BUY-1002"},
]


def seed_turns(reset: bool) -> int:
    db.init_db(include_generated=True)
    profile_store.init_db()
    telemetry_store.init_db(force=reset)
    feedback_store.init_db(force=reset)

    tools = AgentTools()
    n = 0
    for message, buyer_id, repeats in _SCRIPT:
        for _ in range(repeats):
            try:
                answer(message, buyer_id=buyer_id, tools=tools)
                n += 1
            except Exception as exc:  # one bad turn must not abort the seed run
                logger.warning(f"turn failed ({message!r}): {exc}")
    for sig in _USER_SIGNALS:
        try:
            feedback_capture.submit(**sig)
        except Exception as exc:
            logger.warning(f"user signal failed: {exc}")
    logger.info(f"seeded {n} turns · telemetry={telemetry_store.count()} feedback={feedback_store.count()}")
    return n


def cache_eval_snapshot() -> None:
    """Run the frozen eval and cache the result for the dashboard's promotion-gate card.

    Eval drives the agent over baseline cases, and the graph persists telemetry/feedback for every
    turn — so redirect both stores to throwaway files during the run (eval.py's own warning) and
    restore them afterwards, keeping the production census clean.
    """
    from scripts.improvement import eval as eval_mod

    real_tel, real_fb = telemetry_store._db_path, feedback_store._db_path
    tmp = Path(__file__).resolve().parents[1] / "inputs"
    telemetry_store.configure(str(tmp / "_eval_telemetry.db"))
    feedback_store.configure(str(tmp / "_eval_feedback.db"))
    telemetry_store.init_db(force=True)
    feedback_store.init_db(force=True)
    try:
        result = eval_mod.run_eval()
    finally:
        telemetry_store.configure(str(real_tel))
        feedback_store.configure(str(real_fb))
        for f in (tmp / "_eval_telemetry.db", tmp / "_eval_feedback.db"):
            f.unlink(missing_ok=True)

    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result.pop("per_case", None)  # keep the cached snapshot small; axes + failures are enough
    out = Path(__file__).resolve().parent / "eval_snapshot.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info(f"eval snapshot → {out.name}  (overall_accuracy={result.get('overall_accuracy')})")


def main() -> None:
    try:
        reset = "--keep" not in sys.argv
        seed_turns(reset=reset)
        if "--eval" in sys.argv:
            cache_eval_snapshot()
    except Exception:
        logger.exception("seed_demo failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
