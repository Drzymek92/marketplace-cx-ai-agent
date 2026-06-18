"""Per-turn telemetry persistence (Module 7/9 observability; feeds Module 6 aggregation).

`telemetry.build_record()` shapes the per-turn record; this persists EVERY turn — not just the
reviewed ones in feedback.db — so cost/volume aggregation has the full, unbiased population. Feedback
is a biased sample (flagged + a small audit slice); telemetry is the census. Separate telemetry.db so
it can be retained/rotated independently of personalization and feedback data.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "inputs" / "telemetry.db"
_db_path = Path(os.environ.get("TELEMETRY_DB_PATH", str(_DEFAULT_PATH)))

_SCHEMA = """
CREATE TABLE telemetry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id       INTEGER,        -- profile.history.turn_id (nullable: anonymous turns)
    buyer_id      TEXT,
    intent        TEXT,
    classify_mode TEXT,           -- triage | llm | faq
    classify_tier TEXT,
    respond_tier  TEXT,
    llm_calls     INTEGER DEFAULT 0,
    cache_hit     INTEGER DEFAULT 0,
    needs_human   INTEGER DEFAULT 0,
    est_tokens    INTEGER DEFAULT 0,
    tools_used    TEXT,           -- JSON list
    rule_version  TEXT,
    created_at    TEXT
);
CREATE INDEX idx_telemetry_intent ON telemetry(intent);
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
            conn.execute("DROP TABLE IF EXISTS telemetry")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "telemetry" in existing:
            return
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record(rec: dict[str, Any], *, turn_id: Optional[int] = None, buyer_id: Optional[str] = None) -> int:
    """Persist one per-turn telemetry record (the dict from telemetry.build_record)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO telemetry "
            "(turn_id, buyer_id, intent, classify_mode, classify_tier, respond_tier, llm_calls, "
            " cache_hit, needs_human, est_tokens, tools_used, rule_version, created_at) "
            "VALUES (:turn_id,:buyer_id,:intent,:classify_mode,:classify_tier,:respond_tier,:llm_calls,"
            ":cache_hit,:needs_human,:est_tokens,:tools_used,:rule_version,:created_at)",
            {
                "turn_id": turn_id, "buyer_id": buyer_id,
                "intent": rec.get("intent"), "classify_mode": rec.get("classify_mode"),
                "classify_tier": rec.get("classify_tier"), "respond_tier": rec.get("respond_tier"),
                "llm_calls": int(rec.get("llm_calls") or 0),
                "cache_hit": int(bool(rec.get("cache_hit"))),
                "needs_human": int(bool(rec.get("needs_human"))),
                "est_tokens": int(rec.get("est_tokens") or 0),
                "tools_used": json.dumps(rec.get("tools_used") or []),
                "rule_version": rec.get("rule_version"), "created_at": _now(),
            },
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["tools_used"] = json.loads(d.get("tools_used") or "[]")
    d["cache_hit"] = bool(d["cache_hit"])
    d["needs_human"] = bool(d["needs_human"])
    return d


def all_records() -> list[dict]:
    conn = get_conn()
    try:
        return [_row(r) for r in conn.execute("SELECT * FROM telemetry ORDER BY id ASC").fetchall()]
    finally:
        conn.close()


def count() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    finally:
        conn.close()


def tokens_for_buyer(buyer_id: str) -> int:
    """Total est_tokens spent across a buyer's turns (the governor's per-conversation signal)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT COALESCE(SUM(est_tokens), 0) FROM telemetry WHERE buyer_id=?",
                           (buyer_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def tokens_since(iso_start: str) -> int:
    """Total est_tokens since an ISO timestamp (the governor's daily signal: pass start-of-day UTC)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT COALESCE(SUM(est_tokens), 0) FROM telemetry WHERE created_at >= ?",
                           (iso_start,)).fetchone()
        return int(row[0])
    finally:
        conn.close()
