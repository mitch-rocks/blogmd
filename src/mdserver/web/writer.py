"""Writers area: sign-in, dashboard, editor with live preview, media library + JSON API."""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from mdserver.domain import (Media, Post, PostStatus, Role, User, normalise_path, slugify, utcnow,
                             validate_path, validate_username)
from mdserver.render import render_markdown
from mdserver.services import auth
from mdserver.services.auth import require_user_api, require_user_page
from mdserver.services.media import (ALLOWED_TYPES, UploadError, delete_media, save_upload,
                                     sync_post_media)
from mdserver.storage import Conflict

from .templating import flash, render, url_for

router = APIRouter(prefix='/write')
ACCEPT = ','.join('.' + e for e in ALLOWED_TYPES)


# --------------------------------------------------------------------------- helpers

def svc(request: Request):
    return request.app.state.blogmd


def can_edit(user: User, post: Post) -> bool:
    return user.is_admin or post.author_id == user.id


def can_manage_media(user: User, media: Media) -> bool:
    return user.is_admin or media.owner_id == user.id


def author_json(user: User | None) -> dict | None:
    if user is None:
        return None
    return {'id': user.id, 'username': user.username, 'display_name': user.display_name,
            'avatar_url': user.avatar_url}


async def post_json(request: Request, post: Post) -> dict:
    return {
        'id': post.id, 'path': post.path, 'url': post.url, 'title': post.title,
        'description': post.description, 'content': post.content, 'status': post.status.value,
        'nav_order': post.nav_order, 'author': author_json(await svc(request).author(post.author_id)),
        'created_at': post.created_at.isoformat(), 'updated_at': post.updated_at.isoformat(),
        'published_at': post.published_at.isoformat() if post.published_at else None,
    }


def media_json(m: Media) -> dict:
    return {
        'id': m.id, 'url': m.url, 'name': m.original_name, 'mime_type': m.mime_type,
        'kind': m.kind, 'size_bytes': m.size_bytes, 'sha256': m.sha256, 'width': m.width,
        'height': m.height, 'alt_text': m.alt_text, 'created_at': m.created_at.isoformat(),
        'owner_id': m.owner_id, 'markdown': m.markdown(),
    }


def _safe_next(target: str | None) -> str:
    if target and target.startswith('/') and not target.startswith('//') and '\\' not in target:
        return target
    return url_for('writer.dashboard')


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else '') or '?'   # set by ProxyHeadersMiddleware


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------- auth pages

@router.api_route('/login', methods=['GET', 'POST'])
async def login(request: Request):
    nxt = request.query_params.get('next')
    if await auth.current_user(request):
        return _redirect(_safe_next(nxt))
    db = svc(request).db
    error, username = None, ''
    if request.method == 'POST':
        form = await request.form()
        auth.check_csrf(request, form.get('csrf_token'))
        username = str(form.get('username') or '').strip().lower()
        password = str(form.get('password') or '')
        ip = _client_ip(request)
        wait = auth.throttle.blocked(ip, username)
        if wait:
            error = f'Too many attempts. Try again in {wait // 60 + 1} min.'
        else:
            user = await db.users.get_by_username(username)
            if await auth.verify_password_async(user, password):
                auth.throttle.reset(ip, username)
                auth.login_user(request, user)
                return _redirect(_safe_next(nxt))
            auth.throttle.fail(ip, username)
            error = 'Incorrect username or password.'
    no_accounts = not any(u.can_login for u in await db.users.list())
    return await render(request, 'writer/login.html', {
        'error': error, 'username': username, 'no_accounts': no_accounts,
        'allow_registration': svc(request).allow_registration,
    }, status_code=401 if error else 200)


