"""SQLite data access for the marketplace GraphQL server.

Returns plain dicts/rows; schema.py maps them onto GraphQL types and resolves relationships
lazily via DataLoaders (loaders.py). The `*_by_ids` functions are the batch endpoints the
DataLoaders call — one query per entity type per request instead of the N+1 a naive per-field
approach would produce.
"""

import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from scripts.graphql_server import seed_data

_GENERATED_PATH = Path(__file__).resolve().parents[2] / "scripts" / "inputs" / "generated_seed.json"

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "inputs" / "marketplace.db"
_db_path = Path(os.environ.get("MARKETPLACE_DB_PATH", str(_DEFAULT_PATH)))

_SCHEMA = """
CREATE TABLE buyers   (id TEXT PRIMARY KEY, login TEXT, smart INTEGER, locale TEXT);
CREATE TABLE sellers  (id TEXT PRIMARY KEY, name TEXT, rating REAL);
CREATE TABLE offers   (id TEXT PRIMARY KEY, name TEXT, category TEXT, price TEXT, seller_id TEXT);
CREATE TABLE orders   (id TEXT PRIMARY KEY, buyer_id TEXT, seller_id TEXT, status TEXT, placed_at TEXT, delivery_method TEXT);
CREATE TABLE order_items (order_id TEXT, offer_id TEXT, quantity INTEGER, unit_price TEXT);
CREATE TABLE returns  (id TEXT PRIMARY KEY, order_id TEXT, reason TEXT, status TEXT, opened_at TEXT);
-- Monotonic id allocator for return PKs. A persistent counter (not COUNT(*)) so ids are never
-- reused after a delete and generation is atomic under concurrency — see insert_return.
CREATE TABLE id_sequences (name TEXT PRIMARY KEY, next_val INTEGER NOT NULL);
CREATE INDEX idx_orders_buyer ON orders(buyer_id, placed_at DESC, id DESC);
-- FK-column indexes for the batch loaders' `WHERE <fk> IN (...)` lookups. Without these the
-- filter is a full table SCAN whose cost grows linearly with the table — invisible at seed
-- scale (tens of rows) but ~12x slower than the indexed SEARCH at 40k orders (benchmarked).
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_returns_order ON returns(order_id);
CREATE INDEX idx_offers_seller ON offers(seller_id);
"""

_TABLES = ("buyers", "sellers", "offers", "orders", "order_items", "returns")

# --- query-count instrumentation (used by tests to prove DataLoader batching) ---
_CALL_COUNTS: dict[str, int] = {}


def reset_counts() -> None:
    _CALL_COUNTS.clear()


def get_counts() -> dict[str, int]:
    return dict(_CALL_COUNTS)


def _count(name: str) -> None:
    _CALL_COUNTS[name] = _CALL_COUNTS.get(name, 0) + 1


def configure(path: str) -> None:
    global _db_path
    _db_path = Path(path)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(force: bool = False, include_generated: bool = False) -> None:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        if force:
            for tbl in _TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "buyers" in existing:
            return
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO buyers VALUES (:id,:login,:smart,:locale)",
                         [{**b, "smart": int(b["smart"])} for b in seed_data.BUYERS])
        conn.executemany("INSERT INTO sellers VALUES (:id,:name,:rating)", seed_data.SELLERS)
        conn.executemany("INSERT INTO offers VALUES (:id,:name,:category,:price,:seller_id)", seed_data.OFFERS)
        conn.executemany("INSERT INTO orders VALUES (:id,:buyer_id,:seller_id,:status,:placed_at,:delivery_method)", seed_data.ORDERS)
        conn.executemany("INSERT INTO order_items VALUES (:order_id,:offer_id,:quantity,:unit_price)", seed_data.ORDER_ITEMS)
        conn.executemany("INSERT INTO returns VALUES (:id,:order_id,:reason,:status,:opened_at)", seed_data.RETURNS)
        if include_generated:
            _merge_generated(conn)
        _init_return_sequence(conn)
        conn.commit()
    finally:
        conn.close()


def _init_return_sequence(conn: sqlite3.Connection) -> None:
    """Seed the returns id sequence to one above the highest existing RET-<n>, so insert_return
    never reuses or collides with a seeded/generated id. Idempotent; only ever raises the watermark."""
    hi = conn.execute(
        "SELECT MAX(CAST(SUBSTR(id, 5) AS INTEGER)) FROM returns WHERE id LIKE 'RET-%'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO id_sequences(name, next_val) VALUES('returns', ?) "
        "ON CONFLICT(name) DO UPDATE SET next_val = MAX(id_sequences.next_val, excluded.next_val)",
        ((hi or 5000) + 1,),
    )


def _merge_generated(conn: sqlite3.Connection) -> None:
    """Merge the generated bulk dataset (generate_seed.py) on top of the canonical seed. New ID
    ranges, so INSERT OR IGNORE never clobbers the committed records. No-op if the file is absent."""
    if not _GENERATED_PATH.exists():
        return
    d = json.loads(_GENERATED_PATH.read_text(encoding="utf-8"))
    conn.executemany("INSERT OR IGNORE INTO buyers VALUES (:id,:login,:smart,:locale)",
                     [{**b, "smart": int(b["smart"])} for b in d.get("buyers", [])])
    conn.executemany("INSERT OR IGNORE INTO sellers VALUES (:id,:name,:rating)", d.get("sellers", []))
    conn.executemany("INSERT OR IGNORE INTO offers VALUES (:id,:name,:category,:price,:seller_id)", d.get("offers", []))
    conn.executemany("INSERT OR IGNORE INTO orders VALUES (:id,:buyer_id,:seller_id,:status,:placed_at,:delivery_method)", d.get("orders", []))
    conn.executemany("INSERT INTO order_items VALUES (:order_id,:offer_id,:quantity,:unit_price)", d.get("order_items", []))
    conn.executemany("INSERT OR IGNORE INTO returns VALUES (:id,:order_id,:reason,:status,:opened_at)", d.get("returns", []))


