"""One-time, protected bootstrap of the cafe's single primary Admin (there is NO public admin registration).

Usage (deployment shell / Render one-off job):
    ADMIN_BOOTSTRAP_EMAIL=owner@example.com ADMIN_BOOTSTRAP_PASSWORD='...' python -m scripts.bootstrap_admin
    python -m scripts.bootstrap_admin            # prompts for email + password when env vars are not set
    python -m scripts.bootstrap_admin --reset-password   # owner lost the password (needs shell access to the deployment)
It also creates the first branch, default settings and a 0% placeholder tax class (set real tax rates afterwards)."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password, validate_password_strength
from app.db.session import dispose_engine, session_scope
from app.models.core import AdminUser, Branch
from app.models.settings import CafeSettings, DeliverySettings, TaxSetting
from app.services.audit import audit


class BootstrapError(Exception):
    pass


async def bootstrap(email: str, password: str, name: str = "Cafe Admin", branch_code: str = "main", branch_name: str = "Crescent Cafe", reset: bool = False) -> str:
    err = validate_password_strength(password)
    if err:
        raise BootstrapError(f"Weak password: {err}")
    async with session_scope() as db:
        existing = (await db.execute(select(AdminUser).where(AdminUser.is_primary.is_(True)))).scalar_one_or_none()
        if existing is not None:
            if not reset:
                raise BootstrapError("A primary admin already exists. Use --reset-password to change its password.")
            existing.password_hash, existing.failed_logins, existing.locked_until, existing.is_active = hash_password(password), 0, None, True
            from app.services.auth import revoke_all_sessions

            await revoke_all_sessions(db, "admin", existing.id)
            await audit(db, actor_role="system", actor_id=None, action="admin.password_reset_cli", entity_type="admin", entity_id=existing.id)
            return f"Password reset for {existing.email}"
        branch = (await db.execute(select(Branch).where(Branch.code == branch_code))).scalar_one_or_none()
        if branch is None:
            branch = Branch(code=branch_code, name=branch_name)
            db.add(branch)
            await db.flush()
        if (await db.execute(select(CafeSettings).where(CafeSettings.branch_id == branch.id))).scalar_one_or_none() is None:
            db.add(CafeSettings(branch_id=branch.id, name=branch_name))
        if (await db.execute(select(DeliverySettings).where(DeliverySettings.branch_id == branch.id))).scalar_one_or_none() is None:
            db.add(DeliverySettings(branch_id=branch.id))
        if (await db.execute(select(TaxSetting).where(TaxSetting.branch_id == branch.id))).scalar_one_or_none() is None:
            db.add(TaxSetting(branch_id=branch.id, name="Default tax (SET ME)", rate_bps=0, is_default=True))
        admin = AdminUser(branch_id=branch.id, email=email.lower().strip(), name=name, password_hash=hash_password(password), is_primary=True)
        db.add(admin)
        await db.flush()
        await audit(db, actor_role="system", actor_id=None, action="admin.bootstrapped", entity_type="admin", entity_id=admin.id, branch_id=branch.id)
        return f"Primary admin created: {admin.email}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-password", action="store_true")
    ap.add_argument("--branch-code", default=os.getenv("BRANCH_CODE", "main"))
    ap.add_argument("--branch-name", default=os.getenv("BRANCH_NAME", "Crescent Cafe"))
    args = ap.parse_args()
    s = get_settings()
    email, pw = s.admin_bootstrap_email, s.admin_bootstrap_password
    if not args.reset_password and not email:
        email = input("Admin email: ").strip()
    if not pw:
        if not sys.stdin.isatty():
            raise SystemExit("Set ADMIN_BOOTSTRAP_PASSWORD or run interactively")
        pw = getpass.getpass("Admin password (min 10 chars, mixed case + digit): ")
        if getpass.getpass("Repeat password: ") != pw:
            raise SystemExit("Passwords do not match")

    async def run() -> None:
        try:
            print(await bootstrap(email, pw, s.admin_bootstrap_name, args.branch_code, args.branch_name, args.reset_password))
        except BootstrapError as e:
            raise SystemExit(str(e))
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
