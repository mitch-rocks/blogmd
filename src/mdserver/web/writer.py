"""Writers area: sign-in, dashboard, editor with live preview, media library + JSON API."""
from __future__ import annotations

from datetime import datetime

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request,
                   url_for)

from mdserver.domain import (Media, Post, PostStatus, Role, User, normalise_path, slugify, utcnow,
                             validate_path, validate_username)
from mdserver.render import render_markdown
from mdserver.services import auth
from mdserver.services.auth import current_user, login_required
from mdserver.services.media import (ALLOWED_TYPES, UploadError, delete_media, save_upload,
                                     sync_post_media)
from mdserver.storage import Conflict

from .app import services

bp = Blueprint('writer', __name__, url_prefix='/write')


# --------------------------------------------------------------------------- helpers

def can_edit(user: User, post: Post) -> bool:
    return user.is_admin or post.author_id == user.id


def can_manage_media(user: User, media: Media) -> bool:
    return user.is_admin or media.owner_id == user.id


def post_json(post: Post) -> dict:
    author = services().author(post.author_id)
    return {
        'id': post.id, 'path': post.path, 'url': post.url, 'title': post.title,
        'description': post.description, 'content': post.content, 'status': post.status.value,
        'nav_order': post.nav_order, 'author': author_json(author),
        'created_at': post.created_at.isoformat(), 'updated_at': post.updated_at.isoformat(),
        'published_at': post.published_at.isoformat() if post.published_at else None,
    }


def author_json(user: User | None) -> dict | None:
    if user is None:
        return None
    return {'id': user.id, 'username': user.username, 'display_name': user.display_name,
            'avatar_url': user.avatar_url}


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


def _client_ip() -> str:
    return request.remote_addr or '?'


# --------------------------------------------------------------------------- auth pages

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect(_safe_next(request.args.get('next')))
    db = services().db
    error = None
    username = ''
    if request.method == 'POST':
        auth.check_csrf()
        username = (request.form.get('username') or '').strip().lower()
        password = request.form.get('password') or ''
        wait = auth.throttle.blocked(_client_ip(), username)
        if wait:
            error = f'Too many attempts. Try again in {wait // 60 + 1} min.'
        else:
            user = db.users.get_by_username(username)
            if auth.verify_password(user, password):
                auth.throttle.reset(_client_ip(), username)
                auth.login_user(user)
                return redirect(_safe_next(request.args.get('next')))
            auth.throttle.fail(_client_ip(), username)
            error = 'Incorrect username or password.'
    no_accounts = not any(u.can_login for u in db.users.list())
    return render_template('writer/login.html', error=error, username=username,
                           no_accounts=no_accounts,
                           allow_registration=services().allow_registration), (401 if error else 200)


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if not services().allow_registration:
        abort(404)
    db = services().db
    error, form = None, {}
    if request.method == 'POST':
        auth.check_csrf()
        form = {k: (request.form.get(k) or '').strip() for k in ('username', 'display_name')}
        form['username'] = form['username'].lower()
        password = request.form.get('password') or ''
        error = (validate_username(form['username']) or auth.validate_password(password)
                 or (None if form['display_name'] else 'Display name is required.'))
        if not error:
            try:
                user = db.users.create(User(username=form['username'],
                                            display_name=form['display_name'][:80],
                                            role=Role.WRITER,
                                            password_hash=auth.hash_password(password)))
            except Conflict:
                error = 'That username is taken.'
            else:
                auth.login_user(user)
                return redirect(url_for('writer.dashboard'))
    return render_template('writer/register.html', error=error, form=form)


@bp.route('/logout', methods=['POST'])
def logout():
    auth.check_csrf()
    auth.logout_user()
    return redirect(url_for('writer.login'))


