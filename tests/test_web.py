"""HTTP-level tests: public site, auth, CSRF, permissions, editor API, uploads, media, import.
Each test runs twice: SQLite + local media files, and MongoDB + GridFS media."""
import io
import re
import shutil
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import MONGO_OK, mongo_url
from mdserver.domain import Role, User
from mdserver.services.auth import hash_password, throttle
from mdserver.web.app import create_app


class Harness:
    """One TestClient (one event loop, one app lifespan) shared by several 'browsers'."""

    def __init__(self, app, tc: TestClient):
        self.app, self.tc = app, tc

    @property
    def svc(self):
        return self.app.state.blogmd

    @property
    def db(self):
        return self.svc.db

    def run(self, fn, *args):
        """Run an async function on the app's event loop."""
        return self.tc.portal.call(fn, *args)

    def client(self) -> 'Browser':
        return Browser(self.tc)


class Browser:
    """A user agent with its own cookie jar, sharing the harness's TestClient."""

    def __init__(self, tc: TestClient):
        self.tc, self.jar, self.csrf = tc, [], None

    def open(self, method, url, **kw):
        self.tc.cookies.clear()
        for cookie in self.jar:                          # restore this browser's exact cookies
            self.tc.cookies.jar.set_cookie(cookie)
        kw.setdefault('follow_redirects', False)
        r = self.tc.request(method, url, **kw)
        self.jar = list(self.tc.cookies.jar)
        return r

    def get(self, url, **kw):
        return self.open('GET', url, **kw)

    def post(self, url, **kw):
        return self.open('POST', url, **kw)

    def login(self, username, password):
        page = self.get('/write/login').text
        r = self.post('/write/login', data={'username': username, 'password': password,
                                            'csrf_token': csrf_from(page)})
        if r.status_code == 303:
            self.csrf = csrf_from(self.get('/write/').text)
        return r

    def api(self, method, url, **kw):
        headers = {'X-CSRF-Token': self.csrf or '', **kw.pop('headers', {})}
        return self.open(method, url, headers=headers, **kw)


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'csrf: "([^"]+)"', html)
    return m.group(1)


def _backend(kind, tmp_path):
    if kind == 'sqlite':
        return f'sqlite:///{(tmp_path / "web.db").as_posix()}', f'file://{(tmp_path / "media").as_posix()}'
    if not MONGO_OK:
        pytest.skip('no MongoDB')
    return mongo_url(f'blogmd_web_{uuid.uuid4().hex[:10]}'), 'gridfs://'


def make_harness(kind, tmp_path, content_root, **settings):
    db_url, media_url = _backend(kind, tmp_path)
    app = create_app(database_url=db_url, media_url=media_url, content_root=content_root,
                     bootstrap_admin=False, db_wait_seconds=0, NAV_TTL=0, **settings)
    return app


@pytest.fixture(params=['sqlite', 'mongodb'])
def h(request, tmp_path, content_root):
    app = make_harness(request.param, tmp_path, content_root, ALLOW_REGISTRATION=True)
    with TestClient(app) as tc:
        harness = Harness(app, tc)

        async def setup():
            db = harness.db
            mitch = await db.users.get_by_username('mitch')
            mitch.password_hash = hash_password('admin-password')
            await db.users.update(mitch)
            await db.users.create(User(username='wren', display_name='Wren', role=Role.WRITER,
                                       password_hash=hash_password('writer-password')))
            await db.users.create(User(username='other', display_name='Other', role=Role.WRITER,
                                       password_hash=hash_password('other-password')))
        harness.run(setup)
        throttle._fails.clear()
        yield harness
        if hasattr(harness.db, 'drop'):
            harness.run(harness.db.drop)


@pytest.fixture
def admin(h):
    c = h.client(); assert c.login('mitch', 'admin-password').status_code == 303; return c


@pytest.fixture
def writer(h):
    c = h.client(); assert c.login('wren', 'writer-password').status_code == 303; return c


