"""
Command line entry point.

    python src/main.py                       # run the server (same as `serve`)
    python src/main.py serve --port 8001
    python src/main.py import [--overwrite]  # (re)import content/blog/**/*.md into the DB
    python src/main.py create-user mitch --admin
    python src/main.py set-password mitch
    python src/main.py users
"""
from __future__ import annotations

import argparse
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


def _db():
    """Open the configured database, importing the markdown content on first use."""
    from mdserver.services.importer import import_content
    db = open_database(config.DATABASE_URL)
    if db.posts.count() == 0 and db.users.count() == 0:
        print('Empty database - importing', config.ROOT_FOLDER, '->', import_content(db, config.ROOT_FOLDER))
    return db


def cmd_serve(args):
    from mdserver.web.app import create_app
    app = create_app()
    print(f'BlogMD on http://{args.host}:{args.port}  (db: {config.DATABASE_URL}, debug={config.DEBUG})')
    if config.DEBUG:
        app.run(args.host, args.port, debug=True)
    else:
        import uvicorn
        from asgiref.wsgi import WsgiToAsgi
        uvicorn.run(WsgiToAsgi(app), host=args.host, port=args.port)


def cmd_import(args):
    from mdserver.services.importer import import_content
    db = open_database(config.DATABASE_URL)
    report = import_content(db, config.ROOT_FOLDER, overwrite=args.overwrite)
    print(report)
    for line in report.skipped:
        print('  skipped:', line)


def cmd_create_user(args):
    from mdserver.services.auth import hash_password
    username = args.username.lower()
    if err := validate_username(username):
        sys.exit(err)
    db = _db()
    user = User(username=username, display_name=args.display_name or username,
                role=Role.ADMIN if args.admin else Role.WRITER,
                avatar_url=args.avatar or '', password_hash=hash_password(_password(args)))
    try:
        db.users.create(user)
    except Conflict:
        sys.exit(f'User "{username}" already exists. Use set-password instead.')
    print(f'Created {user.role.value} "{username}". Sign in at /write/login')


def cmd_set_password(args):
    from mdserver.services.auth import hash_password
    db = _db()
    user = db.users.get_by_username(args.username.lower())
    if user is None:
        sys.exit(f'No user "{args.username}". Existing: {", ".join(u.username for u in db.users.list())}')
    if user.role == Role.SYSTEM:
        sys.exit('System accounts cannot sign in.')
    user.password_hash = hash_password(_password(args))
    if args.admin:
        user.role = Role.ADMIN
    db.users.update(user)
    print(f'Password set for "{user.username}" ({user.role.value}).')


def cmd_users(_args):
    db = _db()
    for u in db.users.list():
        print(f'{u.id:>4}  {u.username:<20} {u.role.value:<8} '
              f'{"can sign in" if u.can_login else "no password":<12} {u.display_name}')


def main(argv=None):
    logging.basicConfig(level=logging.DEBUG if config.DEBUG else logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')
    p = argparse.ArgumentParser(prog='blogmd', description=f'BlogMD (backends: {", ".join(available_backends())})')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('serve', help='run the web server')
    s.add_argument('--host', default='0.0.0.0')
    s.add_argument('--port', type=int, default=8001)
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

    s = sub.add_parser('users', help='list accounts')
    s.set_defaults(func=cmd_users)

    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(['serve'] + (argv or sys.argv[1:]))
    args.func(args)
