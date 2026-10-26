"""
MongoDB backend (pymongo's native async driver).

    DATABASE_URL=mongodb://mongo:27017/blogmd?replicaSet=rs0

Collections
    users     {_id:int, username, username_ci, display_name, role, avatar_url, bio, password_hash, created_at}
    posts     {_id:int, path, title, description, content, author_id, status, nav_order,
               created_at, updated_at, published_at, media_ids:[int]}
    media     {_id:int, owner_id, original_name, storage_key, mime_type, size_bytes, sha256,
               width, height, duration_s, alt_text, created_at}
    settings  {_id:key, value}
    counters  {_id:collection, seq}      integer ids keep URLs like /write/edit/12 stable

Transactions need a replica set (a single-node one is fine - see docker-compose.yml). Against a
standalone server, transaction() still works as a block but writes aren't atomic (logged once).
"""
from __future__ import annotations

import contextvars
import logging
import re
from contextlib import asynccontextmanager
from datetime import timezone
from urllib.parse import urlparse

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from mdserver.domain import Media, Post, PostStatus, Role, User
from mdserver.storage.base import (Conflict, Database, MediaRepository, NotFound, PostRepository,
                                   SettingsRepository, UserRepository)

log = logging.getLogger(__name__)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- mapping

def _user_doc(u: User) -> dict:
    return {'username': u.username, 'username_ci': u.username.lower(), 'display_name': u.display_name,
            'role': u.role.value, 'avatar_url': u.avatar_url, 'bio': u.bio,
            'password_hash': u.password_hash, 'created_at': _aware(u.created_at)}


def _to_user(d: dict) -> User:
    return User(id=d['_id'], username=d['username'], display_name=d['display_name'], role=Role(d['role']),
                avatar_url=d.get('avatar_url', ''), bio=d.get('bio', ''),
                password_hash=d.get('password_hash'), created_at=_aware(d['created_at']))


def _post_doc(p: Post) -> dict:
    return {'path': p.path, 'title': p.title, 'description': p.description, 'content': p.content,
            'author_id': p.author_id, 'status': p.status.value, 'nav_order': p.nav_order,
            'created_at': _aware(p.created_at), 'updated_at': _aware(p.updated_at),
            'published_at': _aware(p.published_at)}


def _to_post(d: dict) -> Post:
    return Post(id=d['_id'], path=d['path'], title=d['title'], description=d.get('description', ''),
                content=d.get('content', ''), author_id=d.get('author_id'), status=PostStatus(d['status']),
                nav_order=d.get('nav_order', 0), created_at=_aware(d['created_at']),
                updated_at=_aware(d['updated_at']), published_at=_aware(d.get('published_at')))


def _media_doc(m: Media) -> dict:
    return {'owner_id': m.owner_id, 'original_name': m.original_name, 'storage_key': m.storage_key,
            'mime_type': m.mime_type, 'size_bytes': m.size_bytes, 'sha256': m.sha256,
            'width': m.width, 'height': m.height, 'duration_s': m.duration_s,
            'alt_text': m.alt_text, 'created_at': _aware(m.created_at)}


def _to_media(d: dict) -> Media:
    return Media(id=d['_id'], owner_id=d['owner_id'], original_name=d['original_name'],
                 storage_key=d['storage_key'], mime_type=d['mime_type'], size_bytes=d['size_bytes'],
                 sha256=d['sha256'], width=d.get('width'), height=d.get('height'),
                 duration_s=d.get('duration_s'), alt_text=d.get('alt_text', ''),
                 created_at=_aware(d['created_at']))


# --------------------------------------------------------------------------- repositories

class _Repo:
    def __init__(self, db: 'MongoDatabase'):
        self.db = db

    @property
    def s(self):
        """The active transaction session, if any (passed to every operation)."""
        return self.db._session.get()