def png_bytes(w=40, h=30):
    buf = io.BytesIO()
    Image.new('RGB', (w, h), '#8555f6').save(buf, 'PNG')
    return buf.getvalue()


# --------------------------------------------------------------------------- public site

def test_imported_content_and_nav(h):
    c = h.client()
    assert c.get('/').status_code == 302
    assert c.get('/blog').headers['location'] == '/blog/'
    home = c.get('/blog/').text
    assert 'Welcome to my Personal Blog' in home
    assert 'class="sidebar"' in home and '/blog/weekly/16-07-2026/' in home
    post = c.get('/blog/weekly/16-07-2026/').text
    assert 'Devlog Update #1' in post and '16th July, 2026' in post and 'aria-current="page"' in post
    folder = c.get('/blog/theta/')
    assert folder.status_code == 200 and 'Theta Protocol' in folder.text
    missing = c.get('/blog/does-not-exist/')
    assert missing.status_code == 404 and 'coder elves' in missing.text
    assert c.get('/static/image/system_pfp.jpg').status_code == 200
    assert c.get('/static/../blogmd.db').status_code == 404
    assert c.get('/assets/css/site.css').status_code == 200
    assert '_system' not in home


def test_security_headers_and_health(h):
    r = h.client().get('/blog/')
    assert r.headers['x-content-type-options'] == 'nosniff' and r.headers['x-frame-options'] == 'SAMEORIGIN'
    assert h.client().get('/healthz').json() == {'status': 'ok'}


# --------------------------------------------------------------------------- auth

def test_login_required_and_bad_password(h):
    c = h.client()
    r = c.get('/write/')
    assert r.status_code == 303 and r.headers['location'].startswith('/write/login?next=%2Fwrite%2F')
    assert c.post('/write/api/posts', json={}).status_code == 401
    r = c.login('wren', 'nope-nope-nope')
    assert r.status_code == 401 and 'Incorrect username or password' in r.text
    assert c.login('system', 'anything-at-all').status_code == 401


def test_login_throttle(h):
    c = h.client()
    for _ in range(5):
        c.login('wren', 'bad-password-x')
    r = c.login('wren', 'writer-password')
    assert r.status_code == 401 and 'Too many attempts' in r.text


def test_login_csrf_and_open_redirect(h):
    c = h.client()
    assert c.post('/write/login', data={'username': 'wren', 'password': 'writer-password'}).status_code == 400
    page = c.get('/write/login').text
    r = c.post('/write/login?next=//evil.example/', data={
        'username': 'wren', 'password': 'writer-password', 'csrf_token': csrf_from(page)})
    assert r.status_code == 303 and r.headers['location'] == '/write/'


def test_api_requires_csrf(writer):
    assert writer.open('POST', '/write/api/posts', json={'title': 'x'}).status_code == 400
    assert writer.api('POST', '/write/api/posts', json={'title': 'x'}).status_code == 201


def test_password_change_revokes_other_sessions(h, writer):
    other = h.client(); other.login('wren', 'writer-password')
    assert other.get('/write/').status_code == 200
    r = writer.post('/write/account', data={'action': 'password', 'csrf_token': writer.csrf,
                                            'current_password': 'writer-password',
                                            'new_password': 'a-brand-new-password'})
    assert r.status_code == 303 and r.headers['location'] == '/write/account'
    assert 'Password changed' in writer.get('/write/account').text     # flash shown once...
    assert 'Password changed' not in writer.get('/write/account').text  # ...then cleared
    assert other.get('/write/').status_code == 303                     # other session revoked


def test_registration(h):
    c = h.client()
    page = c.get('/write/register').text
    r = c.post('/write/register', data={'username': 'newbie', 'display_name': 'New',
                                        'password': 'long-enough-pw', 'csrf_token': csrf_from(page)})
    assert r.status_code == 303
    assert h.run(h.db.users.get_by_username, 'newbie').role == Role.WRITER


