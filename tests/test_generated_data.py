"""Thorough data-driven tests over the generated bulk dataset (generate_seed.py).

Two jobs: (1) prove the generator is referentially consistent + deterministic; (2) run the rules
engine over EVERY generated order asserting invariants — a property-style sweep that exercises far
more status/date/category combinations than the hand-written seed, catching rule edge cases the
small seed can't. The generation is deterministic (no LLM), so this is fully offline + reproducible.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from scripts.config.loader import load_config
from scripts.graphql_server import db, generate_seed
from scripts.rules.commission import compute_commission
from scripts.rules.eligibility import PROCESSES, qualifies_for
from scripts.rules.models import LineItemView, OrderView
from scripts.rules.returns import is_returnable


def _order_views(data: dict) -> list[OrderView]:
    offers = {o["id"]: o for o in data["offers"]}
    buyers = {b["id"]: b for b in data["buyers"]}
    items_by_order: dict[str, list] = {}
    for it in data["order_items"]:
        items_by_order.setdefault(it["order_id"], []).append(it)
    views = []
    for o in data["orders"]:
        lis = [LineItemView(offer_id=it["offer_id"], name=offers[it["offer_id"]]["name"],
                            category=offers[it["offer_id"]]["category"], quantity=it["quantity"],
                            unit_amount=Decimal(str(it["unit_price"]))) for it in items_by_order.get(o["id"], [])]
        views.append(OrderView(id=o["id"], status=o["status"],
                               placed_at=datetime.fromisoformat(o["placed_at"]),
                               buyer_smart=bool(buyers[o["buyer_id"]]["smart"]),
                               currency="PLN", line_items=lis))
    return views


# --- generator integrity -----------------------------------------------------------
def test_generator_referential_integrity():
    d = generate_seed.generate(seed=3)
    offer_ids = {o["id"] for o in d["offers"]}
    buyer_ids = {b["id"] for b in d["buyers"]}
    seller_ids = {s["id"] for s in d["sellers"]}
    order_ids = {o["id"] for o in d["orders"]}
    assert all(it["offer_id"] in offer_ids for it in d["order_items"])
    assert all(o["buyer_id"] in buyer_ids and o["seller_id"] in seller_ids for o in d["orders"])
    assert all(r["order_id"] in order_ids for r in d["returns"])
    assert all(it["order_id"] in order_ids for it in d["order_items"])
    # new ID ranges only — never collides with the committed seed (ORD-4001..4004, OFR-3001..3005)
    assert all(o["id"].startswith("ORD-41") for o in d["orders"])
    assert all(o["id"].startswith("OFR-31") for o in d["offers"])
    assert len(order_ids) == len(d["orders"])      # ids unique


def test_generator_is_deterministic():
    assert generate_seed.generate(seed=3) == generate_seed.generate(seed=3)
    assert generate_seed.generate(seed=1) != generate_seed.generate(seed=2)


def test_catalog_covers_non_returnable():
    cats = {c["category"] for c in generate_seed.load_catalog()}
    assert {"perishable", "personalized", "digital_unsealed"} <= cats   # rules see non-returnable paths


# --- rules engine sweep over all generated orders ----------------------------------
def test_rules_engine_invariants_over_generated_orders():
    rules = load_config().rules
    blocked = set(rules.returns.non_returnable_categories)
    views = _order_views(generate_seed.generate(seed=7, n_orders=40))
    assert len(views) == 40

    for ov in views:
        # commission: total == items + fee, non-negative, versioned
        cb = compute_commission(ov, rules)
        assert cb.total_commission == cb.items_commission + cb.transaction_fee
        assert cb.total_commission >= Decimal("0.00") and cb.rule_version

        # returns: a returnable order must be DELIVERED, in-window, with a returnable item;
        # free return only for Smart! members
        rd = is_returnable(ov, rules)
        if rd.returnable:
            assert ov.status == "DELIVERED"
            assert rd.days_since_order <= rules.returns.window_days
            assert any(li.category not in blocked for li in ov.line_items)
        if rd.free_return:
            assert rd.returnable and ov.buyer_smart

        # eligibility: every process returns a versioned boolean decision, no exceptions
        for proc in PROCESSES:
            ed = qualifies_for(proc, ov, rules)
            assert isinstance(ed.eligible, bool) and ed.rule_version == rules.version


# --- db merge keeps the canonical seed intact and adds the generated rows ----------
def test_db_init_merges_generated(tmp_path, monkeypatch):
    p = tmp_path / "generated_seed.json"
    generate_seed.write_generated(generate_seed.generate(seed=5, n_orders=10), path=str(p))
    monkeypatch.setattr(db, "_GENERATED_PATH", p)
    db.configure(str(tmp_path / "merged.db"))
    db.init_db(force=True, include_generated=True)
    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 4 + 10   # canonical + generated
        assert conn.execute("SELECT 1 FROM orders WHERE id='ORD-4001'").fetchone()   # committed seed intact
        assert conn.execute("SELECT 1 FROM orders WHERE id='ORD-4101'").fetchone()   # generated present
    finally:
        conn.close()