class MongoUsers(_Repo, UserRepository):
    async def get(self, user_id):
        d = await self.db.d.users.find_one({'_id': user_id}, session=self.s)
        return _to_user(d) if d else None

    async def get_by_username(self, username):
        d = await self.db.d.users.find_one({'username_ci': (username or '').lower()}, session=self.s)
        return _to_user(d) if d else None

    async def list(self):
        return [_to_user(d) async for d in self.db.d.users.find({}, session=self.s).sort('username_ci', ASCENDING)]

    async def create(self, user):
        uid = await self.db._next_id('users')
        try:
            await self.db.d.users.insert_one({'_id': uid, **_user_doc(user)}, session=self.s)
        except DuplicateKeyError as e:
            raise Conflict(str(e)) from e
        user.id = uid
        return user

    async def update(self, user):
        try:
            r = await self.db.d.users.update_one({'_id': user.id}, {'$set': _user_doc(user)}, session=self.s)
        except DuplicateKeyError as e:
            raise Conflict(str(e)) from e
        if r.matched_count == 0:
            raise NotFound(f'user {user.id}')
        return user

    async def delete(self, user_id):
        async with self.db.transaction():
            media_ids = [d['_id'] async for d in self.db.d.media.find({'owner_id': user_id}, {'_id': 1}, session=self.s)]
            if media_ids:
                await self.db.d.posts.update_many({}, {'$pull': {'media_ids': {'$in': media_ids}}}, session=self.s)
                await self.db.d.media.delete_many({'_id': {'$in': media_ids}}, session=self.s)
            await self.db.d.posts.update_many({'author_id': user_id}, {'$set': {'author_id': None}}, session=self.s)
            await self.db.d.users.delete_one({'_id': user_id}, session=self.s)

    async def count(self):
        return await self.db.d.users.count_documents({}, session=self.s)


class MongoPosts(_Repo, PostRepository):
    async def get(self, post_id):
        d = await self.db.d.posts.find_one({'_id': post_id}, session=self.s)
        return _to_post(d) if d else None

    async def get_by_path(self, path):
        d = await self.db.d.posts.find_one({'path': path}, session=self.s)
        return _to_post(d) if d else None

    async def list(self, *, statuses=None, author_id=None, prefix=None, search=None):
        q: dict = {}
        if statuses is not None:
            q['status'] = {'$in': [PostStatus(s).value for s in statuses]}
        if author_id is not None:
            q['author_id'] = author_id
        if prefix:
            q.setdefault('$and', []).append({'$or': [{'path': prefix},
                                                     {'path': {'$regex': '^' + re.escape(prefix + '/')}}]})
        if search:
            rx = {'$regex': re.escape(search), '$options': 'i'}
            q.setdefault('$and', []).append({'$or': [{'title': rx}, {'path': rx}, {'description': rx}]})
        cur = self.db.d.posts.find(q, {'media_ids': 0}, session=self.s).sort('path', ASCENDING)
        return [_to_post(d) async for d in cur]

    async def create(self, post):
        pid = await self.db._next_id('posts')
        try:
            await self.db.d.posts.insert_one({'_id': pid, **_post_doc(post), 'media_ids': []}, session=self.s)
        except DuplicateKeyError as e:
            raise Conflict(str(e)) from e
        post.id = pid
        return post

    async def update(self, post):
        try:
            r = await self.db.d.posts.update_one({'_id': post.id}, {'$set': _post_doc(post)}, session=self.s)
        except DuplicateKeyError as e:
            raise Conflict(str(e)) from e
        if r.matched_count == 0:
            raise NotFound(f'post {post.id}')
        return post

    async def delete(self, post_id):
        await self.db.d.posts.delete_one({'_id': post_id}, session=self.s)

    async def count(self):
        return await self.db.d.posts.count_documents({}, session=self.s)

    async def set_media(self, post_id, media_ids):
        wanted = sorted(set(int(i) for i in media_ids))
        existing = [d['_id'] async for d in self.db.d.media.find({'_id': {'$in': wanted}}, {'_id': 1}, session=self.s)]
        await self.db.d.posts.update_one({'_id': post_id}, {'$set': {'media_ids': sorted(existing)}}, session=self.s)

    async def media_ids(self, post_id):
        d = await self.db.d.posts.find_one({'_id': post_id}, {'media_ids': 1}, session=self.s)
        return sorted(d.get('media_ids', [])) if d else []