def test_logout(writer):
    r = writer.post('/write/logout', data={'csrf_token': writer.csrf})
    assert r.status_code == 303 and writer.get('/write/').status_code == 303


# --------------------------------------------------------------------------- posts API

def test_create_edit_publish_flow(h, writer):
    r = writer.api('POST', '/write/api/posts', json={'title': 'My First Post', 'content': '# Hi', 'status': 'draft'})
    assert r.status_code == 201, r.text
    post = r.json()['post']
    assert post['path'] == 'my-first-post' and post['published_at'] is None

    anon = h.client()
    assert anon.get('/blog/my-first-post/').status_code == 404
    assert writer.get('/blog/my-first-post/').status_code == 200
    assert 'My First Post' not in anon.get('/blog/').text

    r = writer.api('PUT', f'/write/api/posts/{post["id"]}', json={
        'title': 'My First Post', 'path': 'notes/first', 'status': 'published', 'content': 'Hello **world**',
        'base_updated_at': post['updated_at']})
    assert r.status_code == 200, r.text
    assert r.json()['post']['published_at']
    assert '<strong>world</strong>' in anon.get('/blog/notes/first/').text
    assert '/blog/notes/first/' in anon.get('/blog/').text          # nav cache invalidated


def test_conflicting_save_is_detected(writer):
    p = writer.api('POST', '/write/api/posts', json={'title': 'T'}).json()['post']
    stale = p['updated_at']
    time.sleep(1.1)
    assert writer.api('PUT', f'/write/api/posts/{p["id"]}', json={'title': 'T2', 'base_updated_at': stale}).status_code == 200
    r = writer.api('PUT', f'/write/api/posts/{p["id"]}', json={'title': 'T3', 'base_updated_at': stale})
    assert r.status_code == 409 and r.json()['conflict']
    assert writer.api('PUT', f'/write/api/posts/{p["id"]}', json={'title': 'T3', 'base_updated_at': stale,
                                                                 'force': True}).status_code == 200


def test_path_rules(writer, admin):
    assert writer.api('POST', '/write/api/posts', json={'title': 'a', 'path': 'weekly/16-07-2026'}).status_code == 409
    assert writer.api('POST', '/write/api/posts', json={'title': 'a', 'path': '../etc'}).status_code == 400
    assert writer.api('POST', '/write/api/posts', json={'title': 'a', 'path': '_system/x'}).status_code == 400
    assert admin.api('POST', '/write/api/posts', json={'title': 'a', 'path': '_system/x'}).status_code == 201
    assert writer.get('/write/api/check-path?path=weekly').json()['ok'] is False


def test_writers_cannot_touch_others_posts(h, writer):
    admin_post = h.run(h.db.posts.get_by_path, 'weekly/16-07-2026')
    assert writer.get(f'/write/edit/{admin_post.id}').status_code == 403
    assert writer.api('PUT', f'/write/api/posts/{admin_post.id}', json={'title': 'pwned'}).status_code == 403
    assert writer.api('DELETE', f'/write/api/posts/{admin_post.id}').status_code == 403
    assert 'Devlog Update' not in writer.get('/write/').text


def test_admin_sees_and_edits_all(h, admin):
    p = h.run(h.db.posts.get_by_path, 'weekly/16-07-2026')
    assert 'Devlog Update' in admin.get('/write/').text
    assert admin.get(f'/write/edit/{p.id}').status_code == 200


def test_untrusted_html_is_sanitised(h, writer, admin):
    evil = '<img src=x onerror="alert(1)"><script>alert(2)</script><a href="javascript:alert(3)">x</a> <mark>ok</mark>'
    html = writer.api('POST', '/write/api/render', json={'content': evil}).json()['html']
    assert 'onerror' not in html and '<script' not in html and 'javascript:' not in html
    assert '<mark>ok</mark>' in html
    assert '<script>' in admin.api('POST', '/write/api/render', json={'content': evil}).json()['html']
    p = writer.api('POST', '/write/api/posts', json={'title': 'evil', 'content': evil, 'status': 'published'}).json()['post']
    page = h.client().get(p['url']).text
    assert 'onerror' not in page and 'alert(2)' not in page


