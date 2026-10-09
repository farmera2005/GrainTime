"""Audit trail for every change made through the admin panel and setup wizard."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session as DbSession

from .models import AuditLog

# Never written to the audit log, whatever the caller passes.
SECRET_FIELDS = {"password", "password_encrypted", "password_hash", "token", "token_hash",
                 "bind_password", "bind_password_encrypted"}


def scrub(values: dict[str, Any] | None) -> dict[str, Any] | None:
    if values is None:
        return None
    out = {}
    for k, v in values.items():
        if k in SECRET_FIELDS:
            out[k] = "(changed)" if v else None
        elif isinstance(v, (bytes, bytearray)):
            continue
        else:
            out[k] = v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)
    return out


def record(db: DbSession, *, actor_id: int | None, actor_name: str, action: str,
           entity_type: str, entity_id: Any = None, old: dict | None = None,
           new: dict | None = None) -> None:
    db.add(AuditLog(actor_user_id=actor_id, actor_name=actor_name, action=action,
                    entity_type=entity_type,
                    entity_id=None if entity_id is None else str(entity_id),
                    old_value=scrub(old), new_value=scrub(new)))
