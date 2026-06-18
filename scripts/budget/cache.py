"""Module 7 — semantic response cache (embeddings + local fallback).

Caches answers to **order-independent, non-personalized** questions so a repeat (or a paraphrase)
skips the respond LLM. Same safety rule as the static FAQ: never cache anything tied to an order id
or a specific buyer — those answers aren't reusable across users/turns.

Matching: LLM-gateway embeddings + cosine similarity (catches paraphrases). When embeddings are
unavailable (offline, no creds, or the gateway errors), it degrades to a deterministic
normalized-exact match — the same graceful-degradation pattern the summarizer/classifier use.

Invalidation: an entry stores the `rule_version` it was produced under; a lookup under a different
version is treated as stale and skipped (policy answers must not outlive a rules change). The store
is in-memory (warms per process) — a production build would persist + add a TTL.
"""

from __future__ import annotations

import math
import re
from typing import Callable, Optional

from scripts.agent.triage import find_order_id
from scripts.logger import get_logger

logger = get_logger("cache")

SIM_THRESHOLD = 0.92
_entries: list[dict] = []


def clear() -> None:
    _entries.clear()


def size() -> int:
    return len(_entries)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def is_eligible(message: str, order_id: Optional[str] = None) -> bool:
    """Order-independent only — mirrors the FAQ safety rule (no order id anywhere)."""
    return find_order_id(message) is None and not order_id


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _default_embed(text: str) -> Optional[list[float]]:
    try:
        from scripts.llm_client import embed
        return embed(text)
    except Exception as exc:                       # offline / no creds / gateway error
        logger.info(f"cache: embeddings unavailable ({exc}); using normalized-exact fallback")
        return None


def lookup(message: str, *, rule_version: Optional[str] = None,
           embed_fn: Optional[Callable[[str], Optional[list[float]]]] = None,
           threshold: float = SIM_THRESHOLD) -> Optional[str]:
    if not _entries:
        return None
    embed_fn = embed_fn or _default_embed
    norm = _normalize(message)
    vec = embed_fn(message)

    fresh = [e for e in _entries if rule_version is None or e.get("rule_version") in (None, rule_version)]
    if vec is not None:
        best, best_sim = None, 0.0
        for e in fresh:
            if e.get("vec") is None:
                continue
            sim = _cosine(vec, e["vec"])
            if sim > best_sim:
                best, best_sim = e, sim
        if best is not None and best_sim >= threshold:
            logger.info(f"cache: semantic hit (sim={best_sim:.3f})")
            return best["answer"]
        return None
    # local fallback: deterministic normalized-exact match
    for e in fresh:
        if e["norm"] == norm:
            logger.info("cache: normalized-exact hit")
            return e["answer"]
    return None


def store(message: str, answer: str, *, rule_version: Optional[str] = None,
          embed_fn: Optional[Callable[[str], Optional[list[float]]]] = None) -> None:
    embed_fn = embed_fn or _default_embed
    _entries.append({
        "text": message, "norm": _normalize(message), "vec": embed_fn(message),
        "answer": answer, "rule_version": rule_version,
    })