@bp.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    user = current_user()
    db = services().db
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'profile':
            user.display_name = (request.form.get('display_name') or user.display_name).strip()[:80]
            user.avatar_url = (request.form.get('avatar_url') or '').strip()[:500]
            user.bio = (request.form.get('bio') or '').strip()[:1000]
            db.users.update(user)
            flash('Profile saved.', 'ok')
        elif action == 'password':
            if not auth.verify_password(user, request.form.get('current_password') or ''):
                flash('Current password is incorrect.', 'error')
            elif err := auth.validate_password(request.form.get('new_password') or ''):
                flash(err, 'error')
            else:
                user.password_hash = auth.hash_password(request.form['new_password'])
                db.users.update(user)
                auth.login_user(user)   # keep this session, invalidate others
                flash('Password changed. Other sessions were signed out.', 'ok')
        return redirect(url_for('writer.account'))
    return render_template('writer/account.html', user=user)


# --------------------------------------------------------------------------- pages

@bp.route('/')
@login_required
def dashboard():
    user = current_user()
    db = services().db
    status = request.args.get('status') or None
    q = (request.args.get('q') or '').strip() or None
    statuses = [PostStatus(status)] if status in {s.value for s in PostStatus} else None
    posts = db.posts.list(statuses=statuses, search=q,
                          author_id=None if user.is_admin else user.id)
    posts.sort(key=lambda p: p.updated_at, reverse=True)
    authors = {u.id: u for u in db.users.list()}
    mine = db.posts.list(author_id=user.id)
    stats = {s.value: sum(1 for p in mine if p.status == s) for s in PostStatus}
    stats['media'] = len(db.media.list(owner_id=user.id, limit=10_000))
    return render_template('writer/dashboard.html', posts=posts, authors=authors, stats=stats,
                           status=status, q=q or '')


@bp.route('/new')
@login_required
def new_post():
    folder = normalise_path(request.args.get('folder', ''))
    return render_template('writer/editor.html', post=None, post_data=None, folder=folder,
                           author=current_user(),
                           accept=','.join('.' + e for e in ALLOWED_TYPES),
                           max_upload_mb=_max_mb())


@bp.route('/edit/<int:post_id>')
@login_required
def edit_post(post_id: int):
    post = services().db.posts.get(post_id)
    if post is None:
        abort(404)
    if not can_edit(current_user(), post):
        abort(403)
    return render_template('writer/editor.html', post=post, post_data=post_json(post), folder='',
                           author=services().author(post.author_id),
                           accept=','.join('.' + e for e in ALLOWED_TYPES),
                           max_upload_mb=_max_mb())


@bp.route('/media')
@login_required
def media_library():
    return render_template('writer/media.html', accept=','.join('.' + e for e in ALLOWED_TYPES),
                           max_upload_mb=_max_mb())


def _max_mb() -> int:
    from flask import current_app
    return current_app.config['MAX_UPLOAD_BYTES'] // (1024 * 1024)


# --------------------------------------------------------------------------- API: preview

@bp.route('/api/render', methods=['POST'])
@login_required
def api_render():
    data = request.get_json(silent=True) or {}
    user = current_user()
    trusted = user.trusted_html
    if data.get('post_id'):
        post = services().db.posts.get(int(data['post_id']))
        if post and can_edit(user, post):
            author = services().author(post.author_id)
            trusted = author.trusted_html if author else trusted
    return jsonify(html=render_markdown(str(data.get('content', ''))[:2_000_000], trusted=trusted))


@bp.route('/api/check-path')
@login_required
def api_check_path():
    path = normalise_path(request.args.get('path', ''))
    err = _path_error(current_user(), path)
    if err:
        return jsonify(ok=False, path=path, error=err)
    other = services().db.posts.get_by_path(path)
    exclude = request.args.get('id', type=int)
    if other and other.id != exclude:
        return jsonify(ok=False, path=path, error=f'/blog/{path}/ is already used by "{other.title}".')
    return jsonify(ok=True, path=path)


# --------------------------------------------------------------------------- API: posts

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
    except ValueError:
        return 'Unknown status.'

    post.title = title
    post.path = path
    post.description = str(data.get('description', post.description) or '')[:500]
    post.content = str(data.get('content', post.content) or '')
    post.nav_order = int(data.get('nav_order', post.nav_order) or 0)
    if status != PostStatus.DRAFT and post.published_at is None:
        post.published_at = utcnow()
    post.status = status
    post.updated_at = utcnow()
    return None


