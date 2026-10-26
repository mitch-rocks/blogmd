"""
Runtime configuration, read from environment variables (and a .env file if present).

| Variable                | Default                              | Purpose                                          |
| ----------------------- | ------------------------------------ | ------------------------------------------------ |
| DEBUG                   | false                                | Flask debug server instead of uvicorn            |
| CONTENT_PATH            | content                              | Root folder for markdown import + static files   |
| DATABASE_URL            | sqlite:///<CONTENT_PATH>/blogmd.db   | Storage backend URL (scheme selects the backend) |
| MEDIA_URL               | file://<CONTENT_PATH>/media          | Where uploaded media bytes are stored            |
| SECRET_KEY              | generated + persisted in the DB      | Flask session signing key                        |
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
