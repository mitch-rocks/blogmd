"""Public site: blog pages, sidebar navigation, static files and media."""
from __future__ import annotations

import flask
from flask import Blueprint, abort, redirect, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

from mdserver.domain import Post, PostStatus, normalise_path
from mdserver.render import render_markdown
from mdserver.services import auth
from mdserver.services.media import ACTIVE_CONTENT, INLINE_KINDS, INLINE_TYPES
from mdserver.services.navigation import breadcrumbs, find, navigation
from mdserver.storage import NotFound

from .app import services

bp = Blueprint('public', __name__)


def can_view(post: Post) -> bool:
    if post.is_public:
        return True
    user = auth.current_user()
    return bool(user and (user.is_admin or user.id == post.author_id))


def render_post_html(post: Post) -> str:
    author = services().author(post.author_id)
    trusted = author.trusted_html if author else True   # imported/system content is trusted
    return render_markdown(post.content, trusted=trusted)


def page_context(path: str) -> dict:
    tree = navigation(services().db)
    return {'nav': tree, 'current_path': path, 'crumbs': breadcrumbs(tree, path),
            'node': find(tree, path)}


def render_404(message: str | None = None):
    db = services().db
    post = db.posts.get_by_path('_system/404')
    ctx = page_context('_system/404')
    if post:
        return render_template('post.html', post=post, author=services().author(post.author_id),
                               body_html=render_post_html(post), **ctx), 404
    return render_template('error.html', code=404, message=message or 'Page not found.', **ctx), 404


@bp.app_errorhandler(404)
def not_found(_e):
    if request.path.startswith(('/write/api/', '/media/', '/assets/')):
        return flask.jsonify(error='Not found'), 404
    return render_404()


@bp.app_errorhandler(HTTPException)
def http_error(e: HTTPException):
    if e.code == 404:
        return not_found(e)
    if request.path.startswith('/write/api/') or request.is_json:
        return flask.jsonify(error=e.description or e.name), e.code
    return render_template('error.html', code=e.code, message=e.description or e.name,
                           **page_context('')), e.code


# --------------------------------------------------------------------------- pages

@bp.route('/')
def root():
    return redirect('/blog/', code=302)


@bp.route('/blog/')
@bp.route('/blog/<path:path>/')
def blog(path: str = ''):
    path = normalise_path(path)
    db = services().db
    post = db.posts.get_by_path(path)
    ctx = page_context(path)

    if post and can_view(post):
        return render_template('post.html', post=post, author=services().author(post.author_id),
                               body_html=render_post_html(post), **ctx)

    # no page here, but there are published pages below it: show a folder listing
    if ctx['node'] is not None and ctx['node'].children:
        return render_template('folder.html', **ctx)

    return render_404()


# --------------------------------------------------------------------------- files

@bp.route('/favicon.ico')
def favicon():
    static = services().content_root / 'static'
    for name in ('app.ico', 'icon.png'):     # /favicon.ico falls back to icon.png
        if (static / name).is_file():
            return send_from_directory(static.resolve(), name, max_age=86400)
    abort(404)


@bp.route('/static/<path:filename>')
def content_static(filename: str):
    """Files under content/static/ (kept so existing /static/image/... links work)."""
    return send_from_directory((services().content_root / 'static').resolve(), filename,
                               max_age=3600)


@bp.route('/media/<key>')
@bp.route('/media/<key>/<path:_name>')
def media(key: str, _name: str = ''):
    media_id, _, token = key.partition('-')
    if not media_id.isdigit():
        abort(404)
    item = services().db.media.get(int(media_id))
    if item is None or item.public_key != key:
        abort(404)

    files = services().files
    inline = item.kind in INLINE_KINDS or item.mime_type in INLINE_TYPES
    local = files.local_path(item.storage_key)
    try:
        if local is not None:
            if not local.is_file():
                abort(404)
            resp = flask.send_file(local, mimetype=item.mime_type, conditional=True,
                                   as_attachment=not inline, download_name=item.original_name,
                                   max_age=31536000, etag=item.sha256)
        else:
            resp = flask.send_file(files.open(item.storage_key), mimetype=item.mime_type,
                                   as_attachment=not inline, download_name=item.original_name,
                                   max_age=31536000, etag=item.sha256)
    except NotFound:
        abort(404)

    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    if item.mime_type in ACTIVE_CONTENT:
        # an SVG opened directly must not be able to run script on our origin
        resp.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return resp
