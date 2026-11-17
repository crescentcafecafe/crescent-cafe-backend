import io

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_menu_crud_and_availability(cafe):
    a, i = cafe.admin, cafe.ids
    menu = cafe.get("/menu").json()["categories"][0]["items"]
    names = {m["name"]: m for m in menu}
    assert set(names) == {"Latte", "Cake", "Soup"} and names["Soup"]["is_available"] is False
    # availability switch is instant and visible to customers
    assert cafe.put(f"/admin/menu/items/{i['cake']}/availability", {"is_available": False}, h=a).json()["is_available"] is False
    assert {m["name"]: m for m in cafe.get("/menu").json()["categories"][0]["items"]}["Cake"]["is_available"] is False
    # edit + archive (archived items disappear from the public menu but stay for history)
    assert cafe.patch(f"/admin/menu/items/{i['cake']}", {"price_paise": 12000, "name": "Choco Cake"}, h=a).json()["price_paise"] == 12000
    assert cafe.delete(f"/admin/menu/items/{i['cake']}", h=a).status_code == 200
    assert "Choco Cake" not in {m["name"] for m in cafe.get("/menu").json()["categories"][0]["items"]}
    assert any(m["is_archived"] for m in cafe.get("/admin/menu", h=a).json()["categories"][0]["items"])


def test_menu_admin_only(cafe):
    ch = cafe.customer()
    assert cafe.post("/admin/menu/items", {"category_id": cafe.ids["cat"], "name": "X", "price_paise": 100}, h=ch).status_code == 403
    assert cafe.put(f"/admin/menu/items/{cafe.ids['cake']}/availability", {"is_available": False}).status_code == 401


def test_category_reorder_and_item_validation(cafe):
    a = cafe.admin
    c2 = cafe.post("/admin/menu/categories", {"name": "Snacks"}, h=a).json()["id"]
    assert cafe.post("/admin/menu/categories/reorder", {"ids": [c2, cafe.ids["cat"]]}, h=a).status_code == 200
    assert [c["name"] for c in cafe.get("/admin/menu", h=a).json()["categories"]] == ["Snacks", "Coffee"]
    assert cafe.post("/admin/menu/items", {"category_id": c2, "name": "Bad", "price_paise": -5}, h=a).status_code == 422
    assert cafe.post("/admin/menu/items", {"category_id": "00000000-0000-0000-0000-000000000000", "name": "Bad", "price_paise": 5}, h=a).status_code == 422


def test_item_image_upload_validates_content(cafe):
    a, iid = cafe.admin, cafe.ids["cake"]
    r = cafe.post(f"/admin/menu/items/{iid}/image", h=a, files={"file": ("cake.png", io.BytesIO(PNG), "image/png")})
    assert r.status_code == 200 and r.json()["image_url"].startswith("https://cdn.test/")
    first = list(cafe.storage.files)
    assert len(first) == 1
    # replacing deletes the old object
    cafe.post(f"/admin/menu/items/{iid}/image", h=a, files={"file": ("c.png", io.BytesIO(PNG), "image/png")})
    assert len(cafe.storage.files) == 1 and list(cafe.storage.files) != first
    # a text file renamed .png is rejected (content is sniffed)
    bad = cafe.post(f"/admin/menu/items/{iid}/image", h=a, files={"file": ("evil.png", io.BytesIO(b"<script>alert(1)</script>"), "image/png")})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_image"
    big = cafe.post(f"/admin/menu/items/{iid}/image", h=a, files={"file": ("big.png", io.BytesIO(PNG + b"0" * (6 * 1024 * 1024)), "image/png")})
    assert big.json()["error"]["code"] == "image_too_large"
    assert cafe.get("/menu/items/" + iid).json()["image_url"].startswith("https://cdn.test/")


def test_table_crud_unique_number_and_no_limit(cafe):
    a = cafe.admin
    assert cafe.post("/admin/tables", {"number": "1"}, h=a).status_code == 409  # duplicate number
    for n in range(2, 60):  # no hard-coded table limit
        assert cafe.post("/admin/tables", {"number": str(n)}, h=a).status_code == 201
    assert len(cafe.get("/admin/tables", h=a).json()) == 59
    t = cafe.post("/admin/tables", {"number": "VIP", "capacity": 8, "area": "Roof", "notes": "quiet"}, h=a).json()
    assert cafe.patch(f"/admin/tables/{t['id']}", {"is_archived": True}, h=a).json()["is_archived"] is True
    assert cafe.post("/admin/tables", {"number": "VIP"}, h=a).status_code == 201  # archived numbers can be reused


