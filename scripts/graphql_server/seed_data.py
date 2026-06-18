"""Sample marketplace data (Allegro-style domain). Committed and human-readable —
the source of truth that db.init_db() loads into SQLite. No real data.

Notes that make this look like Allegro:
- Listings are "offers" (Allegro's term), not "products".
- Money is stored as a decimal STRING ("199.00") + implicit PLN currency — never a float.
- IDs are prefixed (BUY-/SEL-/OFR-/ORD-/RET-) so the Relay `node(id:)` field can route by
  type. Real Allegro uses opaque ids; prefixes just keep the demo legible.
- Dates anchored around 2026-06-17 so the returns 14-day window has in- and out-of-window orders.
"""

BUYERS = [
    {"id": "BUY-1001", "login": "anna_k", "smart": True, "locale": "pl"},
    {"id": "BUY-1002", "login": "marek_w", "smart": False, "locale": "pl"},
    {"id": "BUY-1003", "login": "john_d", "smart": True, "locale": "en"},
]

SELLERS = [
    {"id": "SEL-2001", "name": "TechParts PL", "rating": 4.8},
    {"id": "SEL-2002", "name": "ModaStyle", "rating": 4.5},
    {"id": "SEL-2003", "name": "DomiGarden", "rating": 4.2},
]

OFFERS = [
    {"id": "OFR-3001", "name": "Wireless Headphones", "category": "electronics", "price": "199.00", "seller_id": "SEL-2001"},
    {"id": "OFR-3002", "name": "USB-C Charger 65W", "category": "electronics", "price": "89.00", "seller_id": "SEL-2001"},
    {"id": "OFR-3003", "name": "Cotton T-Shirt", "category": "fashion", "price": "49.00", "seller_id": "SEL-2002"},
    {"id": "OFR-3004", "name": "Custom Name Mug", "category": "personalized", "price": "39.00", "seller_id": "SEL-2003"},
    {"id": "OFR-3005", "name": "Fresh Coffee Beans 1kg", "category": "perishable", "price": "69.00", "seller_id": "SEL-2003"},
]

# status uses OrderStatus enum values; delivery_method uses DeliveryMethod enum values.
ORDERS = [
    {"id": "ORD-4001", "buyer_id": "BUY-1001", "seller_id": "SEL-2001", "status": "DELIVERED", "placed_at": "2026-06-10T14:30:00+00:00", "delivery_method": "COURIER"},
    {"id": "ORD-4002", "buyer_id": "BUY-1002", "seller_id": "SEL-2002", "status": "DELIVERED", "placed_at": "2026-05-01T09:15:00+00:00", "delivery_method": "PARCEL_LOCKER"},
    {"id": "ORD-4003", "buyer_id": "BUY-1003", "seller_id": "SEL-2003", "status": "SHIPPED", "placed_at": "2026-06-15T18:45:00+00:00", "delivery_method": "PICKUP_POINT"},
    # second order for BUY-1001 so the orders connection has something to paginate.
    {"id": "ORD-4004", "buyer_id": "BUY-1001", "seller_id": "SEL-2001", "status": "DELIVERED", "placed_at": "2026-04-01T11:00:00+00:00", "delivery_method": "COURIER"},
]

ORDER_ITEMS = [
    {"order_id": "ORD-4001", "offer_id": "OFR-3001", "quantity": 1, "unit_price": "199.00"},
    {"order_id": "ORD-4001", "offer_id": "OFR-3002", "quantity": 2, "unit_price": "89.00"},
    {"order_id": "ORD-4002", "offer_id": "OFR-3003", "quantity": 3, "unit_price": "49.00"},
    {"order_id": "ORD-4003", "offer_id": "OFR-3004", "quantity": 1, "unit_price": "39.00"},
    {"order_id": "ORD-4003", "offer_id": "OFR-3005", "quantity": 1, "unit_price": "69.00"},
    {"order_id": "ORD-4004", "offer_id": "OFR-3002", "quantity": 1, "unit_price": "89.00"},
]

RETURNS = [
    {"id": "RET-5001", "order_id": "ORD-4002", "reason": "Wrong size", "status": "REJECTED", "opened_at": "2026-05-20T10:00:00+00:00"},
]
