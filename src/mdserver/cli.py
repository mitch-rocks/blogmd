"""
Command line entry point.

    python src/main.py                       # run the server (same as `serve`)
    python src/main.py serve --port 8001 [--reload]
    python src/main.py import [--overwrite]  # (re)import content/blog/**/*.md into the DB
    python src/main.py create-user mitch --admin
    python src/main.py set-password mitch
    python src/main.py ensure-admin          # apply ADMIN_* env vars without starting the server
    python src/main.py users
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from mdserver import config
from mdserver.domain import Role, User, validate_username
from mdserver.storage import Conflict, available_backends, open_database


def _password(args) -> str:
    from mdserver.services.auth import validate_password
    if getattr(args, 'password', None):
        pw = args.password
    else:
        pw = getpass.getpass('Password: ')
        if pw != getpass.getpass('Repeat password: '):
            sys.exit('Passwords do not match.')
    if err := validate_password(pw):
        sys.exit(err)
    return pw


async def _db():
    """Open the configured database, importing the markdown content on first use."""
    from mdserver.services.importer import import_content
    db = await open_database(config.DATABASE_URL, wait_seconds=config.DB_WAIT_SECONDS)
    if await db.posts.count() == 0 and await db.users.count() == 0:
        print('Empty database - importing', config.ROOT_FOLDER, '->',
              await import_content(db, config.ROOT_FOLDER))
    return db


def run(coro_fn):
    """Run an async command; turn bootstrap/config errors into a clean exit message."""
    from mdserver.services.bootstrap import BootstrapError
    from mdserver.storage import StorageError

    async def wrapper(args):
        db = await _db()
        try:
            return await coro_fn(db, args)
        finally:
            await db.close()

    def cmd(args):
        try:
            asyncio.run(wrapper(args))
        except (BootstrapError, StorageError) as e:
            sys.exit(str(e))
    return cmd


# --------------------------------------------------------------------------- commands

FD_RESERVE = 200   # descriptors kept free for the DB connection pool, uploads, logs, imports


def _fd_budget() -> int | None:
    """
    Raise the soft open-files limit to the hard limit (allowed without privileges; many hosts
    default to soft=1024 with a far higher hard limit) and return the usable limit.
    None where the platform has no such limit (Windows).
    """
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard == resource.RLIM_INFINITY or hard > soft:
        target = 1_048_576 if hard == resource.RLIM_INFINITY else hard
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        except (ValueError, OSError):
            pass
    return soft


def cmd_serve(args):
    import uvicorn
    nofile = _fd_budget()
    # every connection is a file descriptor: never allow more than the process can hold
    budget = max(nofile - FD_RESERVE, 32) if nofile is not None else 10_000
    limit = min(config.WEB_LIMIT_CONCURRENCY, budget) if config.WEB_LIMIT_CONCURRENCY > 0 else budget
    print(f'BlogMD on http://{args.host}:{args.port}  (db: {config.DATABASE_URL.split("@")[-1]}, '
          f'media: {config.MEDIA_URL}, open-files limit: {nofile or "n/a"}, '
          f'max concurrent connections: {limit})', flush=True)
    uvicorn.run(
        'mdserver.web.asgi:app', host=args.host, port=args.port,
        reload=args.reload or config.DEBUG,
        limit_concurrency=limit,
        backlog=config.WEB_BACKLOG,
        timeout_keep_alive=config.WEB_KEEPALIVE,
        proxy_headers=False,             # handled by our ProxyHeadersMiddleware (TRUST_PROXY)
        server_header=False,
        log_level='info',
    )


@run
async def cmd_import(db, args):
    from mdserver.services.importer import import_content
    report = await import_content(db, config.ROOT_FOLDER, overwrite=args.overwrite)
    print(report)
    for line in report.skipped:
        print('  skipped:', line)


@run
async def cmd_create_user(db, args):
    from mdserver.services.auth import hash_password
    username = args.username.lower()
    if err := validate_username(username):
        sys.exit(err)
    user = User(username=username, display_name=args.display_name or username,
                role=Role.ADMIN if args.admin else Role.WRITER,
                avatar_url=args.avatar or '', password_hash=hash_password(_password(args)))
    try:
        await db.users.create(user)
    except Conflict:
        sys.exit(f'User "{username}" already exists. Use set-password instead.')
    print(f'Created {user.role.value} "{username}". Sign in at /write/login')


@run
async def cmd_set_password(db, args):
    from mdserver.services.auth import hash_password
    user = await db.users.get_by_username(args.username.lower())
    if user is None:
        names = ', '.join(u.username for u in await db.users.list())
        sys.exit(f'No user "{args.username}". Existing: {names}')
    if user.role == Role.SYSTEM:
        sys.exit('System accounts cannot sign in.')
    user.password_hash = hash_password(_password(args))
    if args.admin:
        user.role = Role.ADMIN
    await db.users.update(user)
    print(f'Password set for "{user.username}" ({user.role.value}).')


@run
async def cmd_ensure_admin(db, _args):
    from mdserver.services.bootstrap import ensure_admin
    r = await ensure_admin(db)
    if r.action == 'none':
        print('Nothing to do: an admin can already sign in.' if r.username is None
              else f'Admin "{r.username}" already set up.')


@run
async def cmd_users(db, _args):
    for u in await db.users.list():
        print(f'{u.id:>4}  {u.username:<20} {u.role.value:<8} '
              f'{"can sign in" if u.can_login else "no password":<12} {u.display_name}')


def main(argv=None):
    logging.basicConfig(level=logging.DEBUG if config.DEBUG else logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger('pymongo').setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog='blogmd', description=f'BlogMD (backends: {", ".join(available_backends())})')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('serve', help='run the web server')
    s.add_argument('--host', default=config.HOST)
    s.add_argument('--port', type=int, default=config.PORT)
    s.add_argument('--reload', action='store_true', help='restart on code changes (development)')
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser('import', help='import markdown files from CONTENT_PATH into the database')
    s.add_argument('--overwrite', action='store_true', help='replace posts that already exist')
    s.set_defaults(func=cmd_import)

    s = sub.add_parser('create-user', help='create a writer (or admin) account')
    s.add_argument('username')
    s.add_argument('--display-name')
    s.add_argument('--avatar', help='avatar image URL')
    s.add_argument('--admin', action='store_true')
    s.add_argument('--password', help='omit to be prompted (recommended)')
    s.set_defaults(func=cmd_create_user)

    s = sub.add_parser('set-password', help='set or reset a password (e.g. for imported authors)')
    s.add_argument('username')
    s.add_argument('--admin', action='store_true', help='also promote to admin')
    s.add_argument('--password', help='omit to be prompted (recommended)')
    s.set_defaults(func=cmd_set_password)

    s = sub.add_parser('ensure-admin', help='create/repair the admin account from ADMIN_* env vars')
    s.set_defaults(func=cmd_ensure_admin)

    s = sub.add_parser('users', help='list accounts')
    s.set_defaults(func=cmd_users)

    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(['serve'] + (argv or sys.argv[1:]))
    args.func(args)