@bp.route('/api/posts', methods=['POST'])
@login_required
def api_create_post():
    user = current_user()
    data = request.get_json(silent=True) or {}
    db = services().db
    if not str(data.get('path', '')).strip():
        data['path'] = slugify(data.get('title', ''))
    post = Post(path='', title='', author_id=user.id)
    if err := _apply(post, data, user):
        return jsonify(error=err), 400
    post.created_at = post.updated_at
    try:
        with db.transaction():
            db.posts.create(post)
            sync_post_media(db, post.id, post.content)
    except Conflict:
        return jsonify(error=f'/blog/{post.path}/ is already in use.', field='path'), 409
    return jsonify(post=post_json(post)), 201


@bp.route('/api/posts/<int:post_id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_post(post_id: int):
    user = current_user()
    db = services().db
    post = db.posts.get(post_id)
    if post is None:
        abort(404)
    if not can_edit(user, post):
        abort(403)

    if request.method == 'GET':
        return jsonify(post=post_json(post))

    if request.method == 'DELETE':
        db.posts.delete(post.id)
        return jsonify(ok=True)

    data = request.get_json(silent=True) or {}
    # optimistic concurrency: refuse to overwrite a newer save from another tab/user
    base = data.get('base_updated_at')
    if base and not data.get('force') and datetime.fromisoformat(base) != post.updated_at:
        return jsonify(error='This post was changed somewhere else since you opened it.',
                       conflict=True, post=post_json(post)), 409
    if err := _apply(post, data, user):
        return jsonify(error=err), 400
    try:
        with db.transaction():
            db.posts.update(post)
            sync_post_media(db, post.id, post.content)
    except Conflict:
        return jsonify(error=f'/blog/{post.path}/ is already in use.', field='path'), 409
    return jsonify(post=post_json(post))


# --------------------------------------------------------------------------- API: media

@bp.route('/api/media', methods=['GET', 'POST'])
@login_required
def api_media():
    user = current_user()
    svc = services()
    if request.method == 'GET':
        everyone = user.is_admin and request.args.get('scope') == 'all'
        items = svc.db.media.list(owner_id=None if everyone else user.id,
                                  kind=request.args.get('kind') or None,
                                  limit=min(request.args.get('limit', 60, type=int), 200),
                                  offset=request.args.get('offset', 0, type=int))
        return jsonify(media=[media_json(m) for m in items])

    upload = request.files.get('file')
    if upload is None:
        return jsonify(error='No file was sent.'), 400
    from flask import current_app
    try:
        media, created = save_upload(svc.db, svc.files, user, upload.stream, upload.filename,
                                     current_app.config['MAX_UPLOAD_BYTES'])
    except UploadError as e:
        return jsonify(error=str(e)), 400
    alt = (request.form.get('alt_text') or '').strip()
    if alt and created:
        media.alt_text = alt[:300]
        svc.db.media.update(media)
    return jsonify(media=media_json(media), created=created), 201 if created else 200


@bp.route('/api/media/<int:media_id>', methods=['GET', 'PATCH', 'DELETE'])
@login_required
def api_media_item(media_id: int):
    user = current_user()
    svc = services()
    media = svc.db.media.get(media_id)
    if media is None:
        abort(404)
    if not can_manage_media(user, media):
        abort(403)
    if request.method == 'GET':
        used_by = [svc.db.posts.get(pid) for pid in svc.db.media.usage(media.id)]
        return jsonify(media=media_json(media),
                       used_by=[{'id': p.id, 'title': p.title, 'url': p.url} for p in used_by if p])
    if request.method == 'PATCH':
        data = request.get_json(silent=True) or {}
        if 'alt_text' in data:
            media.alt_text = str(data['alt_text'] or '').strip()[:300]
        if data.get('name'):
            media.original_name = str(data['name']).strip()[:200]
        svc.db.media.update(media)
        return jsonify(media=media_json(media))
    used = svc.db.media.usage(media.id)
    if used and not request.args.get('force'):
        return jsonify(error=f'Used by {len(used)} post(s). Remove it from them first, '
                             f'or delete anyway.', used_by=used), 409
    delete_media(svc.db, svc.files, media)
    return jsonify(ok=True)
