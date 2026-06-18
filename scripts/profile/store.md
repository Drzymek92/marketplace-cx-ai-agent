# store.py

## Purpose
SQLite persistence for the agent's own memory (Module 4): user profiles + per-turn conversation
history. Lives in a separate DB file from the marketplace data so personalization can be wiped or
migrated independently. Returns plain dicts; `context.py` decides what is actually injected.

## Inputs
- `buyer_id` + turn fields (message/intent/answer) from `context.record_turn`.
- DB path from `PROFILE_DB_PATH` env var, else `scripts/inputs/profiles.db`.

## Outputs
- `profiles` table (one row/buyer: smart_status, locale, tone_pref, recent_issues JSON, rolling
  `summary`, `summary_through` watermark) and `history` table (append-only turns).
- Auto-seeds 3 demo profiles aligned with `seed_data.BUYERS` on first `init_db()`.

## Key Functions
| Function | What it does |
|---|---|
| `init_db` | Create tables + seed; `_migrate` ALTERs in `summary_through` for older DBs |
| `configure` / `get_conn` | Point at a DB path / open a Row-factory connection |
| `get_profile` / `_row_to_profile` | Read a profile as a dict (incl. `summary_through`) |
| `upsert_profile` | Insert-or-update a profile (explicit-column INSERT + ON CONFLICT) |
| `set_summary` | Set rolling summary; optional `through_turn_id` advances the fold watermark |
| `append_turn` / `count_turns` | Append a history row / count a buyer's turns |
| `recent_turns` | Most recent N turns, chronological (the verbatim window) |
| `get_turn` | One history row by turn_id — lets Module 6 join feedback → message/answer |
| `older_turns` | All turns older than the most recent `keep_recent` (legacy helper) |
| `turns_after` | Aged-out turns past the watermark — the not-yet-folded batch |

## Dependencies
- Internal: none (seed profiles are inline `_SEED_PROFILES`).
- External: stdlib `sqlite3`, `json`, `os`, `datetime`, `pathlib`.

## Known Gotchas
- History is append-only — turns are NEVER deleted. Summarization relies on the `summary_through`
  watermark (not pruning) to fold each turn exactly once; `turns_after` is the correct source for
  "what still needs summarizing." Don't reintroduce a `count_turns > threshold` trigger — that was
  the every-turn re-summarization bug fixed 2026-06-18.
- INSERTs name columns explicitly; if you add a profiles column, update both INSERTs, `_SCHEMA`,
  `_row_to_profile`, and `_migrate`.
- `_row_to_profile` reads `summary_through` defensively (`"summary_through" in row.keys()`) so a
  not-yet-migrated DB still loads.

## Open Work
- None for Module 4. Module 5 (HITL feedback) will add its own store/tables, not reuse this one.
