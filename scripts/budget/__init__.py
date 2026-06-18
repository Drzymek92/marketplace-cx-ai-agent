"""Module 7 (partial) — token / cost governor.

Built as a thin, dependency-safe slice while the agent is fresh:
  - router.py    : model-tier routing + escalation policy (moved out of the agent graph)
  - telemetry.py : per-turn token/tier/tool record (the backbone Modules 5 & 6 attribute against)

DEFERRED until after Module 4 (profile + history), because their inputs don't exist yet:
  - context budgeter (caps/summarizes history turns + profile fields)
  - general / semantic response cache + invalidation (cache keys depend on personalization)
  - config-driven ceilings + graceful-degradation ladder (needs the config loader extended)
See documentation_processed/architecture.md §7 for the deferral note.
"""