def test_render_preview(writer):
    html = writer.api('POST', '/write/api/render', json={'content': '```python\nx = 1\n```\n\n- [x] done'}).json()['html']
    assert 'language-python' in html and 'type="checkbox"' in html


# --------------------------------------------------------------------------- media

def upload(client, data, name, **form):
    return client.api('POST', '/write/api/media', files={'file': (name, io.BytesIO(data))}, data=form)


def test_upload_stores_metadata_and_serves(h, writer):
    r = upload(writer, png_bytes(40, 30), 'diagram.png', alt_text='A diagram')
    assert r.status_code == 201, r.text
    m = r.json()['media']
    assert (m['mime_type'], m['width'], m['height'], m['alt_text']) == ('image/png', 40, 30, 'A diagram')
    assert len(m['sha256']) == 64 and m['size_bytes'] > 0
    assert m['markdown'] == f'![A diagram]({m["url"]})'
    row = h.run(h.db.media.get, m['id'])
    assert row.owner_id == h.run(h.db.users.get_by_username, 'wren').id and row.storage_key.endswith('.png')

    anon = h.client()
    served = anon.get(m['url'])
    assert served.status_code == 200 and served.headers['content-type'] == 'image/png'
    assert served.content == png_bytes(40, 30)
    assert anon.get(m['url'], headers={'If-None-Match': served.headers['etag']}).status_code == 304
    assert anon.get(f'/media/{m["id"]}/diagram.png').status_code == 404
    assert anon.get(f'/media/{m["id"]}-0000000000000000/x').status_code == 404

    again = upload(writer, png_bytes(40, 30), 'copy.png')
    assert again.status_code == 200 and again.json()['media']['id'] == m['id']


def test_media_range_requests(h, writer):
    body = bytes(range(256)) * 4096                    # 1 MiB "video"
    m = upload(writer, body, 'clip.mp4').json()['media']
    anon = h.client()
    r = anon.get(m['url'], headers={'Range': 'bytes=1000-1999'})
    assert r.status_code == 206 and r.content == body[1000:2000]
    assert r.headers['content-range'] == f'bytes 1000-1999/{len(body)}'
    r = anon.get(m['url'], headers={'Range': 'bytes=-10'})
    assert r.status_code == 206 and r.content == body[-10:]
    assert anon.get(m['url'], headers={'Range': f'bytes={len(body)}-'}).status_code == 416
    full = anon.get(m['url'])
    assert full.status_code == 200 and full.content == body


def test_upload_validation(writer):
    assert upload(writer, b'#!/bin/sh\nrm -rf /', 'evil.sh').status_code == 400
    assert upload(writer, b'not really a png', 'fake.png').status_code == 400
    assert upload(writer, b'', 'empty.txt').status_code == 400
    r = upload(writer, b'%PDF-1.4 hello', 'doc.pdf')
    assert r.status_code == 201 and r.json()['media']['markdown'].startswith('[doc.pdf](')


def test_upload_size_limit(h, tmp_path, content_root):
    app = make_harness('sqlite', tmp_path / 'small', content_root, MAX_UPLOAD_MB=1)
    with TestClient(app) as tc:
        hh = Harness(app, tc)

        async def mk():
            await hh.db.users.create(User(username='u', display_name='U', password_hash=hash_password('upload-password')))
        hh.run(mk)
        c = hh.client(); c.login('u', 'upload-password')
        r = upload(c, b'x' * (2 * 1024 * 1024), 'big.txt')
        assert r.status_code == 413 and 'larger than 1 MB' in r.json()['error']


def test_svg_is_sandboxed(h, writer):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    m = upload(writer, svg, 'logo.svg').json()['media']
    assert 'sandbox' in h.client().get(m['url']).headers['content-security-policy']


