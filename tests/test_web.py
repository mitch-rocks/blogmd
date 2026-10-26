"""HTTP-level tests: public site, auth, CSRF, permissions, editor API, uploads, import."""
import io
import re

import pytest
from PIL import Image

from mdserver.domain import Role, User
from mdserver.services.auth import hash_password, throttle
from mdserver.storage import open_database
from mdserver.web.app import create_app


@pytest.fixture
def app(tmp_path, files, content_root):
    db = open_database(f'sqlite:///{(tmp_path / "web.db").as_posix()}')
    app = create_app(db=db, files=files, content_root=content_root, TESTING=True,
                     ALLOW_REGISTRATION=True)
    mitch = db.users.get_by_username('mitch')
    mitch.password_hash = hash_password('admin-password')
    db.users.update(mitch)
    db.users.create(User(username='wren', display_name='Wren', role=Role.WRITER,
                         password_hash=hash_password('writer-password')))
    db.users.create(User(username='other', display_name='Other', role=Role.WRITER,
                         password_hash=hash_password('other-password')))
    throttle._fails.clear()
    yield app
    db.close()


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'csrf: "([^"]+)"', html)
    return m.group(1)


class Client:
    def __init__(self, app):
        self.c = app.test_client()
        self.csrf = None

    def login(self, username, password):
        page = self.c.get('/write/login').get_data(as_text=True)
        r = self.c.post('/write/login', data={'username': username, 'password': password,
                                              'csrf_token': csrf_from(page)})
        if r.status_code == 302:
            self.csrf = csrf_from(self.c.get('/write/').get_data(as_text=True))
        return r

    def api(self, method, url, **kw):
        return self.c.open(url, method=method, headers={'X-CSRF-Token': self.csrf or ''}, **kw)


@pytest.fixture
def admin(app):
    c = Client(app); assert c.login('mitch', 'admin-password').status_code == 302; return c


@pytest.fixture
def writer(app):
    c = Client(app); assert c.login('wren', 'writer-password').status_code == 302; return c


def png_bytes(w=40, h=30):
    buf = io.BytesIO()
    Image.new('RGB', (w, h), '#8555f6').save(buf, 'PNG')
    return buf.getvalue()


# --------------------------------------------------------------------------- public site

def test_imported_content_and_nav(app):
    c = app.test_client()
    assert c.get('/').status_code == 302
    home = c.get('/blog/').get_data(as_text=True)
    assert 'Welcome to my Personal Blog' in home
    assert 'class="sidebar"' in home and '/blog/weekly/16-07-2026/' in home
    post = c.get('/blog/weekly/16-07-2026/').get_data(as_text=True)
    assert 'Devlog Update #1' in post and '16th July, 2026' in post and 'aria-current="page"' in post
    # folder without index.md gets an automatic listing
    folder = c.get('/blog/theta/')
    assert folder.status_code == 200 and 'Theta Protocol' in folder.get_data(as_text=True)
    # 404 uses the imported error page
    missing = c.get('/blog/does-not-exist/')
    assert missing.status_code == 404 and 'coder elves' in missing.get_data(as_text=True)
    assert c.get('/static/image/system_pfp.jpg').status_code == 200
    assert c.get('/static/../blogmd.db').status_code == 404


def test_hidden_system_pages_not_in_nav(app):
    html = app.test_client().get('/blog/').get_data(as_text=True)
    assert '_system' not in html


# --------------------------------------------------------------------------- auth

def test_login_required_and_bad_password(app):
    c = Client(app)
    assert c.c.get('/write/').status_code == 302
    assert c.c.post('/write/api/posts', json={}).status_code == 401
    r = c.login('wren', 'nope-nope-nope')
    assert r.status_code == 401 and 'Incorrect username or password' in r.get_data(as_text=True)
    assert c.login('system', 'anything-at-all').status_code == 401     # system user can't log in


