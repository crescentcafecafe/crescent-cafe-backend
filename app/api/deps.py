from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.core import Branch
from app.services.settings import resolve_branch


async def public_branch(db: AsyncSession = Depends(get_db), x_branch_code: str | None = Header(default=None)) -> Branch:
    """Branch for public/customer calls. Defaults to the only branch; clients may later send X-Branch-Code."""
    return await resolve_branch(db, x_branch_code)


def page(limit: int = 50, offset: int = 0) -> tuple[int, int]:
    return max(1, min(limit, 200)), max(0, offset)
