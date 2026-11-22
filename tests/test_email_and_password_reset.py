import re

import pytest

from tests.conftest import IS_PG  # noqa: F401


def otp_from_email(cafe, email):
    mails = cafe.email.to(email)
    assert mails, f"no e-mail sent to {email}"
    return re.search(r"\b(\d{4})\b", mails[-1]["text"]).group(1)


def register(cafe, name="Asha", email="asha@example.com", password="Passw0rdPass1", phone="9876543210"):
    r = cafe.post("/auth/customer/register", {"name": name, "email": email, "password": password, "phone": phone})
    assert r.status_code == 201, r.text
    return r


def test_phone_is_mandatory_at_registration(cafe):
    r = cafe.post("/auth/customer/register", {"name": "A", "email": "a@example.com", "password": "Passw0rdPass1"})
    assert r.status_code == 422
    assert register(cafe, email="b@example.com").json()["phone_required"] is False


def test_welcome_email_goes_through_the_queue(cafe):
    register(cafe, name="Mohammed Sageer", email="sageer@example.com")
    assert cafe.email.to("sageer@example.com") == []  # queued, not sent inside the request
    cafe.run_jobs()
    mail = cafe.email.to("sageer@example.com")[0]
    assert mail["subject"] == "Welcome to Crescent Cafe!" and "Mohammed Sageer" in mail["html"]