def test_login_throttle(app):
    c = Client(app)
    for _ in range(5):
        c.login('wren', 'bad-password-x')
    r = c.login('wren', 'writer-password')     # correct, but locked out
    assert r.status_code == 401 and 'Too many attempts' in r.get_data(as_text=True)


def test_login_csrf_and_open_redirect(app):
    c = app.test_client()
    assert c.post('/write/login', data={'username': 'wren', 'password': 'writer-password'}).status_code == 400
    page = c.get('/write/login').get_data(as_text=True)
    r = c.post('/write/login?next=//evil.example/', data={
        'username': 'wren', 'password': 'writer-password', 'csrf_token': csrf_from(page)})
    assert r.status_code == 302 and r.headers['Location'].endswith('/write/')


def test_api_requires_csrf(writer):
    r = writer.c.post('/write/api/posts', json={'title': 'x'})          # no header
    assert r.status_code == 400
    assert writer.api('POST', '/write/api/posts', json={'title': 'x'}).status_code == 201


def test_password_change_revokes_other_sessions(app, writer):
    other = Client(app); other.login('wren', 'writer-password')
    assert other.c.get('/write/').status_code == 200
    r = writer.c.post('/write/account', data={'action': 'password', 'csrf_token': writer.csrf,
                                              'current_password': 'writer-password',
                                              'new_password': 'a-brand-new-password'})
    assert r.status_code == 302
    assert writer.c.get('/write/').status_code == 200      # this session survives
    assert other.c.get('/write/').status_code == 302       # the other one is signed out


def test_registration(app):
    c = app.test_client()
    page = c.get('/write/register').get_data(as_text=True)
    r = c.post('/write/register', data={'username': 'newbie', 'display_name': 'New', 'password': 'long-enough-pw',
                                        'csrf_token': csrf_from(page)})
    assert r.status_code == 302
    db = app.extensions['blogmd'].db
    assert db.users.get_by_username('newbie').role == Role.WRITER


# --------------------------------------------------------------------------- posts API

def test_create_edit_publish_flow(app, writer):
    r = writer.api('POST', '/write/api/posts', json={'title': 'My First Post', 'content': '# Hi', 'status': 'draft'})
    assert r.status_code == 201
    post = r.get_json()['post']
    assert post['path'] == 'my-first-post' and post['published_at'] is None

    anon = app.test_client()
    assert anon.get('/blog/my-first-post/').status_code == 404          # drafts are private
    assert writer.c.get('/blog/my-first-post/').status_code == 200      # ...but the author can preview
    assert 'My First Post' not in anon.get('/blog/').get_data(as_text=True)

    r = writer.api('PUT', f'/write/api/posts/{post["id"]}', json={
        'title': 'My First Post', 'path': 'notes/first', 'status': 'published', 'content': 'Hello **world**',
        'base_updated_at': post['updated_at']})
    assert r.status_code == 200
    updated = r.get_json()['post']
    assert updated['published_at']
    page = anon.get('/blog/notes/first/').get_data(as_text=True)
    assert '<strong>world</strong>' in page
    assert '/blog/notes/first/' in anon.get('/blog/').get_data(as_text=True)    # now in the sidebar


def test_conflicting_save_is_detected(writer):
    p = writer.api('POST', '/write/api/posts', json={'title': 'T'}).get_json()['post']
    stale = p['updated_at']
    import time; time.sleep(1.1)
    assert writer.api('PUT', f'/write/api/posts/{p["id"]}', json={'title': 'T2', 'base_updated_at': stale}).status_code == 200
    r = writer.api('PUT', f'/write/api/posts/{p["id"]}', json={'title': 'T3', 'base_updated_at': stale})
    assert r.status_code == 409 and r.get_json()['conflict']
    assert writer.api('PUT', f'/write/api/posts/{p["id"]}', json={'title': 'T3', 'base_updated_at': stale,
                                                                 'force': True}).status_code == 200


