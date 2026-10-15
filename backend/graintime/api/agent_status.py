"""Agent status as shown on the site pages (shared by the site and agent routes)."""

from __future__ import annotations

from datetime import datetime, timezone

from ..common.models import SiteAgent


def agent_summary(a: SiteAgent | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if a is None:
        return {"paired": False, "pairing_pending": False}
    st = a.status or {}
    return {
        "paired": a.key_id is not None,
        "pairing_pending": bool(a.pairing_code_hash and a.pairing_expires_at and a.pairing_expires_at > now),
        "pairing_expires_at": a.pairing_expires_at.isoformat() if a.pairing_expires_at else None,
        "paired_at": a.paired_at.isoformat() if a.paired_at else None,
        "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
        "online": bool(a.last_seen_at and (now - a.last_seen_at).total_seconds() < 300),
        "version": a.version, "hostname": a.hostname, "windows_user": a.windows_user,
        "instance": st.get("instance"), "database": st.get("database"), "driver": st.get("driver"),
        "error": st.get("error"), "clock_skew_s": a.clock_skew_s,
    }
