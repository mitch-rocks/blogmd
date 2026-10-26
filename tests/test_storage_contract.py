"""
Behaviour every storage backend must have. Runs once per entry in conftest.BACKENDS,
so a new backend is "done" when this file passes against it.
"""
import io
import threading
from datetime import datetime, timedelta, timezone

import pytest

from mdserver.domain import Media, Post, PostStatus, Role, User
from mdserver.storage import Conflict, NotFound


def make_user(db, name='alice', role=Role.WRITER):
    return db.users.create(User(username=name, display_name=name.title(), role=role, password_hash='x'))


def make_post(db, path, author=None, status=PostStatus.PUBLISHED, **kw):
    return db.posts.create(Post(path=path, title=kw.pop('title', path or 'Home'), status=status,
                                author_id=author.id if author else None, **kw))


def make_media(db, owner, sha='a' * 64, name='pic.png'):
    return db.media.create(Media(owner_id=owner.id, original_name=name, storage_key=f'k/{sha[:8]}-{name}',
                                 mime_type='image/png', size_bytes=10, sha256=sha, width=2, height=3))


# --------------------------------------------------------------------------- users

def test_user_roundtrip(db):
    u = make_user(db)
    assert u.id is not None
    got = db.users.get(u.id)
    assert got.username == 'alice' and got.role == Role.WRITER and got.password_hash == 'x'
    assert got.created_at.tzinfo is not None
    assert db.users.get_by_username('ALICE').id == u.id   # usernames are case-insensitive


def test_duplicate_username_conflicts(db):
    make_user(db)
    with pytest.raises(Conflict):
        make_user(db, 'Alice')


def test_user_update_and_delete(db):
    u = make_user(db)
    u.display_name = 'Al'
    u.role = Role.ADMIN
    db.users.update(u)
    assert db.users.get(u.id).display_name == 'Al'
    assert db.users.get(u.id).role == Role.ADMIN
    db.users.delete(u.id)
    assert db.users.get(u.id) is None
    with pytest.raises(NotFound):
        db.users.update(u)


# --------------------------------------------------------------------------- posts

def test_post_roundtrip_and_dates(db):
    a = make_user(db)
    when = datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc)
    p = make_post(db, 'weekly/one', a, created_at=when, updated_at=when + timedelta(days=1),
                  description='d', content='# hi', nav_order=3)
    got = db.posts.get_by_path('weekly/one')
    assert got.id == p.id and got.author_id == a.id and got.nav_order == 3
    assert got.created_at == when and got.updated_at == when + timedelta(days=1)
    assert got.content == '# hi' and got.status == PostStatus.PUBLISHED


def test_post_path_unique(db):
    make_post(db, 'x')
    with pytest.raises(Conflict):
        make_post(db, 'x')
    p = make_post(db, 'y')
    p.path = 'x'
    with pytest.raises(Conflict):
        db.posts.update(p)


def test_post_list_filters(db):
    a, b = make_user(db, 'a'), make_user(db, 'b')
    make_post(db, '', a)
    make_post(db, 'weekly', a)
    make_post(db, 'weekly/1', a, title='Scheduler design')
    make_post(db, 'weekly_old/1', b)                 # '_' must not act as a wildcard
    make_post(db, 'weeklyish', b, status=PostStatus.DRAFT)

    paths = lambda posts: [p.path for p in posts]
    assert paths(db.posts.list()) == ['', 'weekly', 'weekly/1', 'weekly_old/1', 'weeklyish']
    assert paths(db.posts.list(prefix='weekly')) == ['weekly', 'weekly/1']
    assert paths(db.posts.list(statuses=[PostStatus.DRAFT])) == ['weeklyish']
    assert paths(db.posts.list(statuses=[])) == []
    assert paths(db.posts.list(author_id=b.id)) == ['weekly_old/1', 'weeklyish']
    assert paths(db.posts.list(search='SCHEDULER')) == ['weekly/1']
    assert paths(db.posts.list(search='%')) == []    # LIKE wildcards are escaped