def test_path_rules(writer, admin):
    assert writer.api('POST', '/write/api/posts', json={'title': 'a', 'path': 'weekly/16-07-2026'}).status_code == 409
    assert writer.api('POST', '/write/api/posts', json={'title': 'a', 'path': '../etc'}).status_code == 400
    assert writer.api('POST', '/write/api/posts', json={'title': 'a', 'path': '_system/x'}).status_code == 400
    assert admin.api('POST', '/write/api/posts', json={'title': 'a', 'path': '_system/x'}).status_code == 201
    r = writer.c.get('/write/api/check-path?path=weekly')
    assert r.get_json()['ok'] is False


def test_writers_cannot_touch_others_posts(app, writer):
    db = app.extensions['blogmd'].db
    admin_post = db.posts.get_by_path('weekly/16-07-2026')
    assert writer.c.get(f'/write/edit/{admin_post.id}').status_code == 403
    assert writer.api('PUT', f'/write/api/posts/{admin_post.id}', json={'title': 'pwned'}).status_code == 403
    assert writer.api('DELETE', f'/write/api/posts/{admin_post.id}').status_code == 403
    dash = writer.c.get('/write/').get_data(as_text=True)
    assert 'Devlog Update' not in dash


def test_admin_sees_and_edits_all(app, admin):
    db = app.extensions['blogmd'].db
    p = db.posts.get_by_path('weekly/16-07-2026')
    assert 'Devlog Update' in admin.c.get('/write/').get_data(as_text=True)
    assert admin.c.get(f'/write/edit/{p.id}').status_code == 200


def test_untrusted_html_is_sanitised(app, writer, admin):
    evil = '<img src=x onerror="alert(1)"><script>alert(2)</script><a href="javascript:alert(3)">x</a> <mark>ok</mark>'
    html = writer.api('POST', '/write/api/render', json={'content': evil}).get_json()['html']
    assert 'onerror' not in html and '<script' not in html and 'javascript:' not in html
    assert '<mark>ok</mark>' in html
    # admins (and imported content) keep raw HTML, as before
    assert '<script>' in admin.api('POST', '/write/api/render', json={'content': evil}).get_json()['html']
    p = writer.api('POST', '/write/api/posts', json={'title': 'evil', 'content': evil, 'status': 'published'}).get_json()['post']
    page = app.test_client().get(p['url']).get_data(as_text=True)
    assert 'onerror' not in page and 'alert(2)' not in page


def test_render_preview_matches_public_renderer(writer):
    html = writer.api('POST', '/write/api/render', json={'content': '```python\nx = 1\n```\n\n- [x] done'}).get_json()['html']
    assert 'language-python' in html and 'type="checkbox"' in html


# --------------------------------------------------------------------------- media

def upload(client, data, name, **form):
    return client.api('POST', '/write/api/media', data={'file': (io.BytesIO(data), name), **form},
                      content_type='multipart/form-data')


def test_upload_stores_metadata_and_serves(app, writer):
    r = upload(writer, png_bytes(40, 30), 'diagram.png', alt_text='A diagram')
    assert r.status_code == 201
    m = r.get_json()['media']
    assert (m['mime_type'], m['width'], m['height'], m['alt_text']) == ('image/png', 40, 30, 'A diagram')
    assert len(m['sha256']) == 64 and m['size_bytes'] > 0
    assert m['markdown'] == f'![A diagram]({m["url"]})'

    db = app.extensions['blogmd'].db
    row = db.media.get(m['id'])
    assert row.owner_id == db.users.get_by_username('wren').id and row.storage_key.endswith('.png')

    served = app.test_client().get(m['url'])
    assert served.status_code == 200 and served.mimetype == 'image/png'
    assert served.headers['X-Content-Type-Options'] == 'nosniff'
    # guessing by numeric id alone doesn't work
    assert app.test_client().get(f'/media/{m["id"]}/diagram.png').status_code == 404
    assert app.test_client().get(f'/media/{m["id"]}-0000000000000000/x').status_code == 404

    # re-uploading identical bytes reuses the existing record
    again = upload(writer, png_bytes(40, 30), 'copy.png')
    assert again.status_code == 200 and again.get_json()['media']['id'] == m['id']