@router.api_route('/register', methods=['GET', 'POST'])
async def register(request: Request):
    if not svc(request).allow_registration:
        raise HTTPException(404)
    error, form_data = None, {}
    if request.method == 'POST':
        form = await request.form()
        auth.check_csrf(request, form.get('csrf_token'))
        form_data = {k: str(form.get(k) or '').strip() for k in ('username', 'display_name')}
        form_data['username'] = form_data['username'].lower()
        password = str(form.get('password') or '')
        error = (validate_username(form_data['username']) or auth.validate_password(password)
                 or (None if form_data['display_name'] else 'Display name is required.'))
        if not error:
            try:
                user = await svc(request).db.users.create(User(
                    username=form_data['username'], display_name=form_data['display_name'][:80],
                    role=Role.WRITER, password_hash=await auth.hash_password_async(password)))
            except Conflict:
                error = 'That username is taken.'
            else:
                auth.login_user(request, user)
                return _redirect(url_for('writer.dashboard'))
    return await render(request, 'writer/register.html', {'error': error, 'form': form_data})


@router.post('/logout')
async def logout(request: Request):
    form = await request.form()
    auth.check_csrf(request, form.get('csrf_token'))
    auth.logout_user(request)
    return _redirect(url_for('writer.login'))


@router.api_route('/account', methods=['GET', 'POST'])
async def account(request: Request, user: User = Depends(require_user_page)):
    db = svc(request).db
    if request.method == 'POST':
        form = await request.form()
        auth.check_csrf(request, form.get('csrf_token'))
        action = form.get('action')
        if action == 'profile':
            user.display_name = (str(form.get('display_name') or '') or user.display_name).strip()[:80]
            user.avatar_url = str(form.get('avatar_url') or '').strip()[:500]
            user.bio = str(form.get('bio') or '').strip()[:1000]
            await db.users.update(user)
            flash(request, 'Profile saved.', 'ok')
        elif action == 'password':
            new = str(form.get('new_password') or '')
            if not await auth.verify_password_async(user, str(form.get('current_password') or '')):
                flash(request, 'Current password is incorrect.', 'error')
            elif err := auth.validate_password(new):
                flash(request, err, 'error')
            else:
                user.password_hash = await auth.hash_password_async(new)
                await db.users.update(user)
                auth.login_user(request, user)      # keep this session, invalidate others
                flash(request, 'Password changed. Other sessions were signed out.', 'ok')
        return _redirect(url_for('writer.account'))
    return await render(request, 'writer/account.html', {'user': user}, endpoint='writer.account')


# --------------------------------------------------------------------------- pages

@router.get('/')
async def dashboard(request: Request, user: User = Depends(require_user_page)):
    db = svc(request).db
    status = request.query_params.get('status') or None
    q = (request.query_params.get('q') or '').strip() or None
    statuses = [PostStatus(status)] if status in {s.value for s in PostStatus} else None
    posts, users, mine, media = await asyncio.gather(
        db.posts.list(statuses=statuses, search=q, author_id=None if user.is_admin else user.id),
        db.users.list(),
        db.posts.list(author_id=user.id),
        db.media.list(owner_id=user.id, limit=10_000),
    )
    posts.sort(key=lambda p: p.updated_at, reverse=True)
    stats = {s.value: sum(1 for p in mine if p.status == s) for s in PostStatus}
    stats['media'] = len(media)
    return await render(request, 'writer/dashboard.html', {
        'posts': posts, 'authors': {u.id: u for u in users}, 'stats': stats,
        'status': status, 'q': q or '',
    }, endpoint='writer.dashboard')


@router.get('/new')
async def new_post(request: Request, user: User = Depends(require_user_page)):
    folder = normalise_path(request.query_params.get('folder', ''))
    return await render(request, 'writer/editor.html', {
        'post': None, 'post_data': None, 'folder': folder, 'author': user, 'accept': ACCEPT,
    }, endpoint='writer.new_post')


@router.get('/edit/{post_id}')
async def edit_post(request: Request, post_id: int, user: User = Depends(require_user_page)):
    post = await svc(request).db.posts.get(post_id)
    if post is None:
        raise HTTPException(404)
    if not can_edit(user, post):
        raise HTTPException(403, 'You can only edit your own posts.')
    return await render(request, 'writer/editor.html', {
        'post': post, 'post_data': await post_json(request, post), 'folder': '',
        'author': await svc(request).author(post.author_id), 'accept': ACCEPT,
    }, endpoint='writer.edit_post')


