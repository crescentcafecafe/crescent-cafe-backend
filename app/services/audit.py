from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system import AuditLog


async def audit(db: AsyncSession, *, actor_role: str, actor_id: uuid.UUID | None, action: str, entity_type: str,
                entity_id: Any = None, before: dict | None = None, after: dict | None = None,
                branch_id: uuid.UUID | None = None, ip: str | None = None) -> None:
    db.add(AuditLog(actor_role=actor_role, actor_id=actor_id, action=action, entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id is not None else None, before=before, after=after,
                    branch_id=branch_id, ip=ip))


def model_snapshot(obj: Any, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        v = getattr(obj, f, None)
        out[f] = str(v) if isinstance(v, (uuid.UUID,)) or hasattr(v, "isoformat") else v
    return out
