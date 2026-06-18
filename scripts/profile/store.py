"""SQLite persistence for profiles + conversation history.

Separate DB file from the marketplace data (scripts/inputs/profiles.db) — personalization
state is the agent's own memory, not seller/order data, so it lives apart and can be wiped
or migrated independently. Returns plain dicts; the context builder (context.py) decides
what is actually injected per the budget.

The `recent_issues` list and the rolling `summary` are stored as JSON text in single columns
(small, read whole) rather than normalized tables — this is durable agent memory, not a
queryable analytics store.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "inputs" / "profiles.db"
_db_path = Path(os.environ.get("PROFILE_DB_PATH", str(_DEFAULT_PATH)))

_SCHEMA = """
CREATE TABLE profiles (
    buyer_id        TEXT PRIMARY KEY,
    smart_status    INTEGER,
    locale          TEXT,
    tone_pref       TEXT,
    recent_issues   TEXT,          -- JSON list
    summary         TEXT,          -- rolling summary of older turns
    summary_through INTEGER DEFAULT 0,  -- highest turn_id already folded into summary (watermark)
    updated_at      TEXT
);
CREATE TABLE history (
    turn_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_id TEXT,
    ts       TEXT,
    message  TEXT,
    intent   TEXT,
    answer   TEXT
);
CREATE INDEX idx_history_buyer ON history(buyer_id, turn_id);
"""

# Sample profiles aligned with seed_data.BUYERS (marketplace.db) so the demo is coherent.
_SEED_PROFILES = [
    {"buyer_id": "BUY-1001", "smart_status": True,  "locale": "pl",
     "tone_pref": "casual", "recent_issues": ["late_delivery"]},
    {"buyer_id": "BUY-1002", "smart_status": False, "locale": "pl",
     "tone_pref": "formal", "recent_issues": []},
    {"buyer_id": "BUY-1003", "smart_status": True,  "locale": "en",
     "tone_pref": "casual", "recent_issues": ["return_dispute", "commission_question"]},
]


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
            conn.execute("DROP TABLE IF EXISTS profiles")
            conn.execute("DROP TABLE IF EXISTS history")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "profiles" in existing:
            _migrate(conn)  # add summary_through to DBs created before the watermark landed
            return
        conn.executescript(_SCHEMA)
        for p in _SEED_PROFILES:
            conn.execute(
                "INSERT INTO profiles "
                "(buyer_id, smart_status, locale, tone_pref, recent_issues, summary, summary_through, updated_at) "
                "VALUES (:buyer_id,:smart,:locale,:tone,:issues,NULL,0,:ts)",
                {"buyer_id": p["buyer_id"], "smart": int(p["smart_status"]), "locale": p["locale"],
                 "tone": p["tone_pref"], "issues": json.dumps(p["recent_issues"]), "ts": _now()},
            )
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(profiles)")}
    if "summary_through" not in cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN summary_through INTEGER DEFAULT 0")
        conn.commit()


def _row_to_profile(row: sqlite3.Row) -> dict:
    return {
        "buyer_id": row["buyer_id"],
        "smart_status": bool(row["smart_status"]),
        "locale": row["locale"],
        "tone_pref": row["tone_pref"],
        "recent_issues": json.loads(row["recent_issues"] or "[]"),
        "summary": row["summary"],
        "summary_through": row["summary_through"] if "summary_through" in row.keys() else 0,
        "updated_at": row["updated_at"],
    }


def get_profile(buyer_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE buyer_id=?", (buyer_id,)).fetchone()
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


def upsert_profile(buyer_id: str, **fields) -> dict:
    current = get_profile(buyer_id) or {
        "buyer_id": buyer_id, "smart_status": False, "locale": "pl",
        "tone_pref": "casual", "recent_issues": [], "summary": None, "summary_through": 0,
    }
    current.update(fields)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO profiles "
            "(buyer_id, smart_status, locale, tone_pref, recent_issues, summary, summary_through, updated_at) "
            "VALUES (:buyer_id,:smart,:locale,:tone,:issues,:summary,:through,:ts) "
            "ON CONFLICT(buyer_id) DO UPDATE SET "
            "smart_status=:smart, locale=:locale, tone_pref=:tone, "
            "recent_issues=:issues, summary=:summary, summary_through=:through, updated_at=:ts",
            {"buyer_id": buyer_id, "smart": int(bool(current["smart_status"])),
             "locale": current["locale"], "tone": current["tone_pref"],
             "issues": json.dumps(current.get("recent_issues", [])),
             "summary": current.get("summary"),
             "through": int(current.get("summary_through") or 0), "ts": _now()},
        )
        conn.commit()
    finally:
        conn.close()
    return get_profile(buyer_id)  # type: ignore[return-value]


def set_summary(buyer_id: str, summary: str, through_turn_id: Optional[int] = None) -> None:
    """Set the rolling summary. Pass `through_turn_id` to also advance the fold watermark
    so those turns are never re-summarized."""
    conn = get_conn()
    try:
        if through_turn_id is None:
            conn.execute("UPDATE profiles SET summary=?, updated_at=? WHERE buyer_id=?",
                         (summary, _now(), buyer_id))
        else:
            conn.execute(
                "UPDATE profiles SET summary=?, summary_through=?, updated_at=? WHERE buyer_id=?",
                (summary, int(through_turn_id), _now(), buyer_id),
            )
        conn.commit()
    finally:
        conn.close()


def append_turn(buyer_id: str, message: str, intent: str, answer: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO history (buyer_id, ts, message, intent, answer) VALUES (?,?,?,?,?)",
            (buyer_id, _now(), message, intent, answer),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_turn(turn_id: int) -> Optional[dict]:
    """One history row by turn_id — lets Module 6 join feedback back to the message/answer."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM history WHERE turn_id=?", (turn_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_turns(buyer_id: str) -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM history WHERE buyer_id=?", (buyer_id,)).fetchone()[0]
    finally:
        conn.close()


def recent_turns(buyer_id: str, limit: int) -> list[dict]:
    """The most recent `limit` turns, returned oldest→newest (chronological for the prompt)."""
    if limit <= 0:
        return []
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM history WHERE buyer_id=? ORDER BY turn_id DESC LIMIT ?",
            (buyer_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def older_turns(buyer_id: str, keep_recent: int) -> list[dict]:
    """Turns older than the most recent `keep_recent` — the ones to fold into the summary."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM history WHERE buyer_id=? ORDER BY turn_id ASC "
            "LIMIT -1 OFFSET 0",
            (buyer_id,),
        ).fetchall()
        older = [dict(r) for r in rows]
        return older[:-keep_recent] if keep_recent > 0 else older
    finally:
        conn.close()


def turns_after(buyer_id: str, after_id: int, keep_recent: int) -> list[dict]:
    """Aged-out turns not yet folded into the summary: those older than the most recent
    `keep_recent` (the verbatim window) AND with turn_id > `after_id` (the fold watermark).

    This is what makes summarization fold each turn exactly once instead of re-summarizing the
    whole tail every turn — the cost guarantee the token governor depends on.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM history WHERE buyer_id=? ORDER BY turn_id ASC", (buyer_id,)
        ).fetchall()
        all_turns = [dict(r) for r in rows]
        aged_out = all_turns[:-keep_recent] if keep_recent > 0 else all_turns
        return [t for t in aged_out if t["turn_id"] > after_id]
    finally:
        conn.close()