@router.get('/media')
async def media_library(request: Request, user: User = Depends(require_user_page)):
    return await render(request, 'writer/media.html', {'accept': ACCEPT}, endpoint='writer.media_library')


# --------------------------------------------------------------------------- API: preview

@router.post('/api/render')
async def api_render(request: Request, user: User = Depends(require_user_api)):
    data = await _json(request)
    trusted = user.trusted_html
    if data.get('post_id'):
        post = await svc(request).db.posts.get(int(data['post_id']))
        if post and can_edit(user, post):
            author = await svc(request).author(post.author_id)
            trusted = author.trusted_html if author else trusted
    html = await asyncio.to_thread(render_markdown, str(data.get('content', ''))[:2_000_000], trusted)
    return {'html': html}


@router.get('/api/check-path')
async def api_check_path(request: Request, user: User = Depends(require_user_api)):
    path = normalise_path(request.query_params.get('path', ''))
    if err := _path_error(user, path):
        return {'ok': False, 'path': path, 'error': err}
    other = await svc(request).db.posts.get_by_path(path)
    exclude = request.query_params.get('id')
    if other and str(other.id) != (exclude or ''):
        return {'ok': False, 'path': path, 'error': f'/blog/{path}/ is already used by "{other.title}".'}
    return {'ok': True, 'path': path}


# --------------------------------------------------------------------------- API: posts

async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _path_error(user: User, path: str) -> str | None:
    if err := validate_path(path):
        return err
    if not user.is_admin and (path == '' or path.startswith('_')):
        return 'Only admins can publish to the blog root or "_" paths.'
    return None


def _apply(post: Post, data: dict, user: User) -> str | None:
    """Copy validated fields from a JSON payload onto a post. Returns an error message."""
    title = str(data.get('title', post.title) or '').strip()
    if not title:
        return 'A title is required.'
    if len(title) > 200:
        return 'Titles are limited to 200 characters.'
    path = normalise_path(str(data['path'])) if 'path' in data else post.path
    if path != post.path or post.id is None:
        if err := _path_error(user, path):
            return err
    try:
        status = PostStatus(data.get('status', post.status.value))
        nav_order = int(data.get('nav_order', post.nav_order) or 0)
    except (ValueError, TypeError):
        return 'Invalid status or nav order.'
    post.title = title
    post.path = path
    post.description = str(data.get('description', post.description) or '')[:500]
    post.content = str(data.get('content', post.content) or '')
    post.nav_order = nav_order
    if status != PostStatus.DRAFT and post.published_at is None:
        post.published_at = utcnow()
    post.status = status
    post.updated_at = utcnow()
    return None


