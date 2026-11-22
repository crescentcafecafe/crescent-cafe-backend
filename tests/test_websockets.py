import pytest
from starlette.websockets import WebSocketDisconnect


def connect(cafe, h=None, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    if h:
        q = (q + "&" if q else "") + "token=" + h["Authorization"].split()[1]
    return cafe.client.websocket_connect("/api/v1/ws" + ("?" + q if q else ""))


def sub(ws, topic):
    ws.send_json({"type": "subscribe", "topic": topic})
    return ws.receive_json()


def next_event(ws, typ):
    for _ in range(10):
        m = ws.receive_json()
        if m.get("type") == typ:
            return m
    raise AssertionError(f"no {typ} event")


def test_ws_requires_valid_authentication(cafe):
    for kwargs in ({}, {"token": "garbage"}):
        with pytest.raises(WebSocketDisconnect):
            with connect(cafe, **kwargs):
                pass
    h = cafe.customer()
    cafe.post("/auth/logout", h=h)
    with pytest.raises(WebSocketDisconnect):  # revoked session
        with connect(cafe, h):
            pass
    with pytest.raises(WebSocketDisconnect):
        with connect(cafe, guest_token="x", order_id="00000000-0000-0000-0000-000000000000"):
            pass


def test_admin_receives_new_order_and_status_events_with_ids(cafe):
    with connect(cafe, cafe.admin) as ws:
        assert ws.receive_json()["type"] == "connected"
        o = cafe.place("pickup", h=cafe.customer()).json()["order"]
        ev = next_event(ws, "order.new")
        assert ev["topic"] == "admin" and ev["id"] and ev["ts"] and ev["data"]["order_id"] == o["id"] and ev["data"]["alarm"] == "start"
        cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
        stop = next_event(ws, "order.alarm_stop")
        assert stop["data"]["alarm"] == "stop" and stop["id"] > ev["id"]  # app silences the alarm on this event


def test_customer_order_topic_authorization(cafe):
    c1, c2 = cafe.customer("A"), cafe.customer("B", phone="9111111111")
    o1 = cafe.place("pickup", h=c1).json()["order"]
    with connect(cafe, c2) as ws:
        ws.receive_json()
        assert sub(ws, f"order:{o1['id']}") == {"type": "error", "code": "forbidden", "topic": f"order:{o1['id']}"}
        assert sub(ws, "admin")["code"] == "forbidden"
        assert sub(ws, "account:customer:" + o1["id"])["code"] == "forbidden"
        assert sub(ws, "bogus:topic")["code"] == "forbidden"
    with connect(cafe, c1) as ws:
        ws.receive_json()
        assert sub(ws, f"order:{o1['id']}")["type"] == "subscribed"
        cafe.post(f"/admin/orders/{o1['id']}/accept", h=cafe.admin)
        ev = next_event(ws, "order.status_changed")
        assert ev["data"]["status"] == "accepted" and ev["topic"] in (f"order:{o1['id']}", f"account:customer:{ev['topic'].split(':')[-1]}")


def test_guest_ws_scoped_to_own_order(cafe):
    r = cafe.place("pickup", customer_name="G", customer_phone="9800011111").json()
    other = cafe.place("pickup", customer_name="H", customer_phone="9800022222").json()
    with connect(cafe, guest_token=r["guest_token"], order_id=r["order"]["id"]) as ws:
        ws.receive_json()
        assert sub(ws, f"order:{other['order']['id']}")["code"] == "forbidden"
        assert sub(ws, f"order:{r['order']['id']}")["type"] == "subscribed"
        cafe.post(f"/admin/orders/{r['order']['id']}/accept", h=cafe.admin)
        assert next_event(ws, "order.status_changed")["data"]["status"] == "accepted"
        assert sub(ws, "admin")["code"] == "forbidden"


def test_reconnect_resubscribe_and_refetch(cafe):
    ch = cafe.customer()
    o = cafe.place("pickup", h=ch).json()["order"]
    with connect(cafe, ch) as ws:
        ws.receive_json()
        sub(ws, f"order:{o['id']}")
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)  # happens while the client is offline
    with connect(cafe, ch) as ws:
        assert ws.receive_json()["type"] == "connected"
        assert sub(ws, f"order:{o['id']}")["type"] == "subscribed"  # clients re-subscribe after reconnecting...
    assert cafe.get(f"/orders/{o['id']}", h=ch).json()["status"] == "accepted"  # ...and refetch state; WS is not storage
    missed = cafe.get("/notifications/events?after_id=0", h=ch).json()["items"]
    assert any(e["data"].get("status") == "accepted" for e in missed)


def test_chat_over_ws_member_only(cafe):
    c1, c2 = cafe.customer("A"), cafe.customer("B", phone="9111111111")
    cid = cafe.post("/chat/conversations", {}, h=c1).json()["id"]
    with connect(cafe, c2) as ws:
        ws.receive_json()
        assert sub(ws, f"conversation:{cid}")["code"] == "forbidden"
    with connect(cafe, c1) as c1ws, connect(cafe, cafe.admin) as adm:
        c1ws.receive_json(); adm.receive_json()
        assert sub(c1ws, f"conversation:{cid}")["type"] == "subscribed"
        assert sub(adm, f"conversation:{cid}")["type"] == "subscribed"
        cafe.post(f"/chat/conversations/{cid}/messages", {"body": "hello there"}, h=c1)
        assert next_event(adm, "chat.message")["data"]["message"]["body"] == "hello there"
        cafe.post(f"/chat/conversations/{cid}/messages", {"body": "hi!", "internal": True}, h=cafe.admin)
        cafe.post(f"/chat/conversations/{cid}/messages", {"body": "welcome"}, h=cafe.admin)
        bodies = []
        while "welcome" not in bodies:
            bodies.append(next_event(c1ws, "chat.message")["data"]["message"]["body"])
        assert "hi!" not in bodies  # the internal note was never broadcast to the customer


def test_location_stream_reaches_only_authorized_viewers(cafe):
    rid, rh = cafe.rider()
    ch = cafe.customer()
    o = cafe.place("delivery", h=ch).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    aid = cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin).json()["assignment_id"]
    cafe.post(f"/deliveries/{aid}/accept", h=rh)
    stranger = cafe.customer("S", phone="9111111111")
    with connect(cafe, ch) as ws, connect(cafe, stranger) as bad:
        ws.receive_json(); bad.receive_json()
        assert sub(ws, f"tracking:{o['id']}")["type"] == "subscribed"
        assert sub(bad, f"tracking:{o['id']}")["code"] == "forbidden"
        cafe.post("/tracking/location", {"assignment_id": aid, "latitude": 12.97, "longitude": 77.59}, h=rh)
        ev = next_event(ws, "tracking.location")
        assert ev["id"] is None and ev["data"]["latitude"] == 12.97  # ephemeral: not persisted as a business event
    with connect(cafe, rh) as rws:  # riders can't spy on the customer's tracking channel
        rws.receive_json()
        assert sub(rws, f"tracking:{o['id']}")["code"] == "forbidden"
        assert sub(rws, f"order:{o['id']}")["type"] == "subscribed"
