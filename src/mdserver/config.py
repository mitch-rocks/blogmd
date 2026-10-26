"""
Runtime configuration, read from environment variables (and a .env file if present).

| Variable                | Default                              | Purpose                                          |
| ----------------------- | ------------------------------------ | ------------------------------------------------ |
| DEBUG                   | false                                | uvicorn auto-reload for development              |
| CONTENT_PATH            | content                              | Root folder for markdown import + static files   |
| DATABASE_URL            | sqlite:///<CONTENT_PATH>/blogmd.db   | mongodb://... or sqlite://... (scheme = backend) |
| MEDIA_URL               | file://<CONTENT_PATH>/media          | file://<dir> or gridfs:// (inside MongoDB)       |
| SECRET_KEY              | generated + persisted in the DB      | Session cookie signing key                       |
| MAX_UPLOAD_MB           | 50                                   | Per-request upload limit                         |
| ALLOW_REGISTRATION      | false                                | Let anyone create a writer account at /write/    |
| SESSION_COOKIE_SECURE   | false                                | Set true when served over HTTPS                  |
| SITE_NAME               | Mitch Naake                          | Shown in the sidebar and og:site_name            |
"""
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is optional
    pass

import os
import pathlib


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


DEBUG: bool = _bool('DEBUG')

# project root = the folder containing src/ (so it works whatever directory you launch from)
PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]

# CONTENT_PATH may be absolute, or relative to the project root
_content = pathlib.Path(os.environ.get('CONTENT_PATH', 'content'))
ROOT_FOLDER: pathlib.Path = _content if _content.is_absolute() else PROJECT_ROOT / _content
STATIC_FOLDER: pathlib.Path = ROOT_FOLDER / 'static'
BLOG_FOLDER: pathlib.Path = ROOT_FOLDER / 'blog'
AUTHOR_FOLDER: pathlib.Path = ROOT_FOLDER / 'author'
ERROR_FOLDER: pathlib.Path = ROOT_FOLDER / 'error'

DATABASE_URL: str = os.environ.get('DATABASE_URL', f'sqlite:///{(ROOT_FOLDER / "blogmd.db").as_posix()}')
MEDIA_URL: str = os.environ.get('MEDIA_URL', f'file://{(ROOT_FOLDER / "media").as_posix()}')

SECRET_KEY: str | None = os.environ.get('SECRET_KEY') or None
MAX_UPLOAD_MB: int = int(os.environ.get('MAX_UPLOAD_MB', '50'))
ALLOW_REGISTRATION: bool = _bool('ALLOW_REGISTRATION')
SESSION_COOKIE_SECURE: bool = _bool('SESSION_COOKIE_SECURE')
SITE_NAME: str = os.environ.get('SITE_NAME', 'Mitch Naake')

# Reverse proxies in front of the app (Traefik, nginx, Cloudflare tunnel...). With N > 0 the
# app trusts the last N X-Forwarded-For/-Proto/-Host hops, so client IPs (login throttling)
# and https URLs are correct. Leave 0 when exposed directly.
TRUST_PROXY: int = int(os.environ.get('TRUST_PROXY', '0'))

# Header carrying the real visitor IP, e.g. CF-Connecting-IP behind Cloudflare. Only set this
# when the app can ONLY be reached through that proxy, otherwise clients could spoof it.
REAL_IP_HEADER: str | None = os.environ.get('REAL_IP_HEADER') or None

HOST: str = os.environ.get('HOST', '0.0.0.0')
PORT: int = int(os.environ.get('PORT', '8001'))

# Web server (uvicorn) capacity. Requests share one event loop, so thousands of waiting
# connections are cheap; the real limit is file descriptors (one per connection). 0 = automatic:
# raise the soft open-files limit to the hard limit and accept up to that minus a reserve, so a
# flood queues instead of failing with "Too many open files". A positive value caps connections
# lower; past the cap, new ones get an immediate 503.
WEB_LIMIT_CONCURRENCY: int = int(os.environ.get('WEB_LIMIT_CONCURRENCY', '0'))
WEB_BACKLOG: int = int(os.environ.get('WEB_BACKLOG', '4096'))   # kernel caps this at net.core.somaxconn
WEB_KEEPALIVE: int = int(os.environ.get('WEB_KEEPALIVE', '5'))          # idle keep-alive seconds

# Seconds to keep retrying the database at startup (e.g. MongoDB container still booting).
DB_WAIT_SECONDS: float = float(os.environ.get('DB_WAIT_SECONDS', '60'))