def _err(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({'error': message, **extra}, status_code=status)


@router.post('/api/posts', status_code=201)
async def api_create_post(request: Request, user: User = Depends(require_user_api)):
    data = await _json(request)
    db = svc(request).db
    if not str(data.get('path', '')).strip():
        data['path'] = slugify(data.get('title', ''))
    post = Post(path='', title='', author_id=user.id)
    if err := _apply(post, data, user):
        return _err(err, 400)
    post.created_at = post.updated_at
    try:
        async with db.transaction():
            await db.posts.create(post)
            await sync_post_media(db, post.id, post.content)
    except Conflict:
        return _err(f'/blog/{post.path}/ is already in use.', 409, field='path')
    svc(request).content_changed()
    return {'post': await post_json(request, post)}


async def _owned_post(request: Request, post_id: int, user: User) -> Post:
    post = await svc(request).db.posts.get(post_id)
    if post is None:
        raise HTTPException(404, 'Not found')
    if not can_edit(user, post):
        raise HTTPException(403, 'You can only edit your own posts.')
    return post


@router.get('/api/posts/{post_id}')
async def api_get_post(request: Request, post_id: int, user: User = Depends(require_user_api)):
    return {'post': await post_json(request, await _owned_post(request, post_id, user))}


@router.delete('/api/posts/{post_id}')
async def api_delete_post(request: Request, post_id: int, user: User = Depends(require_user_api)):
    post = await _owned_post(request, post_id, user)
    await svc(request).db.posts.delete(post.id)
    svc(request).content_changed()
    return {'ok': True}


@router.put('/api/posts/{post_id}')
async def api_update_post(request: Request, post_id: int, user: User = Depends(require_user_api)):
    post = await _owned_post(request, post_id, user)
    db = svc(request).db
    data = await _json(request)
    # optimistic concurrency: refuse to overwrite a newer save from another tab/user
    base = data.get('base_updated_at')
    if base and not data.get('force'):
        try:
            stale = datetime.fromisoformat(base) != post.updated_at
        except ValueError:
            stale = True
        if stale:
            return _err('This post was changed somewhere else since you opened it.', 409,
                        conflict=True, post=await post_json(request, post))
    if err := _apply(post, data, user):
        return _err(err, 400)
    try:
        async with db.transaction():
            await db.posts.update(post)
            await sync_post_media(db, post.id, post.content)
    except Conflict:
        return _err(f'/blog/{post.path}/ is already in use.', 409, field='path')
    svc(request).content_changed()
    return {'post': await post_json(request, post)}


# --------------------------------------------------------------------------- API: media

@router.get('/api/media')
async def api_list_media(request: Request, user: User = Depends(require_user_api)):
    qp = request.query_params
    everyone = user.is_admin and qp.get('scope') == 'all'

    def num(name, default, hi=None):
        try:
            v = max(int(qp.get(name, default)), 0)
        except ValueError:
            v = default
        return min(v, hi) if hi else v

    items = await svc(request).db.media.list(owner_id=None if everyone else user.id,
                                             kind=qp.get('kind') or None,
                                             limit=num('limit', 60, 200), offset=num('offset', 0))
    return {'media': [media_json(m) for m in items]}


@router.post('/api/media')
async def api_upload(request: Request, user: User = Depends(require_user_api)):
    s = svc(request)
    form = await request.form(max_files=1, max_fields=10)
    upload = form.get('file')
    if upload is None or not hasattr(upload, 'read'):
        return _err('No file was sent.', 400)
    try:
        media, created = await save_upload(s.db, s.files, user, upload.file, upload.filename,
                                           s.max_upload_bytes)
    except UploadError as e:
        return _err(str(e), 400)
    finally:
        await upload.close()
    alt = str(form.get('alt_text') or '').strip()
    if alt and created:
        media.alt_text = alt[:300]
        await s.db.media.update(media)
    return JSONResponse({'media': media_json(media), 'created': created}, status_code=201 if created else 200)


async def _owned_media(request: Request, media_id: int, user: User) -> Media:
    media = await svc(request).db.media.get(media_id)
    if media is None:
        raise HTTPException(404, 'Not found')
    if not can_manage_media(user, media):
        raise HTTPException(403, 'Not your file.')
    return media


@router.get('/api/media/{media_id}')
async def api_media_detail(request: Request, media_id: int, user: User = Depends(require_user_api)):
    media = await _owned_media(request, media_id, user)
    db = svc(request).db
    posts = [await db.posts.get(pid) for pid in await db.media.usage(media.id)]
    return {'media': media_json(media),
            'used_by': [{'id': p.id, 'title': p.title, 'url': p.url} for p in posts if p]}


@router.patch('/api/media/{media_id}')
async def api_media_update(request: Request, media_id: int, user: User = Depends(require_user_api)):
    media = await _owned_media(request, media_id, user)
    data = await _json(request)
    if 'alt_text' in data:
        media.alt_text = str(data['alt_text'] or '').strip()[:300]
    if data.get('name'):
        media.original_name = str(data['name']).strip()[:200]
    await svc(request).db.media.update(media)
    return {'media': media_json(media)}


@router.delete('/api/media/{media_id}')
async def api_media_delete(request: Request, media_id: int, user: User = Depends(require_user_api)):
    media = await _owned_media(request, media_id, user)
    s = svc(request)
    used = await s.db.media.usage(media.id)
    if used and not request.query_params.get('force'):
        return _err(f'Used by {len(used)} post(s). Remove it from them first, or delete anyway.',
                    409, used_by=used)
    await delete_media(s.db, s.files, media)
    return {'ok': True}