def test_user_supplied_text_is_html_escaped_in_emails(cafe):
    register(cafe, name="<script>alert(1)</script>", email="x@example.com")
    cafe.run_jobs()
    html = cafe.email.to("x@example.com")[0]["html"]
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_forgot_password_full_flow(cafe):
    register(cafe, email="asha@example.com")
    cafe.run_jobs()
    r = cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    assert r.status_code == 200 and r.json()["otp_length"] == 4 and r.json()["resend_after_seconds"] == 60
    mail = cafe.email.to("asha@example.com")[-1]
    assert mail["subject"] == "Crescent Cafe - Password Reset OTP"
    otp = otp_from_email(cafe, "asha@example.com")
    wrong = "0000" if otp != "0000" else "1111"
    bad = cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": wrong})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_otp"
    ok = cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": otp})
    assert ok.status_code == 200
    token = ok.json()["reset_token"]
    weak = cafe.post("/auth/password/reset", {"reset_token": token, "new_password": "weak"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "weak_password"
    done = cafe.post("/auth/password/reset", {"reset_token": token, "new_password": "BrandNewPass9"})
    assert done.status_code == 200, done.text
    assert cafe.post("/auth/customer/login", {"email": "asha@example.com", "password": "Passw0rdPass1"}).status_code == 401
    assert cafe.post("/auth/customer/login", {"email": "asha@example.com", "password": "BrandNewPass9"}).status_code == 200
    again = cafe.post("/auth/password/reset", {"reset_token": token, "new_password": "AnotherPass77"})
    assert again.status_code == 401 and again.json()["error"]["code"] == "invalid_reset_token"  # one OTP, one password change
    cafe.run_jobs()
    assert any(m["subject"] == "Crescent Cafe - Your password was changed" for m in cafe.email.to("asha@example.com"))


def test_unknown_email_gets_the_same_answer_and_no_email(cafe):
    known = register(cafe, email="known@example.com") and cafe.post("/auth/password/forgot", {"email": "known@example.com"})
    unknown = cafe.post("/auth/password/forgot", {"email": "nobody@example.com"})
    assert unknown.status_code == known.status_code == 200 and unknown.json() == known.json()
    assert cafe.email.to("nobody@example.com") == []


def test_resend_cooldown_and_only_newest_code_works(cafe):
    register(cafe, email="asha@example.com")
    cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    first = otp_from_email(cafe, "asha@example.com")
    cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    assert len([m for m in cafe.email.to("asha@example.com") if "OTP" in m["subject"]]) == 1  # second request inside the cooldown sends nothing
    assert cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": first}).status_code == 200


def test_five_wrong_codes_burn_the_otp(cafe):
    register(cafe, email="asha@example.com")
    cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    otp = otp_from_email(cafe, "asha@example.com")
    wrong = "0000" if otp != "0000" else "1111"
    for _ in range(5):
        assert cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": wrong}).status_code == 422
    assert cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": otp}).status_code == 422  # even the right code is dead now


def test_otp_expires(cafe):
    from datetime import timedelta
    from sqlalchemy import update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.accounts import PasswordResetOtp
    register(cafe, email="asha@example.com")
    cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    otp = otp_from_email(cafe, "asha@example.com")

    async def age():
        async with session_scope() as db:
            await db.execute(update(PasswordResetOtp).values(expires_at=now_utc() - timedelta(seconds=1)))
    cafe.call(age)
    assert cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": otp}).status_code == 422


def test_otp_never_stored_or_queued_in_plaintext(cafe):
    from sqlalchemy import select
    from app.db.session import session_scope
    from app.models.accounts import PasswordResetOtp
    from app.models.system import Job
    register(cafe, email="asha@example.com")
    cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    otp = otp_from_email(cafe, "asha@example.com")

    async def dump():
        async with session_scope() as db:
            rows = (await db.execute(select(PasswordResetOtp))).scalars().all()
            jobs = (await db.execute(select(Job))).scalars().all()
            return [str(r.__dict__) for r in rows], [str(j.payload) for j in jobs]
    rows, jobs = cafe.call(dump)
    assert rows and not any(f"'{otp}'" in r for r in rows) and not any(otp in j for j in jobs)


def test_password_reset_signs_out_other_sessions(cafe):
    tokens = register(cafe, email="asha@example.com").json()
    cafe.post("/auth/password/forgot", {"email": "asha@example.com"})
    otp = otp_from_email(cafe, "asha@example.com")
    t = cafe.post("/auth/password/verify-otp", {"email": "asha@example.com", "otp": otp}).json()["reset_token"]
    cafe.post("/auth/password/reset", {"reset_token": t, "new_password": "BrandNewPass9"})
    assert cafe.post("/auth/refresh", {"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_order_emails_follow_the_order(cafe):
    h = cafe.customer("Asha", email="asha.orders@example.com")
    cafe.run_jobs()
    cafe.email.sent.clear()
    o = cafe.place("pickup", h=h).json()["order"]
    cafe.run_jobs()
    subjects = [m["subject"] for m in cafe.email.to("asha.orders@example.com")]
    assert "Crescent Cafe - Order Confirmation" in subjects
    conf = cafe.email.to("asha.orders@example.com")[0]
    assert o["order_number"] in conf["html"] and "Cake" in conf["html"]
    cafe.post(f"/admin/orders/{o['id']}/accept", h=cafe.admin)
    cafe.run_jobs()
    assert "Your Crescent Cafe Order Has Been Accepted!" in [m["subject"] for m in cafe.email.to("asha.orders@example.com")]


def test_email_provider_outage_is_retried_not_lost(cafe):
    cafe.email.fail_transient = True
    register(cafe, email="asha@example.com")
    cafe.run_jobs(1)
    assert cafe.email.to("asha@example.com") == []
    from sqlalchemy import select, update
    from app.core.security import now_utc
    from app.db.session import session_scope
    from app.models.system import Job

    async def release():
        async with session_scope() as db:
            await db.execute(update(Job).where(Job.status == "queued").values(run_at=now_utc()))
    cafe.call(release)
    cafe.email.fail_transient = False
    cafe.run_jobs()
    assert len(cafe.email.to("asha@example.com")) == 1


def test_missing_resend_key_does_not_break_registration(cafe):
    cafe.email.configured = False
    register(cafe, email="asha@example.com")
    cafe.run_jobs()
    assert cafe.email.sent == []
