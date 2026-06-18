# graph.py

## Purpose
LangGraph agent core (Module 3) plus the Module-7 cascade/telemetry slice. Runs one customer turn
through a cost-disciplined state machine: cheap deterministic steps first, LLM only when needed,
all financial decisions delegated to the versioned rules engine.

## Inputs
- `message: str` and optional `buyer_id` (via `answer()`), threaded as `AgentState`.
- A `Config` (loaded via `config/loader.py`) for context budgeting + module toggles.
- An `AgentTools` registry (GraphQL client + rules engine) injected into `retrieve`.

## Outputs
- Final `AgentState` dict: `answer`, `meta` (classify_mode, tiers, tools_used, rule_version,
  `telemetry` record), plus intent/order_id/confidence/needs_human.
- Side effect: every turn is persisted to history via `profile_ctx.record_turn` in `finalize`.

## Flow
`START → governor → load_context → triage → [faq short-circuit | LLM classify | trusted triage] →
retrieve → stakes_gate → respond → finalize → END`. The FAQ short-circuit routes triage straight to
`finalize` (0 LLM calls). Trusted triage (conf ≥ `TRIAGE_CONFIDENCE_FLOOR`) skips `classify`.
The `governor` node (Module 7) meters spend and emits a degradation directive; later nodes honour it
(`load_context` shrinks context, `stakes_gate` forces human handoff, `respond` caps tier / serves
cached-or-canned). `respond` also checks the semantic cache before calling the LLM.

## Key Functions
| Function | What it does |
|---|---|
| `answer` | Public entry; builds/caches the graph and invokes one turn |
| `build_graph` | Wires nodes + edges, returns the compiled graph |
| `make_governor_node` | Factory → node computing the Module 7 spend/degradation directive |
| `make_load_context_node` | Factory → node injecting budget-trimmed context (honours `shrink_context`) |
| `triage_node` | FAQ lookup + deterministic classify; decides whether to skip the LLM |
| `_triage_route` | Conditional edge: `answered` / `retrieve` / `classify` |
| `make_classify_node` | Factory → LLM classifier; injects config-driven few-shot exemplars (Module 6) |
| `make_retrieve_node` / `_retrieve` | Calls the right tool by intent (order/return/eligibility/commission) |
| `stakes_gate_node` | Flags `needs_human` on high-stakes intent, low confidence, OR governor handoff |
| `make_respond_node` | Factory → reply: semantic-cache lookup, governor tier-cap/canned-only, else grounded LLM |
| `make_finalize_node` | Assembles telemetry + persists turn (history), telemetry census, and HITL feedback |

## Dependencies
- Internal: `agent.faq`, `agent.triage`, `agent.tools`, `agent.state`, `budget.router`,
  `budget.telemetry`, `profile.context`, `config.loader`, `llm_client` (lazy import), `logger`.
- External: `langgraph`.

## Known Gotchas
- The high-stakes gate runs on the classify/trusted-triage path but NOT the FAQ short-circuit —
  intentional: FAQ answers are order-independent policy with no financial decision.
- `llm_client` is imported lazily inside `classify_node`/`respond_node` so the graph builds (and
  tests run) without live LLM creds; both have deterministic fallbacks.
- The LLM never free-calls tools — `_retrieve` dispatches deterministically via `INTENT_TOOL`.
- A module-level `_GRAPH` is cached only when no `tools` are injected; tests/server pass their own.

## Open Work
- Module 5 (HITL queue) replaces the inline `needs_human` banner with a real review record.
- Module 7 deferred half (context ceilings/degradation, semantic cache) plugs in around `respond`.
