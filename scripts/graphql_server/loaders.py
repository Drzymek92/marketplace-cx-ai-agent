"""Per-request DataLoaders that batch entity lookups (the N+1 fix).

A DataLoader collects all `.load(key)` calls made within one event-loop tick and resolves them
with a single batch function call, then caches results for the rest of the request. They MUST be
created fresh per request — a shared loader would leak one request's cache into the next.

`make_loaders()` builds a fresh set; `get_context()` is the FastAPI context_getter. Tests build
loaders inside the running loop (see tests/conftest-style helpers) and pass them as context_value.
"""

from typing import Optional

from strawberry.dataloader import DataLoader

from scripts.graphql_server import db


async def _load_buyers(keys: list[str]) -> list[Optional[dict]]:
    found = db.fetch_buyers_by_ids(keys)
    return [found.get(k) for k in keys]


async def _load_sellers(keys: list[str]) -> list[Optional[dict]]:
    found = db.fetch_sellers_by_ids(keys)
    return [found.get(k) for k in keys]


async def _load_offers(keys: list[str]) -> list[Optional[dict]]:
    found = db.fetch_offers_by_ids(keys)
    return [found.get(k) for k in keys]


async def _load_orders(keys: list[str]) -> list[Optional[dict]]:
    found = db.fetch_orders_by_ids(keys)
    return [found.get(k) for k in keys]


async def _load_order_items(keys: list[str]) -> list[list[dict]]:
    found = db.fetch_order_items_by_order_ids(keys)
    return [found.get(k, []) for k in keys]


def make_loaders() -> dict:
    return {
        "buyer": DataLoader(load_fn=_load_buyers),
        "seller": DataLoader(load_fn=_load_sellers),
        "offer": DataLoader(load_fn=_load_offers),
        "order": DataLoader(load_fn=_load_orders),
        "order_items": DataLoader(load_fn=_load_order_items),
    }


async def get_context() -> dict:
    # Built inside the request's event loop so each DataLoader binds to the right loop.
    return {"loaders": make_loaders()}
