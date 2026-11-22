def addr(**kw):
    return {"label": "home", "line1": "12 MG Road", "postal_code": "560001", "latitude": 12.9716, "longitude": 77.5946, "location_source": "gps", **kw}


def test_saved_addresses_crud_and_default_rules(cafe):
    h = cafe.customer("Asha")
    first = cafe.post("/customers/me/addresses", addr(), h=h)
    assert first.status_code == 201 and first.json()["is_default"] is True  # the first one is the default automatically
    second = cafe.post("/customers/me/addresses", addr(label="work", line1="Office Tower"), h=h).json()
    assert second["is_default"] is False
    assert cafe.patch(f"/customers/me/addresses/{second['id']}", {"is_default": True}, h=h).json()["is_default"] is True
    listing = cafe.get("/customers/me/addresses", h=h).json()
    assert [a["is_default"] for a in listing] == [True, False] and listing[0]["id"] == second["id"]
    assert cafe.patch(f"/customers/me/addresses/{second['id']}", {"landmark": "Near the metro", "latitude": 12.98}, h=h).json()["landmark"] == "Near the metro"
    assert cafe.delete(f"/customers/me/addresses/{second['id']}", h=h).status_code == 200  # deleting the default promotes another
    left = cafe.get("/customers/me/addresses", h=h).json()
    assert len(left) == 1 and left[0]["id"] == first.json()["id"] and left[0]["is_default"] is True


def test_address_validation_and_privacy(cafe):
    h = cafe.customer("Asha")
    assert cafe.post("/customers/me/addresses", addr(latitude=95), h=h).status_code == 422
    assert cafe.post("/customers/me/addresses", addr(label="boat"), h=h).status_code == 422
    assert cafe.post("/customers/me/addresses", {"line1": "x"}, h=h).status_code == 422
    mine = cafe.post("/customers/me/addresses", addr(), h=h).json()
    other = cafe.customer("Ben", phone="9111111111")
    assert cafe.patch(f"/customers/me/addresses/{mine['id']}", {"line1": "Hacked street"}, h=other).status_code == 404
    assert cafe.delete(f"/customers/me/addresses/{mine['id']}", h=other).status_code == 404
    assert cafe.get("/customers/me/addresses", h=other).json() == []
    assert cafe.get("/customers/me/addresses").status_code == 401


def test_address_limit(cafe):
    h = cafe.customer("Asha")
    for i in range(10):
        assert cafe.post("/customers/me/addresses", addr(line1=f"Street {i}"), h=h).status_code == 201
    r = cafe.post("/customers/me/addresses", addr(line1="One too many"), h=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "too_many_addresses"


def test_order_keeps_its_own_copy_of_the_address(cafe):
    h = cafe.customer("Asha")
    saved = cafe.post("/customers/me/addresses", addr(), h=h).json()
    o = cafe.place("delivery", h=h, delivery_address={"line1": saved["line1"], "postal_code": saved["postal_code"], "latitude": saved["latitude"], "longitude": saved["longitude"]}).json()["order"]
    cafe.patch(f"/customers/me/addresses/{saved['id']}", {"line1": "Moved house"}, h=h)
    assert cafe.get(f"/admin/orders/{o['id']}", h=cafe.admin).json()["delivery_address"]["line1"] == "12 MG Road"


def test_admin_customer_list_search_detail_and_disable(cafe):
    ha = cafe.customer("Asha", email="asha.list@example.com", phone="9876543210")
    cafe.customer("Ben", email="ben.list@example.com", phone="9111111111")
    o = cafe.place("pickup", h=ha).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready", "completed")
    allc = cafe.get("/admin/customers", h=cafe.admin).json()
    assert allc["total"] == 2
    found = cafe.get("/admin/customers?q=asha", h=cafe.admin).json()
    assert [c["name"] for c in found["items"]] == ["Asha"] and found["items"][0]["orders_count"] == 1 and found["items"][0]["total_spent_paise"] > 0
    assert cafe.get("/admin/customers?q=9111", h=cafe.admin).json()["items"][0]["name"] == "Ben"
    detail = cafe.get(f"/admin/customers/{found['items'][0]['id']}", h=cafe.admin).json()
    assert detail["orders"][0]["order_number"] == o["order_number"]
    assert cafe.get("/admin/customers", h=ha).status_code == 403
    cid = found["items"][0]["id"]
    assert cafe.patch(f"/admin/customers/{cid}/active", {"is_active": False}, h=cafe.admin).status_code == 200
    assert cafe.post("/auth/customer/login", {"email": "asha.list@example.com", "password": "Passw0rdPass1"}).status_code == 401
