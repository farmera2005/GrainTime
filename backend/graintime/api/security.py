"""Local accounts, server-side sessions, and role checks.

Local accounts are the break-glass administrator created in the setup wizard
(and development logins). Entra ID single sign-on is added once confirmed;
SSO users get rows in `users` with auth_source='entra' and no password.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from ..common.config import get_settings
from ..common.db import get_sessionmaker
from ..common.models import Session, User

COOKIE = "gt_session"
_hasher = PasswordHasher()


def get_db():
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hash_: str | None, password: str) -> bool:
    if not hash_:
        return False
    try:
        return _hasher.verify(hash_, password)
    except (VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: DbSession, user: User, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    hours = get_settings().session_hours
    db.add(Session(token_hash=_token_hash(token), user_id=user.id,
                   expires_at=datetime.now(timezone.utc) + timedelta(hours=hours)))
    user.last_login_at = datetime.now(timezone.utc)
    db.execute(delete(Session).where(Session.expires_at < datetime.now(timezone.utc)))
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, token, max_age=hours * 3600, httponly=True, samesite="strict",
                        secure=secure, path="/")


def end_session(db: DbSession, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(Session).where(Session.token_hash == _token_hash(token)))
    response.delete_cookie(COOKIE, path="/")


def optional_user(request: Request, db: DbSession = Depends(get_db)) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.execute(
        select(User).join(Session, Session.user_id == User.id)
        .where(Session.token_hash == _token_hash(token),
               Session.expires_at > datetime.now(timezone.utc))).scalar_one_or_none()
    if row is None or not row.is_active:
        return None
    return row


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return user


class LoginThrottle:
    """At most `limit` failed sign-ins per username per window (in memory)."""

    def __init__(self, limit: int = 10, window_s: int = 900):
        self.limit, self.window_s = limit, window_s
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(key, []) if now - t < self.window_s]
            self.failures[key] = recent
            return len(recent) >= self.limit

    def fail(self, key: str) -> None:
        with self.lock:
            self.failures.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


throttle = LoginThrottle()
