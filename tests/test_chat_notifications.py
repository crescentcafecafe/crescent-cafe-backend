import uuid

from sqlalchemy import select

from app.db.session import session_scope
from app.models.core import FcmToken
from app.models.notifications import NotificationDeliveryLog, NotificationEvent


def conv(cafe, h, **body):
    r = cafe.post("/chat/conversations", body, h=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def say(cafe, cid, h, text="hello", **kw):
    return cafe.post(f"/chat/conversations/{cid}/messages", {"body": text, **kw}, h=h)


def test_customer_admin_chat_basics_unread_and_receipts(cafe):
    ch = cafe.customer()
    o = cafe.place("pickup", h=ch).json()["order"]
    cid = conv(cafe, ch, order_id=o["id"])
    assert conv(cafe, ch, order_id=o["id"]) == cid  # reuses the open conversation
    assert say(cafe, cid, ch, "Where is my order?").status_code == 201
    inbox = cafe.get("/chat/conversations", h=cafe.admin).json()
    assert len(inbox) == 1 and inbox[0]["unread_count"] == 1 and inbox[0]["participant"]["role"] == "customer" and inbox[0]["order_id"] == o["id"]
    assert say(cafe, cid, cafe.admin, "Almost ready!").status_code == 201
    mine = cafe.get("/chat/conversations", h=ch).json()
    assert mine[0]["unread_count"] == 1 and mine[0]["last_message"]["body"] == "Almost ready!"
    assert "participant" in mine[0] and mine[0]["participant"] is None  # customers don't get admin details
    assert cafe.post(f"/chat/conversations/{cid}/read", h=ch).json()["marked"] == 1
    assert cafe.get("/chat/conversations", h=ch).json()[0]["unread_count"] == 0
    assert cafe.get("/chat/conversations", h=cafe.admin).json()[0]["unread_count"] == 1
    cafe.post(f"/chat/conversations/{cid}/read", h=cafe.admin)
    assert cafe.get("/admin/dashboard", h=cafe.admin).json()["unread_messages"] == 0

    async def receipts():
        from app.models.chat import MessageReadReceipt
        async with session_scope() as db:
            return [(r.role) for r in (await db.execute(select(MessageReadReceipt))).scalars().all()]
    assert sorted(cafe.call(receipts)) == ["admin", "customer"]


def test_chat_membership_is_enforced(cafe):
    c1, c2 = cafe.customer("A"), cafe.customer("B", phone="9111111111")
    rid, rh = cafe.rider()
    cid = conv(cafe, c1)
    say(cafe, cid, c1, "private")
    for h in (c2, rh):
        assert cafe.get(f"/chat/conversations/{cid}/messages", h=h).status_code == 404
        assert say(cafe, cid, h, "intrude").status_code == 404
        assert cafe.post(f"/chat/conversations/{cid}/read", h=h).status_code == 404
    assert cafe.get(f"/chat/conversations/{cid}/messages").status_code == 401
    assert cafe.get("/chat/conversations", h=c2).json() == []
    # cannot open chats about someone else's order
    o = cafe.place("pickup", h=c1).json()["order"]
    assert cafe.post("/chat/conversations", {"order_id": o["id"]}, h=c2).status_code == 404
    assert cafe.post("/chat/conversations", {"order_id": o["id"]}, h=rh).status_code == 404
    assert cafe.post("/chat/conversations", {}, h=cafe.admin).status_code == 403


def test_rider_admin_chat_only_for_assigned_orders(cafe):
    rid, rh = cafe.rider()
    ch = cafe.customer()
    o = cafe.place("delivery", h=ch).json()["order"]
    assert cafe.post("/chat/conversations", {"order_id": o["id"]}, h=rh).status_code == 404  # not assigned yet
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.advance(o["id"], "preparing", "ready")
    cafe.post(f"/admin/orders/{o['id']}/assign", {"rider_id": rid}, h=cafe.admin)
    cid = conv(cafe, rh, order_id=o["id"])
    assert say(cafe, cid, rh, "Customer not answering").status_code == 201
    assert cafe.get("/chat/conversations?kind=rider_admin", h=cafe.admin).json()[0]["participant"]["role"] == "rider"
    assert cafe.get(f"/chat/conversations/{cid}/messages", h=ch).status_code == 404  # the customer is not part of rider chat
    started = cafe.post("/chat/conversations/admin", {"customer_id": cafe.get("/customers/me", h=ch).json()["id"], "order_id": o["id"]}, h=cafe.admin)
    assert started.status_code == 201
    assert cafe.get("/chat/conversations", h=ch).json()[0]["id"] == started.json()["id"]


def test_chat_validation_idempotency_pagination_and_internal_notes(cafe):
    ch = cafe.customer()
    cid = conv(cafe, ch)
    assert say(cafe, cid, ch, "   ").status_code == 422
    assert say(cafe, cid, ch, "x" * 2001).status_code == 422
    a = say(cafe, cid, ch, "once", client_msg_id="m-1").json()
    b = say(cafe, cid, ch, "once", client_msg_id="m-1").json()
    assert a["id"] == b["id"]
    for i in range(5):
        say(cafe, cid, cafe.admin, f"reply {i}")
    note = say(cafe, cid, cafe.admin, "VIP, comp a coffee", internal=True)
    assert note.status_code == 201
    assert say(cafe, cid, ch, "hack", internal=True).status_code == 403
    cust = cafe.get(f"/chat/conversations/{cid}/messages?limit=100", h=ch).json()["items"]
    assert all("VIP" not in m["body"] for m in cust) and len(cust) == 6
    adm = cafe.get(f"/chat/conversations/{cid}/messages?limit=100", h=cafe.admin).json()["items"]
    assert any(m["is_internal"] for m in adm) and len(adm) == 7
    page1 = cafe.get(f"/chat/conversations/{cid}/messages?limit=3", h=cafe.admin).json()
    page2 = cafe.get(f"/chat/conversations/{cid}/messages?limit=3&before_id={page1['next_before_id']}", h=cafe.admin).json()
    assert len(page1["items"]) == 3 and len(page2["items"]) == 3 and page1["items"][-1]["id"] > page2["items"][0]["id"]
    assert cafe.get("/admin/dashboard", h=cafe.admin).json()["unread_messages"] == 1  # internal notes never count as unread
    assert cafe.post(f"/chat/conversations/{cid}/close", h=ch).status_code == 403
    cafe.post(f"/chat/conversations/{cid}/close", h=cafe.admin)
    assert say(cafe, cid, ch, "anyone?").status_code == 403


def test_chat_message_pushes_recipient_and_never_internal(cafe):
    ch = cafe.customer()
    cafe.post("/notifications/devices", {"install_id": "cust-1", "fcm_token": "C" * 40}, h=ch)
    cafe.post("/notifications/devices", {"install_id": "admin-1", "fcm_token": "A" * 40}, h=cafe.admin)
    cid = conv(cafe, ch)
    say(cafe, cid, ch, "hi admin")
    say(cafe, cid, cafe.admin, "private note", internal=True)
    say(cafe, cid, cafe.admin, "hi customer")
    cafe.run_jobs()
    sent = [(m["token"][0], m["body"]) for m in cafe.push.sent if m["data"]["type"] == "chat.message"]
    assert sorted(sent) == [("A", "hi admin"), ("C", "hi customer")]


# ------------------------------------------------------------------ FCM lifecycle
def tokens(cafe):
    async def q():
        async with session_scope() as db:
            return {t.token: t.is_valid for t in (await db.execute(select(FcmToken))).scalars().all()}
    return cafe.call(q)


def test_device_registration_refresh_multi_device_and_unlink(cafe):
    ch = cafe.customer()
    r = cafe.post("/notifications/devices", {"install_id": "phone-1", "fcm_token": "T1" + "x" * 30, "device_model": "Pixel 8", "app_version": "1.0.0"}, h=ch)
    assert r.status_code == 201
    cafe.post("/notifications/devices", {"install_id": "tablet-1", "fcm_token": "T2" + "x" * 30, "platform": "android"}, h=ch)
    assert len(cafe.get("/notifications/devices", h=ch).json()) == 2
    cafe.put("/notifications/devices/phone-1/token", {"fcm_token": "T3" + "x" * 30}, h=ch)  # onNewToken
    t = tokens(cafe)
    assert set(t) == {"T3" + "x" * 30, "T2" + "x" * 30}
    assert cafe.put("/notifications/devices/ghost/token", {"fcm_token": "T4" + "x" * 30}, h=ch).status_code == 404
    assert cafe.delete("/notifications/devices/tablet-1", h=ch).status_code == 200
    assert set(tokens(cafe)) == {"T3" + "x" * 30}
    assert cafe.post("/notifications/devices", {"install_id": "x", "fcm_token": "short"}, h=ch).status_code == 422
    assert cafe.post("/notifications/devices", {"install_id": "phone-9", "fcm_token": "T5" + "x" * 30}).status_code == 401


def test_token_moves_to_new_account_on_shared_phone_and_logout_unlinks(cafe):
    a = cafe.customer("A", email="a@example.com")
    b = cafe.customer("B", email="b@example.com", phone="9111111111")
    tok = "SHARED" + "x" * 30
    cafe.post("/notifications/devices", {"install_id": "phone-1", "fcm_token": tok}, h=a)
    cafe.post("/notifications/devices", {"install_id": "phone-1", "fcm_token": tok}, h=b)  # B logs in on the same phone
    o = cafe.place("pickup", h=a).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.run_jobs()
    assert not [m for m in cafe.push.sent if m["token"] == tok and m["data"]["type"] == "order.status_changed"]  # A's alerts never reach B's login
    o2 = cafe.place("pickup", h=b).json()["order"]
    cafe.post(f"/admin/orders/{o2['id']}/accept", h=cafe.admin)
    cafe.run_jobs()
    assert [m for m in cafe.push.sent if m["token"] == tok and m["data"]["order_id"] == o2["id"]]
    assert cafe.post("/auth/logout", {"install_id": "phone-1"}, h=b).status_code == 200
    assert tok not in tokens(cafe)


def test_invalid_tokens_are_cleaned_up_and_not_retried(cafe):
    ch = cafe.customer()
    good, dead = "GOOD" + "x" * 30, "DEAD" + "x" * 30
    cafe.post("/notifications/devices", {"install_id": "phone-1", "fcm_token": good}, h=ch)
    cafe.post("/notifications/devices", {"install_id": "phone-2", "fcm_token": dead}, h=ch)
    cafe.push.invalid_tokens.add(dead)
    o = cafe.place("pickup", h=ch).json()["order"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.run_jobs()
    assert tokens(cafe) == {good: True, dead: False}
    n_before = len(cafe.push.sent)
    cafe.advance(o["id"], "preparing")
    cafe.run_jobs()
    assert {m["token"] for m in cafe.push.sent[n_before:]} == {good}

    async def logs():
        async with session_scope() as db:
            return sorted({l.status for l in (await db.execute(select(NotificationDeliveryLog))).scalars().all()})
    assert cafe.call(logs) == ["invalid_token", "sent"]


def test_push_retry_with_backoff_is_idempotent(cafe):
    ch = cafe.customer()
    cafe.post("/notifications/devices", {"install_id": "phone-1", "fcm_token": "G" * 40}, h=ch)
    cafe.push.fail_transient = True
    o = cafe.place("pickup", h=ch).json()["order"]
    cafe.run_jobs(1)
    assert not cafe.push.sent
    jobs = cafe.get("/admin/jobs?status=queued", h=cafe.admin).json()
    retry = [j for j in jobs if j["kind"] == "send_push"]
    assert retry and retry[0]["attempts"] == 1 and retry[0]["last_error"]  # re-queued with backoff, error recorded
    cafe.push.fail_transient = False
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.models.system import Job

    async def due():
        async with session_scope() as db:
            await db.execute(update(Job).where(Job.status == "queued").values(run_at=now_utc()))
    cafe.call(due)
    cafe.run_jobs(2)
    created = [m for m in cafe.push.sent if m["data"]["type"] == "order.created"]
    assert len(created) == 1  # delivered exactly once after the retry


def test_failed_job_visible_and_retryable_by_admin(cafe):
    from app.models.system import Job
    from app.services.jobs import enqueue

    async def mk():
        async with session_scope() as db:
            j = await enqueue(db, "does_not_exist", {})
            return str(j.id)
    jid = cafe.call(mk)
    cafe.run_jobs(1)
    failed = cafe.get("/admin/jobs?status=failed", h=cafe.admin).json()
    assert failed[0]["id"] == jid and "no handler" in failed[0]["last_error"]
    assert cafe.post(f"/admin/jobs/{jid}/retry", h=cafe.admin).status_code == 200
    assert cafe.post(f"/admin/jobs/{jid}/retry", h=cafe.admin).status_code == 409
    assert cafe.get("/admin/jobs", h=cafe.customer()).status_code == 403


def test_notifications_disabled_device_is_skipped(cafe):
    ch = cafe.customer()
    cafe.post("/notifications/devices", {"install_id": "phone-1", "fcm_token": "N" * 40, "notifications_enabled": False}, h=ch)
    o = cafe.place("pickup", h=ch).json()["order"]
    cafe.run_jobs()
    assert not cafe.push.sent
    cafe.put("/notifications/devices/phone-1/token", {"fcm_token": "N" * 40, "notifications_enabled": True}, h=ch)
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.run_jobs()
    assert cafe.push.sent


def test_promotions_only_reach_opted_in_customers(cafe):
    yes, no = cafe.customer("Yes", phone="9111111111"), cafe.customer("No", phone="9222222222")
    cafe.patch("/customers/me", {"marketing_opt_in": True}, h=yes)
    cafe.post("/notifications/devices", {"install_id": "yes-phone", "fcm_token": "Y" * 40}, h=yes)
    cafe.post("/notifications/devices", {"install_id": "no-phone", "fcm_token": "N" * 40}, h=no)
    assert cafe.post("/admin/notifications/broadcast", {"title": "Happy hour", "body": "50% off"}, h=yes).status_code == 403
    assert cafe.post("/admin/notifications/broadcast", {"title": "Happy hour", "body": "50% off"}, h=cafe.admin).status_code == 200
    cafe.run_jobs()
    assert [m["token"][0] for m in cafe.push.sent if m["data"]["type"] == "promotion"] == ["Y"]
    cafe.patch("/admin/settings/cafe", {"notify_customers_promotions": False}, h=cafe.admin)
    assert cafe.post("/admin/notifications/broadcast", {"title": "x", "body": "y"}, h=cafe.admin).status_code == 409


def test_missed_events_catch_up_after_reconnect(cafe):
    ch = cafe.customer()
    o = cafe.place("pickup", h=ch).json()["order"]
    first = cafe.get("/notifications/events?after_id=0", h=ch).json()["items"]
    last = max(e["id"] for e in first)
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    new = cafe.get(f"/notifications/events?after_id={last}", h=ch).json()["items"]
    assert [e["data"]["status"] for e in new if e["type"] == "order.status_changed"] == ["accepted"]
    other = cafe.customer("O", phone="9333333333")
    assert cafe.get("/notifications/events?after_id=0", h=other).json()["items"] == []  # nobody else's events
    assert any(e["type"] == "order.new" for e in cafe.get("/notifications/events?after_id=0", h=cafe.admin).json()["items"])