class MongoMedia(_Repo, MediaRepository):
    async def get(self, media_id):
        d = await self.db.d.media.find_one({'_id': media_id}, session=self.s)
        return _to_media(d) if d else None

    async def find_by_hash(self, owner_id, sha256):
        d = await self.db.d.media.find_one({'owner_id': owner_id, 'sha256': sha256},
                                           sort=[('_id', ASCENDING)], session=self.s)
        return _to_media(d) if d else None

    async def list(self, *, owner_id=None, kind=None, limit=100, offset=0):
        q: dict = {}
        if owner_id is not None:
            q['owner_id'] = owner_id
        if kind:
            q['mime_type'] = {'$regex': '^' + re.escape(kind + '/')}
        cur = (self.db.d.media.find(q, session=self.s)
               .sort([('created_at', DESCENDING), ('_id', DESCENDING)]).skip(int(offset)).limit(int(limit)))
        return [_to_media(d) async for d in cur]

    async def create(self, media):
        mid = await self.db._next_id('media')
        try:
            await self.db.d.media.insert_one({'_id': mid, **_media_doc(media)}, session=self.s)
        except DuplicateKeyError as e:
            raise Conflict(str(e)) from e
        media.id = mid
        return media

    async def update(self, media):
        r = await self.db.d.media.update_one(
            {'_id': media.id},
            {'$set': {'original_name': media.original_name, 'alt_text': media.alt_text,
                      'width': media.width, 'height': media.height, 'duration_s': media.duration_s}},
            session=self.s)
        if r.matched_count == 0:
            raise NotFound(f'media {media.id}')
        return media

    async def delete(self, media_id):
        async with self.db.transaction():
            await self.db.d.posts.update_many({'media_ids': media_id}, {'$pull': {'media_ids': media_id}}, session=self.s)
            await self.db.d.media.delete_one({'_id': media_id}, session=self.s)

    async def usage(self, media_id):
        return sorted([d['_id'] async for d in self.db.d.posts.find({'media_ids': media_id}, {'_id': 1}, session=self.s)])


class MongoSettings(_Repo, SettingsRepository):
    async def get(self, key, default=None):
        d = await self.db.d.settings.find_one({'_id': key}, session=self.s)
        return d['value'] if d else default

    async def set(self, key, value):
        await self.db.d.settings.update_one({'_id': key}, {'$set': {'value': value}}, upsert=True, session=self.s)


# --------------------------------------------------------------------------- database

class MongoDatabase(Database):
    schemes = ('mongodb', 'mongodb+srv')

    def __init__(self, url: str, db_name: str | None = None):
        self.url = url
        self.client = AsyncMongoClient(url, tz_aware=True, serverSelectionTimeoutMS=10_000,
                                       appname='blogmd')
        name = db_name or urlparse(url).path.lstrip('/').split('?')[0] or 'blogmd'
        self.d = self.client[name]
        self._session = contextvars.ContextVar(f'mongo_session_{id(self)}', default=None)
        self._tx_supported: bool | None = None
        self.users = MongoUsers(self)
        self.posts = MongoPosts(self)
        self.media = MongoMedia(self)
        self.settings = MongoSettings(self)

    @classmethod
    def from_url(cls, url: str) -> 'MongoDatabase':
        return cls(url)

    async def _next_id(self, name: str) -> int:
        # deliberately outside any transaction: concurrent creates never conflict on the counter
        # (a rolled-back insert just leaves a gap in the id sequence)
        d = await self.d.counters.find_one_and_update(
            {'_id': name}, {'$inc': {'seq': 1}}, upsert=True, return_document=ReturnDocument.AFTER)
        return d['seq']

    async def migrate(self) -> None:
        await self.d.users.create_index('username_ci', unique=True)
        await self.d.posts.create_index('path', unique=True)
        await self.d.posts.create_index('author_id')
        await self.d.posts.create_index('status')
        await self.d.posts.create_index('media_ids')
        await self.d.media.create_index('storage_key', unique=True)
        await self.d.media.create_index([('owner_id', ASCENDING), ('created_at', DESCENDING)])
        await self.d.media.create_index([('owner_id', ASCENDING), ('sha256', ASCENDING)])
        hello = await self.client.admin.command('hello')
        self._tx_supported = bool(hello.get('setName')) or hello.get('msg') == 'isdbgrid'
        if not self._tx_supported:
            log.warning('MongoDB is a standalone server: multi-document writes are not atomic. '
                        'Run it as a replica set (even single-node) for transactions.')

    async def ping(self) -> None:
        await self.client.admin.command('ping')

    @asynccontextmanager
    async def transaction(self):
        if self._session.get() is not None or not self._tx_supported:
            yield                                   # nested (joins outer) or unsupported
            return
        async with self.client.start_session() as session:
            await session.start_transaction()
            token = self._session.set(session)
            try:
                yield
            except BaseException:
                if session.in_transaction:
                    await session.abort_transaction()
                raise
            else:
                await session.commit_transaction()
            finally:
                self._session.reset(token)

    async def close(self) -> None:
        await self.client.close()

    async def drop(self) -> None:
        """Delete the whole database (tests only)."""
        await self.client.drop_database(self.d.name)
