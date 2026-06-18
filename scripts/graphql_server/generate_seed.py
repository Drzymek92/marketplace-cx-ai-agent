"""Generate additional referentially-consistent marketplace data to populate the demo.

Pairs with the synthetic-data-factory for the OFFER CATALOGUE ONLY: when a factory CSV export is
supplied (via --factory-csv) it's used for realistic listing names; otherwise a built-in catalogue
is used so this works fully offline (the gateway isn't always reachable). The relational graph
(sellers, buyers, orders, line items, returns) is built deterministically here with full referential
integrity and a deliberate spread of statuses / dates / categories so the rules engine and agent are
exercised across many cases — in/out of the 14-day returns and 30-day dispute windows, Smart! and
non-Smart! buyers, returnable and non-returnable categories.

Which factory domain: the catalogue source is the FLAT `marketplace_offers` domain, whose CSV has
the `name`/`category`/`price_pln` columns `load_catalog()` expects. The factory also has a RELATIONAL
`marketplace` domain (multi-table, FK-linked, `price` column) — that bundle is a standalone
integration/FK fixture and is NOT a catalogue source here; feeding its offers CSV to --factory-csv
yields 0 usable rows and falls back (with a warning) to the built-in catalogue.

Writes scripts/inputs/generated_seed.json; `db.init_db(include_generated=True)` merges it on top of
the canonical committed seed (new ID ranges, so it never collides with or alters ORD-4001…4004 etc.
that the tests and eval baseline depend on). Deterministic given `seed` (uses random.Random).
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # local scripts/ beats site-packages shadow

from scripts.logger import get_logger

logger = get_logger("generate_seed")

# Anchor matches seed_data.py (~2026-06-17) so the returns/dispute windows behave as designed.
_ANCHOR = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
_GENERATED_PATH = Path(__file__).resolve().parents[2] / "scripts" / "inputs" / "generated_seed.json"

NON_RETURNABLE = {"perishable", "personalized", "digital_unsealed"}

# Built-in catalogue (fallback when no factory export is available). Spans every category, incl. the
# non-returnable ones, so is_returnable/eligibility see both paths.
_BUILTIN_CATALOG = [
    {"name": "Wireless Noise-Cancelling Headphones X200", "category": "electronics", "price_pln": 399},
    {"name": "USB-C 65W GaN Charger", "category": "electronics", "price_pln": 89},
    {"name": "4K Action Camera Pro", "category": "electronics", "price_pln": 749},
    {"name": "Mechanical Keyboard TKL Brown", "category": "electronics", "price_pln": 329},
    {"name": "Organic Cotton Crewneck T-Shirt", "category": "fashion", "price_pln": 59},
    {"name": "Winter Down Jacket Slim", "category": "fashion", "price_pln": 459},
    {"name": "Leather Chelsea Boots", "category": "fashion", "price_pln": 389},
    {"name": "Stainless Steel 10-Piece Cookware Set", "category": "home", "price_pln": 449},
    {"name": "Ceramic Knife Block Set", "category": "home", "price_pln": 199},
    {"name": "Memory Foam Pillow 2-Pack", "category": "home", "price_pln": 119},
    {"name": "Yoga Mat 6mm Non-Slip", "category": "sports", "price_pln": 79},
    {"name": "Adjustable Dumbbell 24kg", "category": "sports", "price_pln": 549},
    {"name": "Four-Person Camping Tent", "category": "sports", "price_pln": 399},
    {"name": "Natural Vitamin C Face Serum", "category": "beauty", "price_pln": 99},
    {"name": "Hair Dryer Ionic 2200W", "category": "beauty", "price_pln": 169},
    {"name": "Wooden Building Blocks 100-Piece", "category": "toys", "price_pln": 129},
    {"name": "Remote Control Off-Road Car", "category": "toys", "price_pln": 219},
    {"name": "Robotic Lawn Mower G3", "category": "garden", "price_pln": 2899},
    {"name": "Cordless Hedge Trimmer 20V", "category": "garden", "price_pln": 349},
    {"name": "Fresh Roasted Arabica Coffee Beans 1kg", "category": "perishable", "price_pln": 69},
    {"name": "Artisan Chocolate Box 24-Piece", "category": "perishable", "price_pln": 89},
    {"name": "Personalized Engraved Oak Photo Frame", "category": "personalized", "price_pln": 89},
    {"name": "Custom-Printed Name T-Shirt", "category": "personalized", "price_pln": 75},
    {"name": "Antivirus Pro 1-Year License Key", "category": "digital_unsealed", "price_pln": 129},
    {"name": "Strategy Game Deluxe Download", "category": "digital_unsealed", "price_pln": 159},
]

_SELLER_NAMES = ["TechParts PL", "ModaStyle", "DomiGarden", "SportasPro", "UrodaShop", "ZabawkiSwiat"]
_REASONS = ["Wrong size", "Damaged in transit", "Changed my mind", "Not as described", "Faulty item"]
_STATUSES = ["NEW", "PAID", "PROCESSING", "SHIPPED", "DELIVERED", "DELIVERED", "DELIVERED", "CANCELLED"]
_METHODS = ["COURIER", "PARCEL_LOCKER", "PICKUP_POINT"]
_RETURN_STATUSES = ["REQUESTED", "APPROVED", "REJECTED", "REFUNDED"]
# day offsets from the anchor — span in-window (≤14), edge (15–30), and out (>30)
_DAY_OFFSETS = [1, 3, 6, 9, 12, 18, 25, 40, 70, 120]


def load_catalog(factory_csv: Optional[str] = None) -> list[dict]:
    """Use a non-empty factory export if given, else the built-in catalogue."""
    if factory_csv:
        p = Path(factory_csv)
        if p.exists():
            rows = list(csv.DictReader(p.read_text(encoding="utf-8").splitlines()))
            cat = [{"name": r["name"], "category": r["category"], "price_pln": int(float(r["price_pln"]))}
                   for r in rows if r.get("name") and r.get("category") and r.get("price_pln")]
            if cat:
                return cat
            logger.warning(f"factory CSV {factory_csv} yielded 0 usable rows "
                           f"(need name/category/price_pln columns); using built-in catalogue")
        else:
            logger.warning(f"factory CSV {factory_csv} not found; using built-in catalogue")
    return list(_BUILTIN_CATALOG)


def _money(pln: int) -> str:
    return f"{int(pln)}.00"


def generate(seed: int = 42, *, n_sellers: int = 6, n_buyers: int = 12, n_orders: int = 40,
             catalog: Optional[list[dict]] = None) -> dict:
    rng = random.Random(seed)
    catalog = catalog or load_catalog()

    sellers = [{"id": f"SEL-21{i:02d}", "name": _SELLER_NAMES[(i - 1) % len(_SELLER_NAMES)],
                "rating": round(rng.uniform(3.8, 5.0), 1)} for i in range(1, n_sellers + 1)]

    offers = [{"id": f"OFR-31{i:02d}", "name": item["name"], "category": item["category"],
               "price": _money(item["price_pln"]), "seller_id": rng.choice(sellers)["id"]}
              for i, item in enumerate(catalog, start=1)]

    locales = ["pl", "pl", "en"]
    buyers = [{"id": f"BUY-11{i:02d}", "login": f"user_{i:02d}", "smart": rng.random() < 0.5,
               "locale": rng.choice(locales)} for i in range(1, n_buyers + 1)]

    orders, order_items, returns = [], [], []
    rid = 1
    for i in range(1, n_orders + 1):
        oid = f"ORD-41{i:02d}"
        buyer = rng.choice(buyers)
        seller = rng.choice(sellers)
        status = rng.choice(_STATUSES)
        days = rng.choice(_DAY_OFFSETS)
        placed = (_ANCHOR - timedelta(days=days)).isoformat()
        orders.append({"id": oid, "buyer_id": buyer["id"], "seller_id": seller["id"],
                       "status": status, "placed_at": placed, "delivery_method": rng.choice(_METHODS)})

        seller_offers = [o for o in offers if o["seller_id"] == seller["id"]] or offers
        for o in rng.sample(seller_offers, min(rng.randint(1, 3), len(seller_offers))):
            order_items.append({"order_id": oid, "offer_id": o["id"],
                                "quantity": rng.randint(1, 3), "unit_price": o["price"]})

        if status == "DELIVERED" and rng.random() < 0.3:
            returns.append({"id": f"RET-51{rid:02d}", "order_id": oid, "reason": rng.choice(_REASONS),
                            "status": rng.choice(_RETURN_STATUSES),
                            "opened_at": (_ANCHOR - timedelta(days=max(0, days - 2))).isoformat()})
            rid += 1

    return {"buyers": buyers, "sellers": sellers, "offers": offers,
            "orders": orders, "order_items": order_items, "returns": returns}


def write_generated(data: Optional[dict] = None, path: Optional[str] = None, seed: int = 42) -> str:
    data = data if data is not None else generate(seed=seed)
    out = Path(path) if path else _GENERATED_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate referentially-consistent demo seed data.")
    parser.add_argument("--factory-csv",
                        help="offer-catalogue CSV from synthetic-data-factory (marketplace_offers domain; "
                             "needs name/category/price_pln columns)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for deterministic output")
    args = parser.parse_args()

    try:
        d = generate(seed=args.seed, catalog=load_catalog(args.factory_csv))
        p = write_generated(d, seed=args.seed)
        logger.info(f"wrote {p}: {len(d['buyers'])} buyers, {len(d['sellers'])} sellers, "
                    f"{len(d['offers'])} offers, {len(d['orders'])} orders, "
                    f"{len(d['order_items'])} items, {len(d['returns'])} returns")
    except Exception:
        logger.exception("seed generation failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
