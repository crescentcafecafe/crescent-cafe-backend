import uuid

import pytest


def test_admin_bootstrap_login_and_single_primary(ctx):
    h = ctx.bootstrap_admin()
    me = ctx.get("/admin/me", h=h).json()
    assert me["email"] == ctx.admin_email
    from scripts.bootstrap_admin import BootstrapError, bootstrap
    with pytest.raises(BootstrapError):  # a second admin can never be bootstrapped
        ctx.call(bootstrap, "second@example.com", "AnotherPassw0rd!")


def test_bootstrap_rejects_weak_password(ctx):
    from scripts.bootstrap_admin import BootstrapError, bootstrap
    with pytest.raises(BootstrapError):
        ctx.call(bootstrap, "owner@example.com", "short")


def test_db_enforces_single_primary_admin(ctx):
    ctx.bootstrap_admin()
    from sqlalchemy.exc import IntegrityError
    from app.db.session import session_scope
    from app.models.core import AdminUser, Branch
    from sqlalchemy import select

    async def add_second():
        async with session_scope() as db:
            b = (await db.execute(select(Branch))).scalars().first()
            db.add(AdminUser(branch_id=b.id, email="x@example.com", name="x", password_hash="x", is_primary=True))
    with pytest.raises(IntegrityError):
        ctx.call(add_second)


def test_no_public_admin_registration(ctx):
    for path in ("/auth/admin/register", "/admin/register", "/auth/register"):
        assert ctx.post(path, {"email": "a@example.com", "password": "Passw0rdPass1"}).status_code in (404, 405)


def test_admin_wrong_password_and_lockout(ctx):
    ctx.bootstrap_admin()
    for _ in range(5):
        r = ctx.post("/auth/admin/login", {"email": ctx.admin_email, "password": "wrong-password"})
        assert r.status_code == 401
    r = ctx.post("/auth/admin/login", {"email": ctx.admin_email, "password": ctx.admin_password})
    assert r.status_code == 401 and r.json()["error"]["code"] == "account_locked"


def test_refresh_rotation_and_reuse_detection(ctx):
    ctx.bootstrap_admin()
    t1 = ctx.post("/auth/admin/login", {"email": ctx.admin_email, "password": ctx.admin_password}).json()
    t2 = ctx.post("/auth/refresh", {"refresh_token": t1["refresh_token"]}).json()
    assert t2["refresh_token"] != t1["refresh_token"]
    # replaying the OLD refresh token is treated as theft: the whole session dies
    assert ctx.post("/auth/refresh", {"refresh_token": t1["refresh_token"]}).status_code == 401
    assert ctx.post("/auth/refresh", {"refresh_token": t2["refresh_token"]}).status_code == 401
    assert ctx.get("/admin/me", h={"Authorization": f"Bearer {t2['access_token']}"}).status_code == 401


def test_logout_revokes_session_immediately(ctx):
    h = ctx.bootstrap_admin()
    assert ctx.post("/auth/logout", h=h).status_code == 200
    assert ctx.get("/admin/me", h=h).status_code == 401


def test_change_password_revokes_other_sessions(ctx):
    h1 = ctx.bootstrap_admin()
    t2 = ctx.post("/auth/admin/login", {"email": ctx.admin_email, "password": ctx.admin_password}).json()
    h2 = {"Authorization": f"Bearer {t2['access_token']}"}
    assert ctx.post("/auth/change-password", {"current_password": ctx.admin_password, "new_password": "BrandNewPassw0rd"}, h=h1).status_code == 200
    assert ctx.get("/admin/me", h=h1).status_code == 200  # current session survives
    assert ctx.get("/admin/me", h=h2).status_code == 401  # other sessions revoked
    assert ctx.post("/auth/admin/login", {"email": ctx.admin_email, "password": "BrandNewPassw0rd"}).status_code == 200


def test_session_listing_and_revocation(ctx):
    h = ctx.bootstrap_admin()
    t2 = ctx.post("/auth/admin/login", {"email": ctx.admin_email, "password": ctx.admin_password}).json()
    sessions = ctx.get("/auth/sessions", h=h).json()
    assert len(sessions) == 2
    other = [s for s in sessions if not s["current"]][0]
    assert ctx.delete(f"/auth/sessions/{other['id']}", h=h).status_code == 200
    assert ctx.get("/admin/me", h={"Authorization": f"Bearer {t2['access_token']}"}).status_code == 401


