"""Flask application factory."""
from __future__ import annotations

import logging
import pathlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import flask

from mdserver import config
from mdserver.domain import User
from mdserver.services import auth
from mdserver.services.importer import import_content
from mdserver.storage import Database, FileStore, open_database, open_file_store

log = logging.getLogger(__name__)


@dataclass
class BlogMD:
    """Per-app services, reachable as current_app.extensions['blogmd']."""
    db: Database
    files: FileStore
    content_root: pathlib.Path
    allow_registration: bool = False
    site_name: str = 'BlogMD'

    def author(self, user_id: int | None) -> User | None:
        return self.db.users.get(user_id) if user_id is not None else None


def services() -> BlogMD:
    return flask.current_app.extensions['blogmd']


def _ordinal_date(value: datetime | None) -> str:
    """16th July, 2026 - matches the original site's date style."""
    if not value:
        return '-'
    d = value.day
    suffix = 'th' if 11 <= d % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th')
    return f'{d}{suffix} {value:%B}, {value.year}'


def _filesize(n: int | None) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return ''


def brand_icon_url(content_root: pathlib.Path) -> str | None:
    """
    Image used for the brand mark (sidebar, writers rail, sign-in) and the favicon:
    content/static/icon.png if present, else the existing content/static/app.ico.
    The ?v= is the file's mtime, so replacing the file busts browser caches.
    """
    static = content_root / 'static'
    for name, url in (('icon.png', '/static/icon.png'), ('app.ico', '/favicon.ico')):
        f = static / name
        if f.is_file():
            return f'{url}?v={int(f.stat().st_mtime)}'
    return None


def create_app(db: Database | None = None, files: FileStore | None = None,
               content_root: str | pathlib.Path | None = None, *, auto_import: bool = True,
               **overrides) -> flask.Flask:
    content_root = pathlib.Path(content_root or config.ROOT_FOLDER)
    db = db or open_database(config.DATABASE_URL)
    files = files or open_file_store(config.MEDIA_URL)

    if auto_import and db.posts.count() == 0:
        report = import_content(db, content_root)
        log.info('Imported existing markdown content: %s', report)

    app = flask.Flask('blogmd', static_folder='static', static_url_path='/assets',
                      template_folder='templates',
                      root_path=str(pathlib.Path(__file__).parent))
    app.url_map.strict_slashes = False

    secret = config.SECRET_KEY or db.settings.get('secret_key')
    if not secret:
        secret = secrets.token_hex(32)
        db.settings.set('secret_key', secret)

    app.config.update(
        SECRET_KEY=secret,
        MAX_CONTENT_LENGTH=config.MAX_UPLOAD_MB * 1024 * 1024 + 64 * 1024,
        MAX_UPLOAD_BYTES=config.MAX_UPLOAD_MB * 1024 * 1024,
        SESSION_COOKIE_NAME='blogmd_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        TEMPLATES_AUTO_RELOAD=config.DEBUG,
    )
    app.config.update(overrides)

    app.extensions['blogmd'] = BlogMD(
        db=db, files=files, content_root=content_root,
        allow_registration=app.config.get('ALLOW_REGISTRATION', config.ALLOW_REGISTRATION),
        site_name=app.config.get('SITE_NAME', config.SITE_NAME),
    )

    app.jinja_env.filters['ordinal_date'] = _ordinal_date
    app.jinja_env.filters['filesize'] = _filesize

    @app.context_processor
    def inject():
        return {
            'site_name': app.extensions['blogmd'].site_name,
            'brand_icon_url': brand_icon_url(app.extensions['blogmd'].content_root),
            'current_user': auth.current_user(),
            'csrf_token': auth.csrf_token,
        }

    @app.after_request
    def security_headers(resp: flask.Response):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        return resp

    from .public import bp as public_bp
    from .writer import bp as writer_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(writer_bp)

    return app
