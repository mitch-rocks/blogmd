"""
Behaviour every storage backend must have. Runs once per entry in conftest.BACKENDS,
so a new backend is "done" when this file passes against it.
"""
import asyncio
import io
from datetime import datetime, timedelta, timezone

import pytest

from mdserver.domain import Media, Post, PostStatus, Role, User
from mdserver.storage import Conflict, NotFound


async def make_user(db, name='alice', role=Role.WRITER):
    return await db.users.create(User(username=name, display_name=name.title(), role=role, password_hash='x'))


async def make_post(db, path, author=None, status=PostStatus.PUBLISHED, **kw):
    return await db.posts.create(Post(path=path, title=kw.pop('title', path or 'Home'), status=status,
                                author_id=author.id if author else None, **kw))


async def make_media(db, owner, sha='a' * 64, name='pic.png'):
    return await db.media.create(Media(owner_id=owner.id, original_name=name, storage_key=f'k/{sha[:8]}-{name}',
                                 mime_type='image/png', size_bytes=10, sha256=sha, width=2, height=3))


# --------------------------------------------------------------------------- users

async def test_user_roundtrip(db):
    u = await make_user(db)
    assert u.id is not None
    got = await db.users.get(u.id)
    assert got.username == 'alice' and got.role == Role.WRITER and got.password_hash == 'x'
    assert got.created_at.tzinfo is not None
    assert (await db.users.get_by_username('ALICE')).id == u.id   # usernames are case-insensitive


async def test_duplicate_username_conflicts(db):
    await make_user(db)
    with pytest.raises(Conflict):
        await make_user(db, 'Alice')


async def test_user_update_and_delete(db):
    u = await make_user(db)
    u.display_name = 'Al'
    u.role = Role.ADMIN
    await db.users.update(u)
    assert (await db.users.get(u.id)).display_name == 'Al'
    assert (await db.users.get(u.id)).role == Role.ADMIN
    await db.users.delete(u.id)
    assert await db.users.get(u.id) is None
    with pytest.raises(NotFound):
        await db.users.update(u)


# --------------------------------------------------------------------------- posts

async def test_post_roundtrip_and_dates(db):
    a = await make_user(db)
    when = datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc)
    p = await make_post(db, 'weekly/one', a, created_at=when, updated_at=when + timedelta(days=1),
                  description='d', content='# hi', nav_order=3)
    got = await db.posts.get_by_path('weekly/one')
    assert got.id == p.id and got.author_id == a.id and got.nav_order == 3
    assert got.created_at == when and got.updated_at == when + timedelta(days=1)
    assert got.content == '# hi' and got.status == PostStatus.PUBLISHED


async def test_post_path_unique(db):
    await make_post(db, 'x')
    with pytest.raises(Conflict):
        await make_post(db, 'x')
    p = await make_post(db, 'y')
    p.path = 'x'
    with pytest.raises(Conflict):
        await db.posts.update(p)


async def test_post_list_filters(db):
    a, b = await make_user(db, 'a'), await make_user(db, 'b')
    await make_post(db, '', a)
    await make_post(db, 'weekly', a)
    await make_post(db, 'weekly/1', a, title='Scheduler design')
    await make_post(db, 'weekly_old/1', b)                 # '_' must not act as a wildcard
    await make_post(db, 'weeklyish', b, status=PostStatus.DRAFT)

    paths = lambda posts: [p.path for p in posts]
    assert paths(await db.posts.list()) == ['', 'weekly', 'weekly/1', 'weekly_old/1', 'weeklyish']
    assert paths(await db.posts.list(prefix='weekly')) == ['weekly', 'weekly/1']
    assert paths(await db.posts.list(statuses=[PostStatus.DRAFT])) == ['weeklyish']
    assert paths(await db.posts.list(statuses=[])) == []
    assert paths(await db.posts.list(author_id=b.id)) == ['weekly_old/1', 'weeklyish']
    assert paths(await db.posts.list(search='SCHEDULER')) == ['weekly/1']
    assert paths(await db.posts.list(search='%')) == []    # LIKE wildcards are escaped


async def test_author_delete_keeps_posts(db):
    a = await make_user(db)
    p = await make_post(db, 'keep', a)
    await db.users.delete(a.id)
    assert (await db.posts.get(p.id)).author_id is None


# --------------------------------------------------------------------------- media

async def test_media_roundtrip_listing_and_hash(db):
    u, v = await make_user(db, 'u'), await make_user(db, 'v')
    m1 = await make_media(db, u, 'a' * 64)
    m2 = await db.media.create(Media(owner_id=u.id, original_name='clip.mp4', storage_key='k/clip', mime_type='video/mp4',
                               size_bytes=99, sha256='b' * 64))
    await make_media(db, v, 'c' * 64)
    got = await db.media.get(m1.id)
    assert (got.width, got.height, got.mime_type, got.kind) == (2, 3, 'image/png', 'image')
    assert [m.id for m in await db.media.list(owner_id=u.id)] == [m2.id, m1.id]       # newest first
    assert [m.id for m in await db.media.list(owner_id=u.id, kind='video')] == [m2.id]
    assert len(await db.media.list()) == 3
    assert len(await db.media.list(limit=1, offset=1)) == 1
    assert (await db.media.find_by_hash(u.id, 'a' * 64)).id == m1.id
    assert await db.media.find_by_hash(v.id, 'a' * 64) is None
    got.alt_text = 'alt'
    await db.media.update(got)
    assert (await db.media.get(m1.id)).alt_text == 'alt'