def test_post_media_links_and_delete_guard(h, writer):
    m = upload(writer, png_bytes(), 'p.png').json()['media']
    p = writer.api('POST', '/write/api/posts', json={'title': 'with image', 'content': m['markdown']}).json()['post']
    assert h.run(h.db.posts.media_ids, p['id']) == [m['id']]
    assert writer.get(f'/write/api/media/{m["id"]}').json()['used_by'][0]['id'] == p['id']
    assert writer.api('DELETE', f'/write/api/media/{m["id"]}').status_code == 409
    assert writer.api('DELETE', f'/write/api/media/{m["id"]}?force=1').status_code == 200
    assert h.client().get(m['url']).status_code == 404


def test_media_is_private_to_owner(h, writer):
    m = upload(writer, png_bytes(), 'mine.png').json()['media']
    other = h.client(); other.login('other', 'other-password')
    assert other.get('/write/api/media').json()['media'] == []
    assert other.api('DELETE', f'/write/api/media/{m["id"]}').status_code == 403
    assert other.api('PATCH', f'/write/api/media/{m["id"]}', json={'alt_text': 'x'}).status_code == 403


# --------------------------------------------------------------------------- proxy headers

def test_proxy_headers_and_throttle_per_client(tmp_path, content_root):
    app = make_harness('sqlite', tmp_path, content_root, TRUST_PROXY=1, REAL_IP_HEADER='CF-Connecting-IP')
    with TestClient(app) as tc:
        hh = Harness(app, tc)

        async def mk():
            await hh.db.users.create(User(username='u', display_name='U', password_hash=hash_password('proxy-password')))
        hh.run(mk)
        throttle._fails.clear()

        def attempt(ip, pw):
            c = hh.client()
            hdr = {'CF-Connecting-IP': ip, 'X-Forwarded-For': '172.68.1.1', 'X-Forwarded-Proto': 'https'}
            page = c.get('/write/login', headers=hdr).text
            return c.post('/write/login', headers=hdr,
                          data={'username': 'u', 'password': pw, 'csrf_token': csrf_from(page)}).status_code

        assert [attempt('203.0.113.9', 'wrong-guess-xx') for _ in range(5)] == [401] * 5
        assert attempt('203.0.113.9', 'proxy-password') == 401          # attacker's IP locked
        assert attempt('198.51.100.7', 'proxy-password') == 303         # real user unaffected


# --------------------------------------------------------------------------- import + icon

def test_import_is_idempotent(h, content_root):
    from mdserver.services.importer import import_content
    report = h.run(import_content, h.db, content_root)
    assert report.posts_created == [] and len(report.skipped) >= 10


def test_import_parses_titles_and_dates(h):
    p = h.run(h.db.posts.get_by_path, 'theta/theta-protocol')
    assert p.title == 'Theta Protocol' and not p.content.lstrip().startswith('# Theta Protocol')
    pajero = h.run(h.db.posts.get_by_path, 'personal/pajero-service')
    assert (pajero.created_at.day, pajero.created_at.month) == (9, 7)


def test_brand_icon(h):
    html = h.client().get('/blog/').text
    assert 'class="brand-icon" src="/favicon.ico?v=' in html and 'class="brand-box"' not in html


def test_brand_icon_prefers_icon_png_then_falls_back(tmp_path, content_root):
    root = tmp_path / 'content'
    shutil.copytree(content_root, root)
    (root / 'static' / 'icon.png').write_bytes(png_bytes(64, 64))
    app = make_harness('sqlite', tmp_path, root)
    with TestClient(app) as tc:
        c = Harness(app, tc).client()
        assert 'class="brand-icon" src="/static/icon.png?v=' in c.get('/blog/').text
        assert c.get('/static/icon.png').status_code == 200
        (root / 'static' / 'icon.png').unlink()
        (root / 'static' / 'app.ico').unlink()
        app.state.blogmd._icon = (0.0, None)          # skip the 10 s icon cache
        html = c.get('/blog/').text
        assert 'class="brand-box"' in html and 'brand-icon' not in html