def test_customer_register_login_profile(ctx):
    h = ctx.customer("Asha", email="asha@example.com", phone="98765 43210")
    me = ctx.get("/customers/me", h=h).json()
    assert me["phone"] == "+919876543210" and me["phone_required"] is False
    assert ctx.post("/auth/customer/register", {"name": "x", "email": "asha@example.com", "password": "Passw0rdPass1"}).status_code == 409
    assert ctx.post("/auth/customer/login", {"email": "asha@example.com", "password": "nope"}).status_code == 401
    assert ctx.post("/auth/customer/login", {"email": "asha@example.com", "password": "Passw0rdPass1"}).status_code == 200
    assert ctx.patch("/customers/me", {"name": "Asha K"}, h=h).json()["name"] == "Asha K"
    assert ctx.post("/auth/customer/register", {"name": "w", "email": "w@example.com", "password": "weak"}).status_code == 422


def test_google_login_requires_phone_next(ctx):
    r = ctx.post("/auth/customer/google", {"id_token": "good-meera"})
    assert r.status_code == 200 and r.json()["phone_required"] is True
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert ctx.get("/customers/me", h=h).json()["email"] == "meera@gmail.com"
    assert ctx.put("/customers/me/phone", {"phone": "9123456780"}, h=h).json()["phone"] == "+919123456780"
    again = ctx.post("/auth/customer/google", {"id_token": "good-meera"}).json()
    assert again["phone_required"] is False  # same account found by Google subject
    assert ctx.post("/auth/customer/google", {"id_token": "bad-token-xyz"}).status_code == 401


def test_invalid_phone_rejected(ctx):
    h = ctx.customer()
    assert ctx.put("/customers/me/phone", {"phone": "12"}, h=h).status_code == 422


def test_rider_login_and_role_separation(cafe):
    rid, rh = cafe.rider()
    assert cafe.get("/riders/me", h=rh).json()["id"] == rid
    ch = cafe.customer()
    # every role is blocked from the others' endpoints
    assert cafe.get("/admin/orders", h=rh).status_code == 403
    assert cafe.get("/admin/orders", h=ch).status_code == 403
    assert cafe.get("/riders/me", h=ch).status_code == 403
    assert cafe.get("/customers/me", h=rh).status_code == 403
    assert cafe.get("/deliveries/mine", h=ch).status_code == 403
    assert cafe.get("/admin/orders").status_code == 401
    assert cafe.get("/admin/orders", h={"Authorization": "Bearer garbage"}).status_code == 401


def test_deactivated_rider_cannot_log_in_and_sessions_die(cafe):
    rid, rh = cafe.rider()
    assert cafe.patch(f"/admin/riders/{rid}", {"is_active": False}, h=cafe.admin).status_code == 200
    assert cafe.get("/riders/me", h=rh).status_code == 401
    assert cafe.post("/auth/rider/login", {"phone": "9000000001", "password": "RiderPass123"}).status_code == 401
    assert cafe.patch(f"/admin/riders/{rid}", {"is_active": True}, h=cafe.admin).status_code == 200
    assert cafe.post("/auth/rider/login", {"phone": "9000000001", "password": "RiderPass123"}).status_code == 200


def test_admin_resets_rider_password(cafe):
    rid, rh = cafe.rider()
    assert cafe.post(f"/admin/riders/{rid}/reset-password", {"new_password": "NewRiderPass99"}, h=cafe.admin).status_code == 200
    assert cafe.get("/riders/me", h=rh).status_code == 401
    r = cafe.post("/auth/rider/login", {"phone": "9000000001", "password": "NewRiderPass99"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True


def test_customer_account_deletion_anonymises(ctx):
    h = ctx.customer("Gone", email="gone@example.com")
    assert ctx.delete("/customers/me", h=h).status_code == 200
    assert ctx.get("/customers/me", h=h).status_code == 401
    assert ctx.post("/auth/customer/login", {"email": "gone@example.com", "password": "Passw0rdPass1"}).status_code == 401


def test_rate_limit_on_login(ctx):
    from app.core.config import get_settings
    s = get_settings()
    s.rate_limit_enabled = True
    try:
        codes = [ctx.post("/auth/customer/login", {"email": "a@example.com", "password": "x"}).status_code for _ in range(12)]
    finally:
        s.rate_limit_enabled = False
    assert 429 in codes