def test_qr_resolve_regenerate_and_revoke(cafe):
    a, i = cafe.admin, cafe.ids
    r = cafe.post("/qr/resolve", {"token": i["qr"]})
    assert r.status_code == 200 and r.json()["table"]["number"] == "1"
    old_session_token = r.json()["table_session_token"]
    # plaintext token is never stored: only a hash exists in the DB
    from sqlalchemy import select
    from app.db.session import session_scope
    from app.models.tables import TableQrToken

    async def tokens():
        async with session_scope() as db:
            return [t.token_hash for t in (await db.execute(select(TableQrToken))).scalars().all()]
    assert i["qr"] not in cafe.call(tokens)
    # regenerate: the old token dies immediately, old table-session tokens stop placing orders
    new = cafe.post(f"/admin/tables/{i['table']}/qr", h=a).json()
    assert new["url"].startswith("https://order.example.com/t/") and new["token"] != i["qr"]
    assert cafe.post("/qr/resolve", {"token": i["qr"]}).status_code == 404
    assert cafe.post("/qr/resolve", {"token": new["token"]}).status_code == 200
    r = cafe.post("/orders", {"order_type": "dine_in", "payment_method": "cash", "items": cafe.cart(), "table_session_token": old_session_token, "customer_name": "G", "customer_phone": "9333333333"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "qr_invalid"
    # explicit revoke
    assert cafe.post(f"/admin/tables/{i['table']}/qr/revoke", {"reason": "photo leaked online"}, h=a).json()["revoked"] == 1
    assert cafe.post("/qr/resolve", {"token": new["token"]}).status_code == 404


def test_qr_endpoints_admin_only(cafe):
    ch = cafe.customer()
    assert cafe.post(f"/admin/tables/{cafe.ids['table']}/qr", h=ch).status_code == 403
    assert cafe.post("/admin/tables/qr/bulk", {"confirm_rotate": True}, h=ch).status_code == 403


def test_bulk_qr_requires_confirmation_and_rotates(cafe):
    a = cafe.admin
    cafe.post("/admin/tables", {"number": "2"}, h=a)
    assert cafe.post("/admin/tables/qr/bulk", {}, h=a).status_code == 422
    r = cafe.post("/admin/tables/qr/bulk", {"confirm_rotate": True}, h=a).json()
    assert r["count"] == 2 and all(x["url"].startswith("https://order.example.com/t/") for x in r["items"])
    assert cafe.post("/qr/resolve", {"token": cafe.ids["qr"]}).status_code == 404


def test_table_pin_optional_check(cafe):
    a, i = cafe.admin, cafe.ids
    cafe.patch(f"/admin/tables/{i['table']}", {"pin": "4321"}, h=a)
    assert cafe.post("/qr/resolve", {"token": i["qr"]}).json()["error"]["code"] == "table_pin_required"
    assert cafe.post("/qr/resolve", {"token": i["qr"], "pin": "0000"}).status_code == 401
    assert cafe.post("/qr/resolve", {"token": i["qr"], "pin": "4321"}).status_code == 200


def test_same_table_orders_are_separate_and_linked(cafe):
    a = cafe.admin
    h1, h2 = cafe.customer("Asha"), cafe.customer("Bala", phone="9111111111")
    tok = cafe.table_token()
    o1 = cafe.place("dine_in", h=h1, table_session_token=tok, kind="latte").json()["order"]
    o2 = cafe.place("dine_in", h=h2, table_session_token=tok, kind="cake", qty=2).json()["order"]
    g = cafe.place("dine_in", table_session_token=tok, customer_name="Guest", customer_phone="9222222222").json()
    assert o1["id"] != o2["id"] != g["order"]["id"]
    assert o1["table_session_id"] == o2["table_session_id"] == g["order"]["table_session_id"]
    assert o1["table_label"].startswith("Table 1")
    sess_orders = cafe.get(f"/admin/table-sessions/{o1['table_session_id']}/orders", h=a).json()
    assert len(sess_orders) == 3 and len({o["id"] for o in sess_orders}) == 3  # separately billed
    assert {o["total_paise"] for o in sess_orders} == {o1["total_paise"], o2["total_paise"], g["order"]["total_paise"]}
    # customers never see each other's orders
    assert cafe.get(f"/orders/{o2['id']}", h=h1).status_code == 404
    assert cafe.get(f"/orders/{o1['id']}", h=h1).status_code == 200


def test_session_close_transfer_and_history_survives_qr_regen(cafe):
    a, i = cafe.admin, cafe.ids
    h = cafe.customer()
    tok = cafe.table_token()
    o = cafe.place("dine_in", h=h, table_session_token=tok).json()["order"]
    sid = o["table_session_id"]
    t2 = cafe.post("/admin/tables", {"number": "2"}, h=a).json()
    assert cafe.post(f"/admin/table-sessions/{sid}/transfer", {"new_table_id": t2["id"]}, h=a).status_code == 200
    assert cafe.get(f"/orders/{o['id']}", h=h).json()["table_label"] == "Table 2"
    busy = cafe.post(f"/admin/table-sessions/{sid}/transfer", {"new_table_id": t2["id"]}, h=a)
    assert busy.status_code == 422  # same table
    cafe.post(f"/admin/tables/{i['table']}/qr", h=a)  # regenerate QR
    assert cafe.get(f"/orders/{o['id']}", h=h).status_code == 200  # history stays accessible
    assert cafe.post(f"/admin/table-sessions/{sid}/close", {"reason": "customers left"}, h=a).status_code == 200
    assert cafe.post(f"/admin/table-sessions/{sid}/close", {}, h=a).status_code == 409
    r = cafe.post("/orders", {"order_type": "dine_in", "payment_method": "cash", "items": cafe.cart(), "table_session_token": tok}, h=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "table_session_closed"
    assert len(cafe.get(f"/admin/table-sessions/{sid}/orders", h=a).json()) == 1
