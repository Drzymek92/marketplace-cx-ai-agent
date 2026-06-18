# db.py

## Purpose
SQLite data access for the marketplace GraphQL server. Returns plain dicts/rows; `schema.py` maps
them onto GraphQL types and resolves relationships lazily via DataLoaders (`loaders.py`). The
`*_by_ids` functions are the batch endpoints the DataLoaders call — one query per entity type per
request instead of the N+1 a naive per-field approach would produce.

## Inputs
- `seed_data.py` — the committed canonical seed (buyers/sellers/offers/orders/items/returns).
- `scripts/inputs/generated_seed.json` — optional bulk dataset from `generate_seed.py`, merged when
  `init_db(include_generated=True)`.
- `MARKETPLACE_DB_PATH` env var overrides the default `scripts/inputs/marketplace.db`.

## Outputs
- `scripts/inputs/marketplace.db` (SQLite), rebuilt from the seed on `init_db`.
- Query results as dicts / grouped dicts; pagination payloads for the Relay connection.

## Key Functions
| Function | What it does |
|---|---|
| `configure` / `get_conn` | Point at a DB path (tests use a temp file); open a `Row`-factory connection |
| `init_db` | Create schema + load canonical seed; `force` drops first; `include_generated` merges the bulk file |
| `_merge_generated` | `INSERT OR IGNORE` the generated bundle on top of the seed (new ID ranges never clobber committed records) |
| `fetch_*_by_ids` | Batch endpoints (`WHERE id IN (...)`) for buyers/sellers/offers/orders — the DataLoader N+1 fix |
| `fetch_order_items_by_order_ids` | Batch line-item fetch, grouped by `order_id` |
| `fetch_orders_page` | Keyset (seek) pagination over `(placed_at DESC, id DESC)` — stable under concurrent inserts/deletes |
| `fetch_return` / `insert_return` | Read one return; create a `REQUESTED` return for an existing order |
| `reset_counts` / `get_counts` / `_count` | Per-entity query instrumentation (tests assert DataLoader batching) |

## Dependencies
- Internal: `graphql_server.seed_data`.
- External: stdlib only (`sqlite3`, `json`, `os`, `pathlib`, `datetime`).

## Known Gotchas
- `insert_return` derives its PK from `COUNT(*)` (`db.py:212`) — racy under concurrent or post-delete
  inserts. Left in deliberately as a documented demo tradeoff (a real build would use an autoincrement
  PK or a sequence). See the project punch-list item #9.
- `_merge_generated` uses `INSERT OR IGNORE` for keyed tables but plain `INSERT` for `order_items`
  (no PK) — re-merging the same generated file would duplicate items; `init_db` early-returns if the
  schema already exists, so this only matters on a forced rebuild.
- The batch fns use the sync sqlite driver inside async DataLoaders (blocking) — fine for a demo; a
  production async driver would make them truly non-blocking. Batching (the N+1 win) is realized regardless.

## Open Work
- None outstanding for the demo. Production hardening (async driver, sequence-based return PK) noted above.
