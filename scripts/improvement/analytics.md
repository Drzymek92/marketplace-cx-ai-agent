# analytics.py

## Purpose
Module 9 — the analytics surface behind the performance dashboard. A thin, presentation-oriented
layer over `improvement/aggregate.py`: aggregate merges the telemetry CENSUS (every turn) and the
feedback SAMPLE (reviewed turns) per intent; this module re-frames the same data by **pipeline
module** so the dashboard shows how each cost-saving module (triage, FAQ, semantic cache, tier
router, rules engine, HITL gate, eval harness) is pulling its weight. Powers `/analytics.json`.

## Inputs
- Telemetry rows (`budget.telemetry_store.all_records()`) — the per-turn census.
- Feedback rows (`feedback.store.all_records()`) — the reviewed sample.
- Optional `eval_snapshot.json` (written by `seed_demo.py --eval`) for the promotion-gate card.

## Outputs
- A single dashboard payload dict: `kpis`, per-module `modules` cards, `classify_path`, `tier_mix`,
  `tools`, `per_intent`, `quality`, `coverage`, `eval`.

## Key Functions
| Function | What it does |
|---|---|
| `kpis` | Headline cost/efficiency numbers; the honest metric = LLM calls avoided vs a naive 2/turn floor |
| `classify_path` | Counts how intent was resolved: deterministic triage / static FAQ / LLM classifier |
| `tool_usage` | Frequency of each rule/tool call across turns |
| `module_breakdown` | Per-module cards `{key,name,layer,stat,detail,insight}` with a plain-English read on each |
| `load_eval_snapshot` | Best-effort read of the cached eval snapshot (None if absent/unreadable) |
| `build` | Pure assembly of the full payload from row lists (no I/O) — the testable core |
| `build_dashboard` | Convenience wrapper that reads the live stores + snapshot |

## Dependencies
- Internal: `budget.telemetry_store`, `budget.router` (`_TIER_ORDER`), `feedback.store`,
  `improvement.aggregate`.
- External: stdlib only (`json`, `datetime`, `pathlib`, `typing`).

## Known Gotchas
- Reaches into `router._TIER_ORDER` (a private symbol) to rank premium tiers (`analytics.py:25,30-31`).
  Left as-is deliberately (low value); a cleaner build would export a public tier-order interface.
  Project punch-list item #7.
- The dashboard "insight" threshold bands are hardcoded magic numbers (e.g. `0.05 <= rate <= 0.4`,
  `analytics.py:132,143,165,197`). Intentional for the demo; punch-list item #6 proposes lifting them
  into config / a presentation map.
- `cache_hit` in telemetry conflates FAQ + semantic-cache; `kpis`/`module_breakdown` exclude
  `classify_mode == "faq"` so the cache hit rate reflects semantic-cache reuse only — keep that filter
  if the metric is changed.

## Open Work
- Optional: externalize the insight threshold bands (#6) and add a public tier-order accessor (#7).
  Both are deferred polish, not correctness issues.
