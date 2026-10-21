import os
import tempfile
from datetime import datetime, timedelta

os.environ["PEDDLO_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def test_full_flow():
    with TestClient(app) as c:
        hawker = c.post("/users", json={"name": "Ramesh", "phone": "9000000001", "role": "hawker"}).json()
        cust = c.post("/users", json={"name": "Anita", "phone": "9000000002", "role": "customer", "society": "Green Apts"}).json()
        gig = c.post("/users", json={"name": "Suresh", "phone": "9000000003", "role": "gig"}).json()

        tomato = c.post(f"/hawkers/{hawker['id']}/products", json={"name": "Tomato", "unit": "kg", "price": 40, "stock": 10}).json()
        eta = (datetime.now() + timedelta(hours=2)).isoformat()
        stop = c.post(f"/hawkers/{hawker['id']}/stops", json={"society": "Green Apts", "sequence": 1, "eta": eta}).json()

        assert len(c.get("/stops", params={"society": "Green Apts"}).json()) == 1

        order = c.post("/orders", json={"customer_id": cust["id"], "stop_id": stop["id"], "items": [{"product_id": tomato["id"], "qty": 3}]})
        assert order.status_code == 201 and order.json()["total"] == 120

        assert c.get(f"/stops/{stop['id']}/packing-list").json()["items"][0]["total_qty"] == 3
        assert c.get(f"/hawkers/{hawker['id']}/products").json()[0]["stock"] == 7

        too_much = c.post("/orders", json={"customer_id": cust["id"], "stop_id": stop["id"], "items": [{"product_id": tomato["id"], "qty": 99}]})
        assert too_much.status_code == 400

        c.patch(f"/orders/{order.json()['id']}/status", json={"status": "cancelled"})
        assert c.get(f"/hawkers/{hawker['id']}/products").json()[0]["stock"] == 10

        late = (datetime.now() + timedelta(minutes=5)).isoformat()
        c.patch(f"/stops/{stop['id']}/eta", json={"eta": late})
        closed = c.post("/orders", json={"customer_id": cust["id"], "stop_id": stop["id"], "items": [{"product_id": tomato["id"], "qty": 1}]})
        assert closed.status_code == 400

        svc = c.post(f"/gig/{gig['id']}/services", json={"skill": "Plumber", "rate": 300}).json()
        assert len(c.get("/gig/services", params={"skill": "plum"}).json()) == 1
        when = (datetime.now() + timedelta(days=1)).isoformat()
        assert c.post("/bookings", json={"customer_id": cust["id"], "service_id": svc["id"], "scheduled_for": when}).status_code == 201


def test_ui_and_new_endpoints():
    with TestClient(app) as c:
        home = c.get("/")
        assert home.status_code == 200 and "PEDDLO" in home.text

        h = c.post("/users", json={"name": "Geeta", "phone": "9111111111", "role": "hawker"}).json()
        assert c.get("/users/by-phone/9111111111").json()["id"] == h["id"]
        assert c.get("/users/by-phone/0000000").status_code == 404

        eta = (datetime.now() + timedelta(hours=3)).isoformat()
        c.post(f"/hawkers/{h['id']}/stops", json={"society": "Blue Apts", "sequence": 1, "eta": eta})
        assert len(c.get(f"/hawkers/{h['id']}/stops").json()) == 1

        g = c.post("/users", json={"name": "Raju", "phone": "9222222222", "role": "gig"}).json()
        assert c.get(f"/gig/{g['id']}/bookings").json() == []


def test_location_features():
    with TestClient(app) as c:
        cfg = c.get("/config").json()
        lat, lng = cfg["lat"], cfg["lng"]

        h = c.post("/users", json={"name": "Loc Hawker", "phone": "9333333333", "role": "hawker"}).json()
        eta = (datetime.now() + timedelta(hours=3)).isoformat()
        c.post(f"/hawkers/{h['id']}/stops", json={"society": "Near", "sequence": 1, "eta": eta, "lat": lat + 0.002, "lng": lng})
        c.post(f"/hawkers/{h['id']}/stops", json={"society": "Far", "sequence": 2, "eta": eta, "lat": lat + 0.2, "lng": lng})
        c.post(f"/hawkers/{h['id']}/stops", json={"society": "NoGPS", "sequence": 3, "eta": eta})

        near = c.get("/stops/nearby", params={"lat": lat, "lng": lng, "radius_km": 3}).json()
        assert [s["society"] for s in near] == ["Near"] and near[0]["distance_km"] < 1
        wide = c.get("/stops/nearby", params={"lat": lat, "lng": lng, "radius_km": 50}).json()
        assert [s["society"] for s in wide] == ["Near", "Far"]  # nearest first, no-GPS stop excluded

        g = c.post("/users", json={"name": "Loc Gig", "phone": "9444444444", "role": "gig"}).json()
        assert c.patch(f"/users/{g['id']}/location", json={"lat": lat + 0.003, "lng": lng}).status_code == 200
        c.post(f"/gig/{g['id']}/services", json={"skill": "Painter", "rate": 400})
        found = c.get("/gig/nearby", params={"lat": lat, "lng": lng, "radius_km": 2, "skill": "paint"}).json()
        assert len(found) == 1 and found[0]["distance_km"] < 1
        assert c.get("/gig/nearby", params={"lat": 10.0, "lng": 10.0}).json() == []