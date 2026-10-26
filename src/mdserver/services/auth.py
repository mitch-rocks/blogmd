"""Password hashing, sessions, CSRF and a small login throttle."""
from __future__ import annotations

import functools
import hmac
import secrets
import threading
import time

from flask import abort, current_app, g, redirect, request, session, url_for
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
    if user is None or not user.can_login:
        check_password_hash(_DUMMY_HASH, password or '')
        return False
    return check_password_hash(user.password_hash, password or '')


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
        self._fails[key] = hits
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
            self._recent((ip, username.lower())).append(time.monotonic())

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails.pop((ip, username.lower()), None)


throttle = LoginThrottle()


# --------------------------------------------------------------------------- sessions

def login_user(user: User) -> None:
    session.clear()                       # new session on login (fixation defence)
    session['uid'] = user.id
    session['pwv'] = _password_fingerprint(user)
    session['csrf'] = secrets.token_urlsafe(32)
    session.permanent = True


def logout_user() -> None:
    session.clear()


def _password_fingerprint(user: User) -> str:
    # changing a password invalidates every existing session for that user
    return (user.password_hash or '')[-16:]


def current_user() -> User | None:
    if 'current_user' not in g:
        user = None
        uid = session.get('uid')
        if uid is not None:
            user = current_app.extensions['blogmd'].db.users.get(uid)
            if user is None or not user.can_login or \
                    not hmac.compare_digest(session.get('pwv', ''), _password_fingerprint(user)):
                session.clear()
                user = None
        g.current_user = user
    return g.current_user


def csrf_token() -> str:
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def check_csrf() -> None:
    sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
    expected = session.get('csrf', '')
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description='Invalid or missing CSRF token. Reload the page and try again.')


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith('/write/api/'):
                abort(401)
            return redirect(url_for('writer.login', next=request.full_path.rstrip('?')))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            check_csrf()
        return view(*args, **kwargs)
    return wrapper