def _phs(n: int) -> str:
    return ",".join(["?"] * n)


# --- batch endpoints (one query per call; DataLoaders dedupe keys before calling) ---
def fetch_buyers_by_ids(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    _count("buyers")
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM buyers WHERE id IN ({_phs(len(ids))})", list(ids)).fetchall()
        return {r["id"]: {"id": r["id"], "login": r["login"], "smart": bool(r["smart"]), "locale": r["locale"]}
                for r in rows}
    finally:
        conn.close()


def fetch_sellers_by_ids(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    _count("sellers")
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM sellers WHERE id IN ({_phs(len(ids))})", list(ids)).fetchall()
        return {r["id"]: dict(r) for r in rows}
    finally:
        conn.close()


def fetch_offers_by_ids(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    _count("offers")
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM offers WHERE id IN ({_phs(len(ids))})", list(ids)).fetchall()
        return {r["id"]: dict(r) for r in rows}
    finally:
        conn.close()


def fetch_orders_by_ids(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    _count("orders")
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM orders WHERE id IN ({_phs(len(ids))})", list(ids)).fetchall()
        return {r["id"]: dict(r) for r in rows}
    finally:
        conn.close()


def fetch_order_items_by_order_ids(order_ids: list[str]) -> dict[str, list[dict]]:
    if not order_ids:
        return {}
    _count("order_items")
    conn = get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM order_items WHERE order_id IN ({_phs(len(order_ids))})",
                            list(order_ids)).fetchall()
        grouped: dict[str, list[dict]] = {oid: [] for oid in order_ids}
        for r in rows:
            grouped.setdefault(r["order_id"], []).append(
                {"offer_id": r["offer_id"], "quantity": r["quantity"], "unit_price": r["unit_price"]})
        return grouped
    finally:
        conn.close()


def fetch_orders_page(buyer_id: str, first: int, after: Optional[tuple[str, str]] = None) -> dict:
    """Keyset (seek) pagination over orders sorted by (placed_at DESC, id DESC).

    `after` is the decoded sort key (placed_at, id) of the last item seen — NOT an offset —
    so the page stays consistent even if rows are inserted/removed between requests.
    Returns raw order rows; nested fields are resolved lazily via DataLoaders.
    """
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM orders WHERE buyer_id=?", (buyer_id,)).fetchone()[0]
        sql = "SELECT * FROM orders WHERE buyer_id=?"
        params: list = [buyer_id]
        if after:
            sql += " AND (placed_at < ? OR (placed_at = ? AND id < ?))"
            params += [after[0], after[0], after[1]]
        sql += " ORDER BY placed_at DESC, id DESC LIMIT ?"
        params.append(first + 1)  # one extra row to detect a next page without a second query
        rows = conn.execute(sql, params).fetchall()
        has_next = len(rows) > first
        rows = rows[:first]
        return {"orders": [dict(r) for r in rows], "has_next": has_next, "total": total}
    finally:
        conn.close()


def fetch_return(return_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_return(order_id: str, reason: str) -> Optional[dict]:
    """Atomically allocate a monotonic RET-<n> id and insert the return.

    The id is drawn from the persistent `id_sequences` counter, not `COUNT(*)`: id generation and
    the insert run in one `BEGIN IMMEDIATE` transaction, so concurrent writers serialize, and the
    counter only ever advances — ids are never reused after a delete and never collide with a
    seeded/generated id (which `5001 + COUNT(*)` could do under concurrency, deletes, or range overlap).
    """
    conn = get_conn()
    conn.isolation_level = None  # manual transaction control (explicit BEGIN/COMMIT/ROLLBACK)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Defensive for a pre-sequence (legacy) db: ensure the allocator table + row exist.
        conn.execute("CREATE TABLE IF NOT EXISTS id_sequences (name TEXT PRIMARY KEY, next_val INTEGER NOT NULL)")
        if not conn.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            conn.execute("ROLLBACK")
            return None
        row = conn.execute("SELECT next_val FROM id_sequences WHERE name='returns'").fetchone()
        if row is None:
            hi = conn.execute(
                "SELECT MAX(CAST(SUBSTR(id, 5) AS INTEGER)) FROM returns WHERE id LIKE 'RET-%'"
            ).fetchone()[0]
            next_val = (hi or 5000) + 1
            conn.execute("INSERT INTO id_sequences(name, next_val) VALUES('returns', ?)", (next_val,))
        else:
            next_val = row[0]
        conn.execute("UPDATE id_sequences SET next_val=? WHERE name='returns'", (next_val + 1,))
        record = {"id": f"RET-{next_val}", "order_id": order_id, "reason": reason,
                  "status": "REQUESTED", "opened_at": datetime.now(timezone.utc).isoformat()}
        conn.execute("INSERT INTO returns VALUES (:id,:order_id,:reason,:status,:opened_at)", record)
        conn.execute("COMMIT")
        return record
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
