"""Peddlo demo data - fills the database with sample hawkers, customers and gig workers.

Run:  python seed.py      (or press the Run button in VS Code)
WARNING: this DELETES everything already in peddlo.db and starts fresh.

Edit the lists below to add your own people, products and stops.
"""
import sqlite3
from datetime import datetime, timedelta

from main import DB_PATH, DEMO_LOCATION, init_db

# ---------------- EDIT YOUR DEMO DATA HERE ----------------
# GPS (lat, lng) of each society. Offsets are around DEMO_LOCATION (see main.py).
# Tip: in Google Maps, right-click a place and click the numbers to copy its coordinates.
C_LAT, C_LNG = DEMO_LOCATION
SOCIETIES = {
    "Green Apartments": (C_LAT + 0.0020, C_LNG + 0.0015),   # ~0.3 km from centre
    "Sunrise Society":  (C_LAT + 0.0080, C_LNG + 0.0075),   # ~1.2 km
    "Lotus Heights":    (C_LAT + 0.0220, C_LNG + 0.0270),   # ~4 km
    "Model Town":       (C_LAT + 0.0120, C_LNG + 0.0900),   # ~10 km (outside most radii)
}

# Stops are scheduled relative to "now" so orders are always open when you demo.
# (Orders close 15 minutes before a stop's arrival time.)
HAWKERS = [
    {
        "name": "Ramesh", "phone": "9000000001",
        "products": [  # name, unit, price, stock
            ("Tomato", "kg", 40, 25), ("Onion", "kg", 30, 30),
            ("Potato", "kg", 25, 40), ("Spinach", "bunch", 20, 15),
        ],
        "stops": [  # society, hours from now
            ("Green Apartments", 2.0), ("Sunrise Society", 3.0), ("Lotus Heights", 4.0),
        ],
    },
    {
        "name": "Sunita", "phone": "9000000002",
        "products": [
            ("Banana", "dozen", 50, 20), ("Apple", "kg", 150, 15),
            ("Pomegranate", "kg", 120, 10), ("Guava", "kg", 60, 12),
        ],
        "stops": [("Green Apartments", 2.5), ("Model Town", 4.0), ("Lotus Heights", 4.5)],
    },
]

CUSTOMERS = [
    {"name": "Anita", "phone": "9000000011", "society": "Green Apartments"},
    {"name": "Rahul", "phone": "9000000012", "society": "Sunrise Society"},
]

GIG_WORKERS = [
    {"name": "Suresh", "phone": "9000000021", "skill": "Plumber", "rate": 300, "unit": "per_hour", "loc": (C_LAT + 0.0010, C_LNG - 0.0010)},
    {"name": "Raju", "phone": "9000000022", "skill": "Electrician", "rate": 350, "unit": "per_hour", "loc": (C_LAT + 0.0060, C_LNG + 0.0050)},
    {"name": "Meena", "phone": "9000000023", "skill": "Home cook", "rate": 600, "unit": "per_job", "loc": (C_LAT + 0.0250, C_LNG + 0.0200)},
    {"name": "Pooja", "phone": "9000000024", "skill": "Tutor", "rate": 250, "unit": "per_hour", "loc": (C_LAT + 0.0550, C_LNG + 0.0950)},
]
# -----------------------------------------------------------


def add_user(db, name, phone, role, society=None, loc=None):
    lat, lng = loc if loc else (None, None)
    cur = db.execute(
        "INSERT INTO users (name, phone, role, society, lat, lng) VALUES (?,?,?,?,?,?)",
        (name, phone, role, society, lat, lng),
    )
    return cur.lastrowid


def main():
    init_db()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    for table in ["order_items", "orders", "bookings", "gig_services", "stops", "products", "users"]:
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM sqlite_sequence")

    now = datetime.now().replace(second=0, microsecond=0)
    first_stop = {}  # hawker name -> (stop_id, hawker_id)
    product_ids = {}  # (hawker name, product name) -> product id

    for h in HAWKERS:
        hid = add_user(db, h["name"], h["phone"], "hawker")
        for name, unit, price, stock in h["products"]:
            cur = db.execute(
                "INSERT INTO products (hawker_id, name, unit, price, stock) VALUES (?,?,?,?,?)",
                (hid, name, unit, price, stock),
            )
            product_ids[(h["name"], name)] = cur.lastrowid
        for seq, (society, hours) in enumerate(h["stops"], start=1):
            eta = (now + timedelta(hours=hours)).isoformat()
            cur = db.execute(
                "INSERT INTO stops (hawker_id, society, sequence, eta, lat, lng) VALUES (?,?,?,?,?,?)",
                (hid, society, seq, eta, *SOCIETIES.get(society, (None, None))),
            )
            first_stop.setdefault(h["name"], (cur.lastrowid, hid, society))

    customer_ids = {}
    for c in CUSTOMERS:
        customer_ids[c["name"]] = add_user(db, c["name"], c["phone"], "customer", c["society"], SOCIETIES.get(c["society"]))

    for g in GIG_WORKERS:
        wid = add_user(db, g["name"], g["phone"], "gig", loc=g["loc"])
        db.execute(
            "INSERT INTO gig_services (worker_id, skill, rate, rate_unit) VALUES (?,?,?,?)",
            (wid, g["skill"], g["rate"], g["unit"]),
        )

    # One sample pre-order so the hawker's packing list isn't empty in the demo:
    # Anita orders 2 kg tomato + 1 kg onion from Ramesh's first stop.
    if "Ramesh" in first_stop and "Anita" in customer_ids:
        stop_id, hid, _ = first_stop["Ramesh"]
        items = [(product_ids[("Ramesh", "Tomato")], 2, 40), (product_ids[("Ramesh", "Onion")], 1, 30)]
        total = sum(qty * price for _, qty, price in items)
        cur = db.execute(
            "INSERT INTO orders (customer_id, hawker_id, stop_id, total, created_at) VALUES (?,?,?,?,?)",
            (customer_ids["Anita"], hid, stop_id, total, datetime.now().isoformat()),
        )
        for pid, qty, price in items:
            db.execute(
                "INSERT INTO order_items (order_id, product_id, qty, price) VALUES (?,?,?,?)",
                (cur.lastrowid, pid, qty, price),
            )
            db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, pid))

    db.commit()
    db.close()

    print("Demo data loaded into", DB_PATH)
    print("\nLog in with these phone numbers (no password needed):")
    for h in HAWKERS:
        print(f"  Hawker    {h['name']:8} {h['phone']}")
    for c in CUSTOMERS:
        print(f"  Customer  {c['name']:8} {c['phone']}  (society: {c['society']})")
    for g in GIG_WORKERS:
        print(f"  Gig       {g['name']:8} {g['phone']}  ({g['skill']})")


if __name__ == "__main__":
    main()