def test_upload_validation(writer):
    assert upload(writer, b'#!/bin/sh\nrm -rf /', 'evil.sh').status_code == 400
    assert upload(writer, b'not really a png', 'fake.png').status_code == 400
    assert upload(writer, b'', 'empty.txt').status_code == 400
    assert upload(writer, b'GIF89a' + b'\0' * 20, '../../x.html.gif').status_code in (201, 400)
    r = upload(writer, b'%PDF-1.4 hello', 'doc.pdf')
    assert r.status_code == 201 and r.get_json()['media']['markdown'].startswith('[doc.pdf](')


def test_svg_is_sandboxed(app, writer):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    m = upload(writer, svg, 'logo.svg').get_json()['media']
    r = app.test_client().get(m['url'])
    assert 'sandbox' in r.headers['Content-Security-Policy']


def test_post_media_links_and_delete_guard(app, writer):
    m = upload(writer, png_bytes(), 'p.png').get_json()['media']
    p = writer.api('POST', '/write/api/posts', json={'title': 'with image', 'content': m['markdown']}).get_json()['post']
    db = app.extensions['blogmd'].db
    assert db.posts.media_ids(p['id']) == [m['id']]
    detail = writer.c.get(f'/write/api/media/{m["id"]}').get_json()
    assert detail['used_by'][0]['id'] == p['id']
    assert writer.api('DELETE', f'/write/api/media/{m["id"]}').status_code == 409
    assert writer.api('DELETE', f'/write/api/media/{m["id"]}?force=1').status_code == 200
    assert app.test_client().get(m['url']).status_code == 404


def test_media_is_private_to_owner(app, writer):
    m = upload(writer, png_bytes(), 'mine.png').get_json()['media']
    other = Client(app); other.login('other', 'other-password')
    assert other.c.get('/write/api/media').get_json()['media'] == []
    assert other.api('DELETE', f'/write/api/media/{m["id"]}').status_code == 403
    assert other.api('PATCH', f'/write/api/media/{m["id"]}', json={'alt_text': 'x'}).status_code == 403


# --------------------------------------------------------------------------- import

def test_import_is_idempotent(app, content_root):
    from mdserver.services.importer import import_content
    report = import_content(app.extensions['blogmd'].db, content_root)
    assert report.posts_created == [] and len(report.skipped) >= 10


def test_import_parses_titles_and_dates(app):
    db = app.extensions['blogmd'].db
    p = db.posts.get_by_path('theta/theta-protocol')
    assert p.title == 'Theta Protocol' and not p.content.lstrip().startswith('# Theta Protocol')
    pajero = db.posts.get_by_path('personal/pajero-service')
    assert (pajero.created_at.day, pajero.created_at.month) == (9, 7)     # dd/mm/yyyy


# --------------------------------------------------------------------------- brand icon

def test_brand_icon_uses_favicon(app):
    html = app.test_client().get('/blog/').get_data(as_text=True)
    assert 'class="brand-icon" src="/favicon.ico?v=' in html
    assert 'class="brand-box"' not in html


def test_brand_icon_prefers_icon_png_then_falls_back(tmp_path, files, content_root):
    import shutil
    from mdserver.storage import open_database
    root = tmp_path / 'content'
    shutil.copytree(content_root, root)
    db = open_database(f'sqlite:///{(tmp_path / "b.db").as_posix()}')
    app = create_app(db=db, files=files, content_root=root, TESTING=True)
    c = app.test_client()

    (root / 'static' / 'icon.png').write_bytes(png_bytes(64, 64))
    html = c.get('/blog/').get_data(as_text=True)
    assert 'class="brand-icon" src="/static/icon.png?v=' in html
    assert c.get('/static/icon.png').status_code == 200

    (root / 'static' / 'icon.png').unlink()
    (root / 'static' / 'app.ico').unlink()
    html = c.get('/blog/').get_data(as_text=True)
    assert 'class="brand-box"' in html and 'brand-icon' not in html
    db.close()