def test_author_delete_keeps_posts(db):
    a = make_user(db)
    p = make_post(db, 'keep', a)
    db.users.delete(a.id)
    assert db.posts.get(p.id).author_id is None


# --------------------------------------------------------------------------- media

def test_media_roundtrip_listing_and_hash(db):
    u, v = make_user(db, 'u'), make_user(db, 'v')
    m1 = make_media(db, u, 'a' * 64)
    m2 = db.media.create(Media(owner_id=u.id, original_name='clip.mp4', storage_key='k/clip', mime_type='video/mp4',
                               size_bytes=99, sha256='b' * 64))
    make_media(db, v, 'c' * 64)
    got = db.media.get(m1.id)
    assert (got.width, got.height, got.mime_type, got.kind) == (2, 3, 'image/png', 'image')
    assert [m.id for m in db.media.list(owner_id=u.id)] == [m2.id, m1.id]       # newest first
    assert [m.id for m in db.media.list(owner_id=u.id, kind='video')] == [m2.id]
    assert len(db.media.list()) == 3
    assert len(db.media.list(limit=1, offset=1)) == 1
    assert db.media.find_by_hash(u.id, 'a' * 64).id == m1.id
    assert db.media.find_by_hash(v.id, 'a' * 64) is None
    got.alt_text = 'alt'
    db.media.update(got)
    assert db.media.get(m1.id).alt_text == 'alt'


def test_post_media_links(db):
    u = make_user(db)
    p = make_post(db, 'p', u)
    m1, m2 = make_media(db, u, '1' * 64), make_media(db, u, '2' * 64)
    db.posts.set_media(p.id, [m1.id, m2.id, m2.id, 999])     # dupes + unknown ids ignored
    assert db.posts.media_ids(p.id) == sorted([m1.id, m2.id])
    assert db.media.usage(m1.id) == [p.id]
    db.posts.set_media(p.id, [m2.id])
    assert db.media.usage(m1.id) == []
    db.media.delete(m2.id)
    assert db.posts.media_ids(p.id) == []
    db.posts.set_media(p.id, [m1.id])
    db.posts.delete(p.id)
    assert db.media.usage(m1.id) == []


# --------------------------------------------------------------------------- settings / tx

def test_settings(db):
    assert db.settings.get('k') is None
    assert db.settings.get('k', 'dflt') == 'dflt'
    db.settings.set('k', '1')
    db.settings.set('k', '2')
    assert db.settings.get('k') == '2'


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            make_user(db, 'rolled')
            raise RuntimeError
    assert db.users.get_by_username('rolled') is None


def test_nested_transaction(db):
    with db.transaction():
        make_user(db, 'outer')
        with pytest.raises(Conflict):
            with db.transaction():
                make_user(db, 'inner')
                make_user(db, 'outer')         # conflict inside the savepoint
    assert db.users.get_by_username('outer') is not None
    assert db.users.get_by_username('inner') is None


def test_visible_across_threads(db):
    make_user(db, 'shared')
    seen = []
    t = threading.Thread(target=lambda: seen.append(db.users.get_by_username('shared')))
    t.start(); t.join()
    assert seen[0] is not None


def test_migrate_is_idempotent(db):
    db.migrate()
    db.migrate()
    make_user(db)


# --------------------------------------------------------------------------- file store

def test_local_file_store(files):
    files.save('2026/10/a.txt', io.BytesIO(b'hello'))
    assert files.exists('2026/10/a.txt')
    with files.open('2026/10/a.txt') as fh:
        assert fh.read() == b'hello'
    files.delete('2026/10/a.txt')
    assert not files.exists('2026/10/a.txt')
    with pytest.raises(NotFound):
        files.open('missing')
    with pytest.raises(Exception):
        files.save('../escape.txt', io.BytesIO(b'x'))
