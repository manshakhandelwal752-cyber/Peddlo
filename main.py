"""Peddlo backend (MVP) - hawkers on routes, pre-orders, and gig-worker bookings.

Run:  uvicorn main:app --reload
Docs: http://127.0.0.1:8000/docs
"""
import math
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

DB_PATH = os.getenv("PEDDLO_DB", "peddlo.db")
ORDER_CUTOFF_MIN = 15  # orders close this many minutes before the hawker's ETA
# Centre of the demo area. The 'Use demo location' button and seed.py both use it,
# so the demo works wherever you actually are. Change it to your own area if you like.
DEMO_LOCATION = (28.7041, 77.1025)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('customer','hawker','gig')),
    society TEXT,
    lat REAL,
    lng REAL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hawker_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    price REAL NOT NULL,
    stock REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hawker_id INTEGER NOT NULL REFERENCES users(id),
    society TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    eta TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','arrived','done','cancelled')),
    lat REAL,
    lng REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    hawker_id INTEGER NOT NULL REFERENCES users(id),
    stop_id INTEGER NOT NULL REFERENCES stops(id),
    status TEXT NOT NULL DEFAULT 'placed'
        CHECK (status IN ('placed','accepted','delivered','cancelled')),
    total REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty REAL NOT NULL,
    price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gig_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL REFERENCES users(id),
    skill TEXT NOT NULL,
    rate REAL NOT NULL,
    rate_unit TEXT NOT NULL DEFAULT 'per_hour'
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    service_id INTEGER NOT NULL REFERENCES gig_services(id),
    scheduled_for TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested','accepted','completed','cancelled'))
);
"""


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # upgrade an older peddlo.db that was created before location support
    for table in ("users", "stops"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in ("lat", "lng"):
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Peddlo API", version="0.2.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).parent / "index.html")


# ---------- schemas ----------
class UserIn(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=7)
    role: Literal["customer", "hawker", "gig"]
    society: Optional[str] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class ProductIn(BaseModel):
    name: str = Field(min_length=1)
    unit: str = "kg"
    price: float = Field(gt=0)
    stock: float = Field(ge=0)


class StopIn(BaseModel):
    society: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    eta: datetime
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)


class StopEtaIn(BaseModel):
    eta: datetime


class StopStatusIn(BaseModel):
    status: Literal["scheduled", "arrived", "done", "cancelled"]


class ItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0)


class OrderIn(BaseModel):
    customer_id: int
    stop_id: int
    items: list[ItemIn] = Field(min_length=1)


class OrderStatusIn(BaseModel):
    status: Literal["accepted", "delivered", "cancelled"]


class ServiceIn(BaseModel):
    skill: str = Field(min_length=1)
    rate: float = Field(gt=0)
    rate_unit: Literal["per_hour", "per_day", "per_job"] = "per_hour"


class BookingIn(BaseModel):
    customer_id: int
    service_id: int
    scheduled_for: datetime
    note: Optional[str] = None


class BookingStatusIn(BaseModel):
    status: Literal["accepted", "completed", "cancelled"]


# ---------- helpers ----------
def require_user(db: sqlite3.Connection, user_id: int, role: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"User {user_id} not found")
    if row["role"] != role:
        raise HTTPException(400, f"User {user_id} is not a {role}")
    return row


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line distance between two GPS points, in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


# ---------- users ----------
@app.post("/users", status_code=201)
def create_user(body: UserIn, db: sqlite3.Connection = Depends(get_db)):
    try:
        cur = db.execute(
            "INSERT INTO users (name, phone, role, society, lat, lng) VALUES (?,?,?,?,?,?)",
            (body.name, body.phone, body.role, body.society, body.lat, body.lng),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Phone number already registered")
    return {"id": cur.lastrowid, **body.model_dump()}


@app.get("/config")
def config():
    return {"lat": DEMO_LOCATION[0], "lng": DEMO_LOCATION[1], "name": "Demo area"}


@app.patch("/users/{user_id}/location")
def set_location(user_id: int, body: LocationIn, db: sqlite3.Connection = Depends(get_db)):
    cur = db.execute("UPDATE users SET lat = ?, lng = ? WHERE id = ?", (body.lat, body.lng, user_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "User not found")
    return {"id": user_id, "lat": body.lat, "lng": body.lng}


@app.get("/users/by-phone/{phone}")
def get_user_by_phone(phone: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    if row is None:
        raise HTTPException(404, "User not found")
    return dict(row)


@app.get("/users/{user_id}")
def get_user(user_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "User not found")
    return dict(row)


# ---------- hawker products ----------
@app.post("/hawkers/{hawker_id}/products", status_code=201)
def add_product(hawker_id: int, body: ProductIn, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, hawker_id, "hawker")
    cur = db.execute(
        "INSERT INTO products (hawker_id, name, unit, price, stock) VALUES (?,?,?,?,?)",
        (hawker_id, body.name, body.unit, body.price, body.stock),
    )
    return {"id": cur.lastrowid, "hawker_id": hawker_id, **body.model_dump()}


@app.get("/hawkers/{hawker_id}/products")
def list_products(hawker_id: int, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, hawker_id, "hawker")
    return rows(db.execute("SELECT * FROM products WHERE hawker_id = ?", (hawker_id,)))


# ---------- hawker route / stops ----------
@app.post("/hawkers/{hawker_id}/stops", status_code=201)
def add_stop(hawker_id: int, body: StopIn, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, hawker_id, "hawker")
    cur = db.execute(
        "INSERT INTO stops (hawker_id, society, sequence, eta, lat, lng) VALUES (?,?,?,?,?,?)",
        (hawker_id, body.society, body.sequence, body.eta.isoformat(), body.lat, body.lng),
    )
    return {"id": cur.lastrowid, "hawker_id": hawker_id, "status": "scheduled",
            "society": body.society, "sequence": body.sequence, "eta": body.eta.isoformat(),
            "lat": body.lat, "lng": body.lng}


@app.get("/stops/nearby")
def nearby_stops(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(3.0, gt=0, le=50),
    db: sqlite3.Connection = Depends(get_db),
):
    """Residents: hawkers arriving within radius_km of me, nearest first."""
    stops = rows(db.execute(
        "SELECT s.*, u.name AS hawker_name FROM stops s JOIN users u ON u.id = s.hawker_id "
        "WHERE s.status IN ('scheduled','arrived') AND s.lat IS NOT NULL AND s.lng IS NOT NULL"))
    result = []
    for st in stops:
        d = haversine_km(lat, lng, st["lat"], st["lng"])
        if d <= radius_km:
            st["distance_km"] = round(d, 2)
            result.append(st)
    result.sort(key=lambda st: (st["distance_km"], st["eta"]))
    return result


@app.get("/hawkers/{hawker_id}/stops")
def hawker_stops(hawker_id: int, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, hawker_id, "hawker")
    return rows(db.execute("SELECT * FROM stops WHERE hawker_id = ? ORDER BY sequence, eta", (hawker_id,)))


@app.get("/stops")
def find_stops(society: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    """Residents: which hawkers are coming to my society, and when?"""
    sql = (
        "SELECT s.*, u.name AS hawker_name FROM stops s "
        "JOIN users u ON u.id = s.hawker_id "
        "WHERE s.status IN ('scheduled','arrived')"
    )
    params: list = []
    if society:
        sql += " AND s.society = ?"
        params.append(society)
    sql += " ORDER BY s.eta"
    return rows(db.execute(sql, params))


@app.patch("/stops/{stop_id}/eta")
def update_eta(stop_id: int, body: StopEtaIn, db: sqlite3.Connection = Depends(get_db)):
    cur = db.execute("UPDATE stops SET eta = ? WHERE id = ?", (body.eta.isoformat(), stop_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Stop not found")
    return {"id": stop_id, "eta": body.eta.isoformat()}


@app.patch("/stops/{stop_id}/status")
def update_stop_status(stop_id: int, body: StopStatusIn, db: sqlite3.Connection = Depends(get_db)):
    cur = db.execute("UPDATE stops SET status = ? WHERE id = ?", (body.status, stop_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Stop not found")
    return {"id": stop_id, "status": body.status}


@app.get("/stops/{stop_id}/packing-list")
def packing_list(stop_id: int, db: sqlite3.Connection = Depends(get_db)):
    """Hawker: exactly what to load for this stop (less wastage)."""
    if db.execute("SELECT 1 FROM stops WHERE id = ?", (stop_id,)).fetchone() is None:
        raise HTTPException(404, "Stop not found")
    items = rows(db.execute(
        "SELECT p.name, p.unit, SUM(oi.qty) AS total_qty, COUNT(DISTINCT o.id) AS orders "
        "FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id "
        "JOIN products p ON p.id = oi.product_id "
        "WHERE o.stop_id = ? AND o.status != 'cancelled' "
        "GROUP BY p.id ORDER BY p.name", (stop_id,)))
    return {"stop_id": stop_id, "items": items}


# ---------- orders ----------
@app.post("/orders", status_code=201)
def place_order(body: OrderIn, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, body.customer_id, "customer")
    stop = db.execute("SELECT * FROM stops WHERE id = ?", (body.stop_id,)).fetchone()
    if stop is None:
        raise HTTPException(404, "Stop not found")
    if stop["status"] != "scheduled":
        raise HTTPException(400, f"Stop is {stop['status']}; orders are closed")

    cutoff = datetime.fromisoformat(stop["eta"]) - timedelta(minutes=ORDER_CUTOFF_MIN)
    if datetime.now() > cutoff:
        raise HTTPException(400, f"Orders close {ORDER_CUTOFF_MIN} min before arrival")

    lines, total = [], 0.0
    for item in body.items:
        product = db.execute(
            "SELECT * FROM products WHERE id = ? AND hawker_id = ?",
            (item.product_id, stop["hawker_id"]),
        ).fetchone()
        if product is None:
            raise HTTPException(400, f"Product {item.product_id} is not sold by this hawker")
        if product["stock"] < item.qty:
            raise HTTPException(400, f"Only {product['stock']} {product['unit']} of {product['name']} left")
        lines.append((product, item.qty))
        total += product["price"] * item.qty

    cur = db.execute(
        "INSERT INTO orders (customer_id, hawker_id, stop_id, total, created_at) VALUES (?,?,?,?,?)",
        (body.customer_id, stop["hawker_id"], body.stop_id, total, datetime.now().isoformat()),
    )
    order_id = cur.lastrowid
    for product, qty in lines:
        db.execute(
            "INSERT INTO order_items (order_id, product_id, qty, price) VALUES (?,?,?,?)",
            (order_id, product["id"], qty, product["price"]),
        )
        db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, product["id"]))
    return {"id": order_id, "status": "placed", "total": total}


@app.patch("/orders/{order_id}/status")
def update_order_status(order_id: int, body: OrderStatusIn, db: sqlite3.Connection = Depends(get_db)):
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        raise HTTPException(404, "Order not found")
    if order["status"] in ("delivered", "cancelled"):
        raise HTTPException(400, f"Order already {order['status']}")
    if body.status == "cancelled":  # give the stock back
        for it in db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall():
            db.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (it["qty"], it["product_id"]))
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (body.status, order_id))
    return {"id": order_id, "status": body.status}


@app.get("/customers/{customer_id}/orders")
def customer_orders(customer_id: int, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, customer_id, "customer")
    return rows(db.execute(
        "SELECT o.*, s.eta, s.society FROM orders o JOIN stops s ON s.id = o.stop_id "
        "WHERE o.customer_id = ? ORDER BY o.id DESC", (customer_id,)))


# ---------- gig workers ----------
@app.post("/gig/{worker_id}/services", status_code=201)
def add_service(worker_id: int, body: ServiceIn, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, worker_id, "gig")
    cur = db.execute(
        "INSERT INTO gig_services (worker_id, skill, rate, rate_unit) VALUES (?,?,?,?)",
        (worker_id, body.skill, body.rate, body.rate_unit),
    )
    return {"id": cur.lastrowid, "worker_id": worker_id, **body.model_dump()}


@app.get("/gig/nearby")
def nearby_gig(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(5.0, gt=0, le=100),
    skill: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    """Gig workers within radius_km of me, nearest first."""
    sql = ("SELECT g.*, u.name AS worker_name, u.society, u.lat, u.lng FROM gig_services g "
           "JOIN users u ON u.id = g.worker_id WHERE u.lat IS NOT NULL AND u.lng IS NOT NULL")
    params: list = []
    if skill:
        sql += " AND LOWER(g.skill) LIKE ?"
        params.append(f"%{skill.lower()}%")
    result = []
    for g in rows(db.execute(sql, params)):
        d = haversine_km(lat, lng, g["lat"], g["lng"])
        if d <= radius_km:
            g["distance_km"] = round(d, 2)
            result.append(g)
    result.sort(key=lambda g: g["distance_km"])
    return result


@app.get("/gig/services")
def search_services(skill: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    sql = ("SELECT g.*, u.name AS worker_name, u.society, u.lat, u.lng FROM gig_services g "
           "JOIN users u ON u.id = g.worker_id")
    params: list = []
    if skill:
        sql += " WHERE LOWER(g.skill) LIKE ?"
        params.append(f"%{skill.lower()}%")
    return rows(db.execute(sql, params))


@app.get("/gig/{worker_id}/bookings")
def worker_bookings(worker_id: int, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, worker_id, "gig")
    return rows(db.execute(
        "SELECT b.*, g.skill, u.name AS customer_name, u.society FROM bookings b "
        "JOIN gig_services g ON g.id = b.service_id "
        "JOIN users u ON u.id = b.customer_id "
        "WHERE g.worker_id = ? ORDER BY b.scheduled_for", (worker_id,)))


@app.post("/bookings", status_code=201)
def create_booking(body: BookingIn, db: sqlite3.Connection = Depends(get_db)):
    require_user(db, body.customer_id, "customer")
    if db.execute("SELECT 1 FROM gig_services WHERE id = ?", (body.service_id,)).fetchone() is None:
        raise HTTPException(404, "Service not found")
    if body.scheduled_for < datetime.now():
        raise HTTPException(400, "Booking time must be in the future")
    cur = db.execute(
        "INSERT INTO bookings (customer_id, service_id, scheduled_for, note) VALUES (?,?,?,?)",
        (body.customer_id, body.service_id, body.scheduled_for.isoformat(), body.note),
    )
    return {"id": cur.lastrowid, "status": "requested"}


@app.patch("/bookings/{booking_id}/status")
def update_booking_status(booking_id: int, body: BookingStatusIn, db: sqlite3.Connection = Depends(get_db)):
    cur = db.execute("UPDATE bookings SET status = ? WHERE id = ?", (body.status, booking_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Booking not found")
    return {"id": booking_id, "status": body.status}


if __name__ == "__main__":
    import uvicorn

    # Lets you press "Run Python File" in VS Code (or run: python main.py)
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)