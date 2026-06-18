"""Module 3 — agent core (LangGraph state machine + tool registry).

The graph drives a fixed flow (classify -> retrieve -> apply rules -> respond); the
tool registry in tools.py wraps the GraphQL client and rules engine as the callables
the graph invokes deterministically. See documentation_processed/architecture.md §3.
"""
