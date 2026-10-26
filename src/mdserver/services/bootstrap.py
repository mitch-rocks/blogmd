"""
Make sure an admin can sign in when the server starts (for Docker / unattended deploys).

Environment:
    ADMIN_USERNAME        account to manage (default "admin")
    ADMIN_PASSWORD        its password, or
    ADMIN_PASSWORD_FILE   path to a file holding it (Docker / Swarm secrets)
    ADMIN_DISPLAY_NAME    display name when the account is created (default: the username)
    ADMIN_RESET_PASSWORD  "true" to overwrite an existing password with the configured one

Rules, applied on every start (idempotent):
  * Account missing  -> created as admin, with the configured password, or a generated one that is
                        printed to the log once. If ADMIN_USERNAME wasn't set and some other admin can
                        already sign in, nothing is created.
  * Account exists   -> promoted to admin if needed. A password is only set if it has none (or
                        ADMIN_RESET_PASSWORD=true), so a password changed in the UI survives restarts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import secrets
from dataclasses import dataclass

from mdserver.domain import Role, User, validate_username
from mdserver.services.auth import hash_password, validate_password
from mdserver.storage import Database

log = logging.getLogger('blogmd.bootstrap')


class BootstrapError(RuntimeError):
    pass


@dataclass
class BootstrapResult:
    username: str | None = None
    action: str = 'none'              # none | created | password-set | promoted
    generated_password: str | None = None


def _bool(v: str | None) -> bool:
    return (v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _configured_password(env) -> str | None:
    path = env.get('ADMIN_PASSWORD_FILE')
    if path:
        try:
            pw = pathlib.Path(path).read_text(encoding='utf-8').strip('\r\n')
        except OSError as e:
            raise BootstrapError(f'ADMIN_PASSWORD_FILE could not be read: {e}') from e
        if not pw:
            raise BootstrapError(f'ADMIN_PASSWORD_FILE ({path}) is empty.')
        return pw
    return env.get('ADMIN_PASSWORD') or None


async def ensure_admin(db: Database, env=None) -> BootstrapResult:
    env = os.environ if env is None else env
    explicit = bool(env.get('ADMIN_USERNAME'))
    username = (env.get('ADMIN_USERNAME') or 'admin').strip().lower()
    if err := validate_username(username):
        raise BootstrapError(f'ADMIN_USERNAME: {err}')
    password = _configured_password(env)
    if password is not None and (err := validate_password(password)):
        raise BootstrapError(f'ADMIN_PASSWORD: {err}')
    reset = _bool(env.get('ADMIN_RESET_PASSWORD'))

    result = BootstrapResult(username=username)
    some_admin_can_login = any(u.is_admin and u.can_login for u in await db.users.list())
    user = await db.users.get_by_username(username)

    def new_password() -> str:
        if password is not None:
            return password
        result.generated_password = secrets.token_urlsafe(15)   # ~20 chars
        return result.generated_password

    async with db.transaction():
        if user is None:
            if not explicit and password is None and some_admin_can_login:
                result.username = None
                return result                  # an admin already exists; don't invent another
            await db.users.create(User(username=username,
                                 display_name=(env.get('ADMIN_DISPLAY_NAME') or username)[:80],
                                 role=Role.ADMIN,
                                 password_hash=await asyncio.to_thread(hash_password, new_password())))
            result.action = 'created'
        else:
            if user.role == Role.SYSTEM:
                raise BootstrapError(f'"{username}" is a system account and cannot be the admin login.')
            changed = False
            if user.role != Role.ADMIN and (explicit or not some_admin_can_login):
                user.role = Role.ADMIN
                result.action = 'promoted'
                changed = True
            needs_pw = not user.password_hash or (reset and password is not None)
            if needs_pw and (password is not None or not some_admin_can_login or explicit):
                user.password_hash = await asyncio.to_thread(hash_password, new_password())
                result.action = 'password-set'
                changed = True
            if changed:
                await db.users.update(user)

    if result.generated_password:
        banner = '=' * 64
        log.warning('\n%s\n  BlogMD admin account ready\n    username: %s\n    password: %s\n'
                    '  Sign in at /write/login and change it under Account.\n'
                    '  (Set ADMIN_PASSWORD or ADMIN_PASSWORD_FILE to choose it yourself.)\n%s',
                    banner, username, result.generated_password, banner)
    elif result.action != 'none':
        log.info('admin account "%s": %s', username, result.action)
    return result
