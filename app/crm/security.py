"""Admin login: Argon2 password hashes, server-side sessions behind an
HTTP-only cookie, login lockout, and the guards for the /api routes."""

import hashlib
import secrets
import time
from collections import deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import SessionLocal, utcnow
from .models import AdminSession, AdminUser

COOKIE = "scalryx_session"
_hasher = PasswordHasher()
# Checked when the email is unknown, so timing doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    return (request.client.host if request.client else "unknown")[:64]


# ------------------------------------------------------------ rate limits --


class SlidingWindow:
    """At most `limit` hits per `seconds` for each key, in memory. One process
    is plenty here; with several workers each counts alone."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, seconds: float) -> deque[float]:
        now = time.monotonic()
        window = self.hits.setdefault(key, deque())
        while window and now - window[0] > seconds:
            window.popleft()
        if len(self.hits) > 10_000:
            self.hits = {key: window}
        return window

    def hit(self, key: str, limit: int, seconds: float = 60) -> bool:
        """Record a hit; True when the key is over its limit."""
        window = self._prune(key, seconds)
        window.append(time.monotonic())
        return len(window) > limit

    def count(self, key: str, seconds: float) -> int:
        return len(self._prune(key, seconds))

    def retry_after(self, key: str, seconds: float) -> int:
        window = self._prune(key, seconds)
        return max(1, int(seconds - (time.monotonic() - window[0]))) if window else 1

    def clear(self, key: str) -> None:
        self.hits.pop(key, None)


ingest_limiter = SlidingWindow()
login_failures = SlidingWindow()


def login_keys(request: Request, email: str) -> list[str]:
    return [f"ip:{client_ip(request)}", f"email:{email.lower()[:254]}"]


def check_lockout(keys: list[str]) -> None:
    s = get_settings()
    window = s.login_lockout_minutes * 60
    for key in keys:
        if login_failures.count(key, window) >= s.login_max_failures:
            wait = login_failures.retry_after(key, window)
            raise HTTPException(429, "Too many failed attempts. Try again later.", headers={"Retry-After": str(wait)})


def record_failure(keys: list[str]) -> None:
    for key in keys:
        login_failures.hit(key, limit=10**9, seconds=get_settings().login_lockout_minutes * 60)


# --------------------------------------------------------------- sessions --


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def start_session(db: AsyncSession, response: Response, user: AdminUser) -> None:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    await db.execute(delete(AdminSession).where(AdminSession.expires_at < utcnow()))
    db.add(AdminSession(token_hash=_digest(token), user_id=user.id, expires_at=utcnow() + timedelta(days=s.session_days)))
    response.set_cookie(
        COOKIE, token, max_age=s.session_days * 86400, httponly=True, secure=s.cookie_secure, samesite="lax", path="/"
    )


async def end_session(db: AsyncSession, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        await db.execute(delete(AdminSession).where(AdminSession.token_hash == _digest(token)))
    response.delete_cookie(COOKIE, path="/", secure=get_settings().cookie_secure, httponly=True, samesite="lax")


async def get_db():
    async with SessionLocal() as db:
        yield db


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> AdminUser:
    token = request.cookies.get(COOKIE)
    if token:
        row = await db.get(AdminSession, _digest(token))
        if row and row.expires_at > utcnow():
            user = await db.get(AdminUser, row.user_id)
            if user:
                return user
    raise HTTPException(401, "Please log in")


def require_trusted_origin(request: Request) -> None:
    """Belt and braces on top of SameSite: a browser request that changes
    data must come from the dashboard's own origin."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in get_settings().dashboard_origin_list:
            raise HTTPException(403, "Origin not allowed")


def require_ingest_key(request: Request) -> None:
    s = get_settings()
    if not s.ingest_key:
        raise HTTPException(503, "Lead ingest is not configured")
    # Counted before the key check, so guessing the key is rate limited too.
    if ingest_limiter.hit(f"ingest:{client_ip(request)}", s.ingest_rate_per_minute):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
    if not secrets.compare_digest(request.headers.get("x-ingest-key", ""), s.ingest_key):
        raise HTTPException(401, "Bad ingest key")


async def find_admin(db: AsyncSession, email: str) -> AdminUser | None:
    return await db.scalar(select(AdminUser).where(AdminUser.email == email.strip().lower()))
