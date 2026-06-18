"""Module 4 — user profile + conversation history (personalization).

`store.py` persists profiles + turn history (SQLite, separate from the marketplace DB).
`context.py` is the budget-aware builder: it filters the profile to the fields the config
allows, injects the last N turns verbatim, and rolls older turns into a running summary —
so the personalization context the agent pays for stays bounded.
"""
