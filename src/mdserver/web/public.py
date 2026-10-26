"""Public site: blog pages, sidebar navigation, favicon and media streaming."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from mdserver.domain import Post, normalise_path
from mdserver.services import auth
from mdserver.services.media import ACTIVE_CONTENT, INLINE_KINDS, INLINE_TYPES
from mdserver.services.navigation import breadcrumbs, find
from mdserver.storage import NotFound

from .templating import render

router = APIRouter()


async def can_view(request: Request, post: Post) -> bool:
    if post.is_public:
        return True
    user = await auth.current_user(request)
    return bool(user and (user.is_admin or user.id == post.author_id))


async def page_context(request: Request, path: str) -> dict:
    tree = await request.app.state.blogmd.nav()
    return {'nav': tree, 'current_path': path, 'crumbs': breadcrumbs(tree, path), 'node': find(tree, path)}


async def render_post(request: Request, post: Post, path: str, status_code: int = 200):
    svc = request.app.state.blogmd
    return await render(request, 'post.html', {
        'post': post, 'author': await svc.author(post.author_id),
        'body_html': await svc.post_html(post), **await page_context(request, path),
    }, status_code=status_code)


async def not_found_page(request: Request):
    svc = request.app.state.blogmd
    post = await svc.db.posts.get_by_path('_system/404')
    if post:
        return await render_post(request, post, '_system/404', status_code=404)
    return await render(request, 'error.html', {'code': 404, 'message': 'Page not found.',
                                                **await page_context(request, '')}, status_code=404)


# --------------------------------------------------------------------------- pages

@router.get('/', include_in_schema=False)
async def root():
    return RedirectResponse('/blog/', status_code=302)


@router.get('/blog', include_in_schema=False)
async def blog_no_slash():
    return RedirectResponse('/blog/', status_code=301)


@router.get('/blog/{path:path}')
async def blog(request: Request, path: str = ''):
    path = normalise_path(path)
    post = await request.app.state.blogmd.db.posts.get_by_path(path)
    if post and await can_view(request, post):
        return await render_post(request, post, path)
    ctx = await page_context(request, path)
    if ctx['node'] is not None and ctx['node'].children:      # folder without its own page
        return await render(request, 'folder.html', ctx)
    return await not_found_page(request)


@router.get('/favicon.ico', include_in_schema=False)
async def favicon(request: Request):
    static = request.app.state.blogmd.content_root / 'static'
    for name in ('app.ico', 'icon.png'):            # /favicon.ico falls back to icon.png
        if (static / name).is_file():
            return FileResponse(static / name, headers={'Cache-Control': 'public, max-age=86400'})
    raise HTTPException(404)


# --------------------------------------------------------------------------- media

_RANGE = re.compile(r'bytes=(\d*)-(\d*)$')


def _parse_range(header: str | None, size: int):
    """Single byte range -> (start, end) inclusive; None = whole file; 'invalid' = 416."""
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if not m or (m[1] == '' and m[2] == ''):
        return None                                   # malformed / multi-range: send it all
    if m[1] == '':                                    # suffix: last N bytes
        n = int(m[2])
        return (max(size - n, 0), size - 1) if n else 'invalid'
    start = int(m[1])
    end = min(int(m[2]), size - 1) if m[2] else size - 1
    return (start, end) if start <= end and start < size else 'invalid'


@router.get('/media/{key}')
@router.get('/media/{key}/{name:path}')
async def media(request: Request, key: str, name: str = ''):
    svc = request.app.state.blogmd
    media_id, _, _token = key.partition('-')
    if not media_id.isdigit():
        raise HTTPException(404)
    item = await svc.db.media.get(int(media_id))
    if item is None or item.public_key != key:
        raise HTTPException(404)

    inline = item.kind in INLINE_KINDS or item.mime_type in INLINE_TYPES
    disposition = ('inline' if inline else 'attachment') + f'; filename="{_ascii(item.original_name)}"'
    headers = {
        'Cache-Control': 'public, max-age=31536000, immutable',
        'ETag': f'"{item.sha256}"',
        'Accept-Ranges': 'bytes',
        'Content-Disposition': disposition,
    }
    if item.mime_type in ACTIVE_CONTENT:
        # an SVG opened directly must not be able to run script on our origin
        headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if request.headers.get('if-none-match') == headers['ETag']:
        return Response(status_code=304, headers=headers)

    local = svc.files.local_path(item.storage_key)
    if local is not None:
        if not local.is_file():
            raise HTTPException(404)
        return FileResponse(local, media_type=item.mime_type, headers=headers)   # handles Range itself

    size = item.size_bytes
    rng = _parse_range(request.headers.get('range'), size)
    if rng == 'invalid':
        return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
    start, end = rng or (0, size - 1)
    try:
        body = svc.files.iter_range(item.storage_key, start, end)
        first = await anext(body, b'')               # surface NotFound before sending headers
    except NotFound:
        raise HTTPException(404)

    async def stream():
        if first:
            yield first
        async for chunk in body:
            yield chunk

    headers['Content-Length'] = str(end - start + 1)
    if rng:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    return StreamingResponse(stream(), status_code=206 if rng else 200,
                             media_type=item.mime_type, headers=headers)


def _ascii(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._ -]+', '_', name)[:150] or 'file'
