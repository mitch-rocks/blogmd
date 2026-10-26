"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from mdserver import config
from mdserver.domain import Post, User
from mdserver.render import render_markdown
from mdserver.services import auth
from mdserver.services.importer import import_content
from mdserver.services.navigation import NavNode, navigation
from mdserver.storage import Database, FileStore, open_database, open_file_store

from .middleware import (BodySizeLimitMiddleware, LazySessionMiddleware, ProxyHeadersMiddleware,
                         SecurityHeadersMiddleware)

log = logging.getLogger(__name__)
WEB_DIR = pathlib.Path(__file__).parent


def brand_icon_url(content_root: pathlib.Path) -> str | None:
    """content/static/icon.png if present, else content/static/app.ico (?v= busts caches)."""
    static = content_root / 'static'
    for name, url in (('icon.png', '/static/icon.png'), ('app.ico', '/favicon.ico')):
        f = static / name
        if f.is_file():
            return f'{url}?v={int(f.stat().st_mtime)}'
    return None


@dataclass
class BlogMD:
    """Per-app services: request.app.state.blogmd"""
    db: Database
    files: FileStore
    content_root: pathlib.Path
    allow_registration: bool = False
    site_name: str = 'BlogMD'
    max_upload_bytes: int = 50 * 1024 * 1024
    secret_key: str = ''
    nav_ttl: float = 5.0
    _html_cache: OrderedDict = field(default_factory=OrderedDict)
    _nav: tuple = (0.0, -1, None)
    _content_version: int = 0
    _icon: tuple = (0.0, None)

    async def author(self, user_id: int | None) -> User | None:
        return await self.db.users.get(user_id) if user_id is not None else None

    # ---- caches (per process; the nav TTL bounds staleness across replicas)
    def content_changed(self) -> None:
        self._content_version += 1

    async def nav(self) -> NavNode:
        ts, version, tree = self._nav
        if tree is None or version != self._content_version or time.monotonic() - ts > self.nav_ttl:
            tree = await navigation(self.db)
            self._nav = (time.monotonic(), self._content_version, tree)
        return tree

    async def post_html(self, post: Post) -> str:
        author = await self.author(post.author_id)
        trusted = author.trusted_html if author else True      # imported/system content is trusted
        key = (post.id, post.updated_at, trusted, len(post.content), hash(post.content))
        html = self._html_cache.get(key)
        if html is None:
            html = await asyncio.to_thread(render_markdown, post.content, trusted)   # CPU-bound
            self._html_cache[key] = html
            if len(self._html_cache) > 512:
                self._html_cache.popitem(last=False)
        else:
            self._html_cache.move_to_end(key)
        return html

    def brand_icon_url(self) -> str | None:
        ts, url = self._icon
        if time.monotonic() - ts > 10:
            url = brand_icon_url(self.content_root)
            self._icon = (time.monotonic(), url)
        return url


def create_app(*, database_url: str | None = None, media_url: str | None = None,
               content_root: str | pathlib.Path | None = None, auto_import: bool = True,
               bootstrap_admin: bool = True, db_wait_seconds: float | None = None,
               **settings) -> FastAPI:
    content_root = pathlib.Path(content_root or config.ROOT_FOLDER)
    database_url = database_url or config.DATABASE_URL
    media_url = media_url or config.MEDIA_URL
    max_upload = int(settings.get('MAX_UPLOAD_MB', config.MAX_UPLOAD_MB)) * 1024 * 1024
    wait = config.DB_WAIT_SECONDS if db_wait_seconds is None else db_wait_seconds

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = await open_database(database_url, wait_seconds=wait)
        files = open_file_store(media_url, db)
        if auto_import and await db.posts.count() == 0:
            report = await import_content(db, content_root)
            log.info('Imported existing markdown content: %s', report)
        secret = settings.get('SECRET_KEY') or config.SECRET_KEY or await db.settings.get('secret_key')
        if not secret:
            secret = secrets.token_hex(32)
            await db.settings.set('secret_key', secret)
        if bootstrap_admin:
            from mdserver.services.bootstrap import ensure_admin
            await ensure_admin(db)
        app.state.blogmd = BlogMD(
            db=db, files=files, content_root=content_root,
            allow_registration=bool(settings.get('ALLOW_REGISTRATION', config.ALLOW_REGISTRATION)),
            site_name=settings.get('SITE_NAME', config.SITE_NAME),
            max_upload_bytes=max_upload, secret_key=secret,
            nav_ttl=float(settings.get('NAV_TTL', 5.0)),
        )
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)

    # ---- middleware (last added = outermost)
    app.add_middleware(LazySessionMiddleware, get_settings=lambda: dict(
        secret_key=app.state.blogmd.secret_key, session_cookie='blogmd_session',
        max_age=14 * 24 * 3600, same_site='lax',
        https_only=bool(settings.get('SESSION_COOKIE_SECURE', config.SESSION_COOKIE_SECURE))))
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_upload + 64 * 1024)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(ProxyHeadersMiddleware,
                       trusted_hops=int(settings.get('TRUST_PROXY', config.TRUST_PROXY)),
                       real_ip_header=settings.get('REAL_IP_HEADER', config.REAL_IP_HEADER))

    # ---- static files
    app.mount('/assets', StaticFiles(directory=WEB_DIR / 'static'), name='assets')
    app.mount('/static', StaticFiles(directory=content_root / 'static', check_dir=False), name='content-static')

    # ---- errors
    from .public import not_found_page
    from .templating import render, url_for

    @app.exception_handler(auth.LoginRequired)
    async def login_required(request: Request, exc: auth.LoginRequired):
        return RedirectResponse(url_for('writer.login', next=exc.next_url), status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        api = request.url.path.startswith(('/write/api/', '/media/', '/healthz'))
        if exc.status_code == 404 and not api:
            return await not_found_page(request)
        if api or 'application/json' in request.headers.get('accept', ''):
            return JSONResponse({'error': exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return await render(request, 'error.html', {
            'code': exc.status_code, 'message': exc.detail,
            'nav': await request.app.state.blogmd.nav(), 'current_path': '', 'crumbs': [], 'node': None,
        }, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({'error': 'Invalid request.'}, status_code=422)

    @app.get('/healthz', include_in_schema=False)
    async def healthz(request: Request):
        try:
            await asyncio.wait_for(request.app.state.blogmd.db.ping(), 3)
        except Exception as e:
            return JSONResponse({'status': 'error', 'detail': type(e).__name__}, status_code=503)
        return {'status': 'ok'}

    from .public import router as public_router
    from .writer import router as writer_router
    app.include_router(writer_router)
    app.include_router(public_router)
    return app
