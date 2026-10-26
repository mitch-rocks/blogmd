"""Password hashing, sessions, CSRF and a small login throttle (FastAPI / Starlette sessions)."""
from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time

from fastapi import HTTPException, Request
from werkzeug.security import check_password_hash, generate_password_hash

from mdserver.domain import User

MIN_PASSWORD_LENGTH = 10
# a valid hash to compare against when the username doesn't exist, so response
# timing doesn't reveal which usernames are real
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def validate_password(password: str) -> str | None:
    if len(password or '') < MIN_PASSWORD_LENGTH:
        return f'Passwords need at least {MIN_PASSWORD_LENGTH} characters.'
    return None


def verify_password(user: User | None, password: str) -> bool:
    """CPU-heavy (scrypt): call through verify_password_async from request handlers."""
    if user is None or not user.can_login:
        check_password_hash(_DUMMY_HASH, password or '')
        return False
    return check_password_hash(user.password_hash, password or '')


async def verify_password_async(user: User | None, password: str) -> bool:
    return await asyncio.to_thread(verify_password, user, password)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


# --------------------------------------------------------------------------- throttle

class LoginThrottle:
    """In-memory: N failures per (ip, username) within a window locks that pair out."""

    def __init__(self, max_failures: int = 5, window_s: int = 300):
        self.max_failures, self.window_s = max_failures, window_s
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key) -> list[float]:
        now = time.monotonic()
        hits = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        if hits:
            self._fails[key] = hits
        else:
            self._fails.pop(key, None)
        return hits

    def blocked(self, ip: str, username: str) -> int:
        """Seconds until the pair may try again (0 = allowed)."""
        with self._lock:
            hits = self._recent((ip, username.lower()))
            if len(hits) < self.max_failures:
                return 0
            return int(self.window_s - (time.monotonic() - hits[0])) + 1

    def fail(self, ip: str, username: str) -> None:
        with self._lock:
            key = (ip, username.lower())
            self._fails[key] = self._recent(key) + [time.monotonic()]
            if len(self._fails) > 50_000:              # bound memory under credential stuffing
                for k in list(self._fails)[:10_000]:
                    self._recent(k)

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails.pop((ip, username.lower()), None)


throttle = LoginThrottle()


# --------------------------------------------------------------------------- sessions

def _password_fingerprint(user: User) -> str:
    # changing a password invalidates every existing session for that user
    return (user.password_hash or '')[-16:]


def login_user(request: Request, user: User) -> None:
    request.session.clear()                    # new session on login (fixation defence)
    request.session.update(uid=user.id, pwv=_password_fingerprint(user), csrf=secrets.token_urlsafe(32))


def logout_user(request: Request) -> None:
    request.session.clear()


async def current_user(request: Request) -> User | None:
    if not hasattr(request.state, 'current_user'):
        user = None
        uid = request.session.get('uid')
        if uid is not None:
            user = await request.app.state.blogmd.db.users.get(uid)
            if user is None or not user.can_login or \
                    not hmac.compare_digest(request.session.get('pwv', ''), _password_fingerprint(user)):
                request.session.clear()
                user = None
        request.state.current_user = user
    return request.state.current_user


def csrf_token(request: Request) -> str:
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_urlsafe(32)
    return request.session['csrf']


def check_csrf(request: Request, form_token: str | None = None) -> None:
    sent = request.headers.get('X-CSRF-Token') or form_token or ''
    expected = request.session.get('csrf', '')
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(400, 'Invalid or missing CSRF token. Reload the page and try again.')


# --------------------------------------------------------------------------- dependencies

class LoginRequired(Exception):
    """Raised by page routes; turned into a redirect to the sign-in page."""

    def __init__(self, next_url: str):
        self.next_url = next_url


async def require_user_page(request: Request) -> User:
    user = await current_user(request)
    if user is None:
        nxt = request.url.path + (f'?{request.url.query}' if request.url.query else '')
        raise LoginRequired(nxt)
    return user


async def require_user_api(request: Request) -> User:
    """JSON API: 401 when signed out, CSRF header required for anything that changes state."""
    user = await current_user(request)
    if user is None:
        raise HTTPException(401, 'Sign in required.')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        check_csrf(request)
    return user
