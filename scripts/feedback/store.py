"""SQLite persistence for HITL feedback (Module 5).

Its own DB file (scripts/inputs/feedback.db), separate from marketplace data and the
profile/history store — feedback is the audit trail the improvement loop (Module 6) reads, so it
lives apart and can be retained/exported on its own schedule. Each row snapshots the turn's
telemetry so aggregation never has to join back to history.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scripts.feedback.models import FeedbackRecord

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "inputs" / "feedback.db"
_db_path = Path(os.environ.get("FEEDBACK_DB_PATH", str(_DEFAULT_PATH)))

_SCHEMA = """
CREATE TABLE feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id      INTEGER,        -- profile.history.turn_id (nullable: anonymous turns)
    buyer_id     TEXT,
    intent       TEXT,
    signal_type  TEXT,           -- rating | correction | edit | escalation
    reviewer     TEXT,           -- sim | user | <reviewer id>
    rating       INTEGER,        -- +1 / -1 (nullable)
    correction   TEXT,
    edit         TEXT,
    note         TEXT,
    model_tier   TEXT,
    tokens       INTEGER DEFAULT 0,
    tools_used   TEXT,           -- JSON list
    rule_version TEXT,
    created_at   TEXT
);
CREATE INDEX idx_feedback_intent ON feedback(intent);
CREATE INDEX idx_feedback_turn ON feedback(turn_id);
"""


def configure(path: str) -> None:
    global _db_path
    _db_path = Path(path)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(force: bool = False) -> None:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        if force:
            conn.execute("DROP TABLE IF EXISTS feedback")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "feedback" in existing:
            return
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record(rec: FeedbackRecord) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO feedback "
            "(turn_id, buyer_id, intent, signal_type, reviewer, rating, correction, edit, note, "
            " model_tier, tokens, tools_used, rule_version, created_at) "
            "VALUES (:turn_id,:buyer_id,:intent,:signal_type,:reviewer,:rating,:correction,:edit,"
            ":note,:model_tier,:tokens,:tools_used,:rule_version,:created_at)",
            {
                "turn_id": rec.turn_id, "buyer_id": rec.buyer_id, "intent": rec.intent,
                "signal_type": rec.signal_type, "reviewer": rec.reviewer, "rating": rec.rating,
                "correction": rec.correction, "edit": rec.edit, "note": rec.note,
                "model_tier": rec.model_tier, "tokens": int(rec.tokens or 0),
                "tools_used": json.dumps(rec.tools_used or []),
                "rule_version": rec.rule_version, "created_at": _now(),
            },
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["tools_used"] = json.loads(d.get("tools_used") or "[]")
    return d


def list_for_turn(turn_id: int) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM feedback WHERE turn_id=? ORDER BY id ASC", (turn_id,)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def all_records() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM feedback ORDER BY id ASC").fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def count() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    finally:
        conn.close()