async def test_post_media_links(db):
    u = await make_user(db)
    p = await make_post(db, 'p', u)
    m1, m2 = await make_media(db, u, '1' * 64), await make_media(db, u, '2' * 64)
    await db.posts.set_media(p.id, [m1.id, m2.id, m2.id, 999])     # dupes + unknown ids ignored
    assert await db.posts.media_ids(p.id) == sorted([m1.id, m2.id])
    assert await db.media.usage(m1.id) == [p.id]
    await db.posts.set_media(p.id, [m2.id])
    assert await db.media.usage(m1.id) == []
    await db.media.delete(m2.id)
    assert await db.posts.media_ids(p.id) == []
    await db.posts.set_media(p.id, [m1.id])
    await db.posts.delete(p.id)
    assert await db.media.usage(m1.id) == []


# --------------------------------------------------------------------------- settings / tx

async def test_settings(db):
    assert await db.settings.get('k') is None
    assert await db.settings.get('k', 'dflt') == 'dflt'
    await db.settings.set('k', '1')
    await db.settings.set('k', '2')
    assert await db.settings.get('k') == '2'


async def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await make_user(db, 'rolled')
            await make_post(db, 'rolled-post')
            raise RuntimeError
    assert await db.users.get_by_username('rolled') is None
    assert await db.posts.get_by_path('rolled-post') is None


async def test_nested_transaction_joins_outer(db):
    # nested blocks join the enclosing transaction (MongoDB has no savepoints):
    # success commits with the outer block, an escaping error rolls back everything
    async with db.transaction():
        await make_user(db, 'outer')
        async with db.transaction():
            await make_user(db, 'inner')
    assert await db.users.get_by_username('inner') is not None

    with pytest.raises(Conflict):
        async with db.transaction():
            await make_user(db, 'outer2')
            async with db.transaction():
                await make_user(db, 'outer')        # duplicate -> Conflict escapes both blocks
    assert await db.users.get_by_username('outer2') is None


async def test_concurrent_tasks(db):
    # many coroutines at once (as under web load): unique ids, nothing lost
    users = await asyncio.gather(*(make_user(db, f'user{i}') for i in range(25)))
    assert len({u.id for u in users}) == 25
    assert await db.users.count() == 25
    found = await asyncio.gather(*(db.users.get_by_username(f'USER{i}') for i in range(25)))
    assert all(found)


async def test_transactions_isolated_from_other_tasks(db):
    gate = asyncio.Event()

    async def failing():
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await make_user(db, 'doomed')
                gate.set()
                await asyncio.sleep(0.05)
                raise RuntimeError

    async def bystander():
        await gate.wait()
        await make_user(db, 'bystander')          # outside the failing transaction

    await asyncio.gather(failing(), bystander())
    assert await db.users.get_by_username('doomed') is None
    assert await db.users.get_by_username('bystander') is not None


async def test_ping_and_migrate_are_idempotent(db):
    await db.ping()
    await db.migrate()
    await db.migrate()
    await make_user(db)


# --------------------------------------------------------------------------- file stores

@pytest.fixture(params=['local', 'gridfs'])
async def store(request, tmp_path):
    from conftest import MONGO_OK, mongo_url
    from mdserver.storage import open_database, open_file_store
    import uuid
    if request.param == 'local':
        yield open_file_store(f'file://{(tmp_path / "media").as_posix()}')
        return
    if not MONGO_OK:
        pytest.skip('no MongoDB')
    mdb = await open_database(mongo_url(f'blogmd_fs_{uuid.uuid4().hex[:8]}'))
    yield open_file_store('gridfs://', mdb)
    await mdb.drop()
    await mdb.close()


async def _read(store, key, start=0, end=None, chunk=7):
    return b''.join([c async for c in store.iter_range(key, start, end, chunk_size=chunk)])


async def test_file_store_roundtrip_and_ranges(store):
    body = bytes(range(256)) * 2000                  # 512 KB: spans several GridFS chunks
    await store.save('2026/10/a.bin', io.BytesIO(body))
    assert await store.exists('2026/10/a.bin')
    assert await _read(store, '2026/10/a.bin', chunk=64 * 1024) == body
    assert await _read(store, '2026/10/a.bin', 10, 19) == body[10:20]
    assert await _read(store, '2026/10/a.bin', 300_000, None, 100_000) == body[300_000:]
    await store.delete('2026/10/a.bin')
    assert not await store.exists('2026/10/a.bin')
    with pytest.raises(NotFound):
        await _read(store, 'missing')


async def test_local_store_rejects_escaping_keys(files):
    with pytest.raises(Exception):
        await files.save('../escape.txt', io.BytesIO(b'x'))
