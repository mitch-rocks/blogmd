"""Jinja2 rendering with the helpers the templates use (url_for, csrf_token, flash messages)."""
from __future__ import annotations

import pathlib
from datetime import datetime
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from mdserver.services import auth

TEMPLATES = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / 'templates'))

# endpoint name -> path template (Flask-style names kept so templates read the same)
ROUTES = {
    'static': '/assets/{filename}',
    'writer.dashboard': '/write/',
    'writer.login': '/write/login',
    'writer.logout': '/write/logout',
    'writer.register': '/write/register',
    'writer.account': '/write/account',
    'writer.new_post': '/write/new',
    'writer.edit_post': '/write/edit/{post_id}',
    'writer.media_library': '/write/media',
}


def url_for(endpoint: str, **params) -> str:
    """url_for('writer.edit_post', post_id=3) -> /write/edit/3 ; extra kwargs become the query string."""
    template = ROUTES[endpoint]
    used = {k: v for k, v in params.items() if '{' + k + '}' in template}
    path = template.format(**used)
    query = {k: v for k, v in params.items() if k not in used and v is not None}
    return path + ('?' + urlencode(query) if query else '')


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


TEMPLATES.env.globals['url_for'] = url_for
TEMPLATES.env.filters['ordinal_date'] = _ordinal_date
TEMPLATES.env.filters['filesize'] = _filesize


# --------------------------------------------------------------------------- flash messages

def flash(request: Request, message: str, category: str = 'message') -> None:
    request.session.setdefault('_flashes', []).append([category, message])
    request.session['_flashes'] = request.session['_flashes']      # mark modified


def _flashes(request: Request):
    def get_flashed_messages(with_categories: bool = False):
        items = request.session.pop('_flashes', [])
        return [tuple(i) for i in items] if with_categories else [i[1] for i in items]
    return get_flashed_messages


# --------------------------------------------------------------------------- render

async def render(request: Request, name: str, context: dict | None = None, status_code: int = 200,
                 endpoint: str | None = None):
    svc = request.app.state.blogmd
    ctx = {
        'site_name': svc.site_name,
        'brand_icon_url': svc.brand_icon_url(),
        'current_user': await auth.current_user(request),
        'csrf_token': lambda: auth.csrf_token(request),
        'get_flashed_messages': _flashes(request),
        'endpoint': endpoint,
        'max_upload_mb': svc.max_upload_bytes // (1024 * 1024),
    }
    ctx.update(context or {})
    return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)
