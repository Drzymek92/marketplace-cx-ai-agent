# scripts/agent/ — Module 3 (agent core)

## Purpose
LangGraph CX agent: turns a customer message into a grounded answer by classifying intent,
fetching data via the GraphQL client, applying versioned business rules, and composing a reply.

## Inputs
- A user `message` (str) + optional `buyer_id`, via `answer()` or `POST /chat`.
- Live GraphQL endpoint at http://127.0.0.1:8000/graphql (the `MarketplaceClient` consumes it).

## Outputs
- Final `AgentState`: `answer`, `intent`, `needs_human`, `tool_calls`, `meta` (tiers, rule_version).

## Flow (graph.py)
`triage → [faq | classify | trusted] → retrieve → stakes_gate → respond → finalize`.
- **triage** (`triage.py` + `faq.py`, zero LLM) — runs first. FAQ phrase match → canned answer,
  short-circuit to finalize (0 LLM calls). Else deterministic graded-confidence classify; conf ≥ 0.8
  → trust it and SKIP the LLM classifier; otherwise escalate to the LLM classify node.
- **classify** — LLM (fast tier) → `{intent, order_id, confidence}` JSON; only reached when triage
  was unsure. Falls back to deterministic triage if the LLM errors. `ORD-####` regex always wins.
- **retrieve** — maps intent→tool (`INTENT_TOOL`) and calls it deterministically via `AgentTools`.
- **stakes_gate** — refund/dispute/commission (`HIGH_STAKES`) or confidence < 0.55 → `needs_human`.
  Fires even after a trusted triage (skipping the LLM never bypasses the safety gate).
- **respond** — LLM composes the answer grounded ONLY in retrieved facts; HITL banner if flagged.
- **finalize** — assembles the per-turn telemetry record into `meta.telemetry` (`budget/telemetry.py`).

Tier routing + escalation policy live in `scripts/budget/router.py`; cost telemetry in
`scripts/budget/telemetry.py`. `meta.telemetry.llm_calls` = 0 (FAQ) / 1 (trusted triage) / 2 (LLM classify).

## Key Functions / objects
| Name | What it does |
|---|---|
| `answer(message, buyer_id, tools)` | Run one turn; builds/caches the graph (fresh graph if tools injected) |
| `build_graph(tools)` | Compile the LangGraph; injects `AgentTools` into the retrieve node via closure |
| `AgentTools.call(name, **kw)` | Dispatch a tool by name; returns `ToolResult(ok, data, error)` |
| `TIER_MODELS` | tier→model map for an OpenAI-compatible gateway (cheap `fast`/`bulk` → capable `reason`) |
| `_classify_fallback` | Keyword/regex intent classifier for graceful degradation (no LLM) |

## Dependencies
- Internal: `scripts.graphql_client.client`, `scripts.rules.*`, `scripts.config.loader`,
  `scripts.llm_client` (lazy import inside nodes), `scripts.logger`.
- External: `langgraph`, `langchain-openai` (via llm_client).

## Known Gotchas
- LLM is imported lazily inside nodes → tests monkeypatch `scripts.llm_client.llm_json/llm_call`.
- Tools are injected through a closure in `build_graph`, NOT through the state dict (LangGraph
  rejects keys outside the typed channels).
- `/chat` is a **sync** FastAPI route on purpose: it runs in a threadpool so the agent's httpx
  self-call to `/graphql` (same process) doesn't block the event loop.
- Model-tier routing here is a stand-in; Module 7 (cost governor) will own it.

## Open Work
- Wire Module 4 profile/history into classify+respond context.
- Emit the structured feedback/telemetry record (Module 5) keyed to `tool_calls`+`rule_version`.
- Real HITL queue instead of the inline banner.
