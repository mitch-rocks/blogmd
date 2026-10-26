"""SQLite implementation of the storage interfaces (stdlib sqlite3, no ORM)."""
from __future__ import annotations

import itertools
import pathlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator

from mdserver.domain import Media, Post, PostStatus, Role, User, utcnow
from mdserver.storage.base import (Conflict, Database, MediaRepository, NotFound,
                                   PostRepository, SettingsRepository, UserRepository)
from .schema import MIGRATIONS


# --------------------------------------------------------------------------- conversions

def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='seconds')


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_user(r: sqlite3.Row) -> User:
    return User(id=r['id'], username=r['username'], display_name=r['display_name'],
                role=Role(r['role']), avatar_url=r['avatar_url'], bio=r['bio'],
                password_hash=r['password_hash'], created_at=_parse_dt(r['created_at']))


def _row_to_post(r: sqlite3.Row) -> Post:
    return Post(id=r['id'], path=r['path'], title=r['title'], description=r['description'],
                content=r['content'], author_id=r['author_id'], status=PostStatus(r['status']),
                nav_order=r['nav_order'], created_at=_parse_dt(r['created_at']),
                updated_at=_parse_dt(r['updated_at']), published_at=_parse_dt(r['published_at']))


def _row_to_media(r: sqlite3.Row) -> Media:
    return Media(id=r['id'], owner_id=r['owner_id'], original_name=r['original_name'],
                 storage_key=r['storage_key'], mime_type=r['mime_type'], size_bytes=r['size_bytes'],
                 sha256=r['sha256'], width=r['width'], height=r['height'],
                 duration_s=r['duration_s'], alt_text=r['alt_text'],
                 created_at=_parse_dt(r['created_at']))


def _like(term: str) -> str:
    return '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


# --------------------------------------------------------------------------- database

class SQLiteDatabase(Database):
    schemes = ('sqlite',)
    _mem_counter = itertools.count()

    def __init__(self, path: str):
        self._local = threading.local()
        self._anchor = None
        if path in ('', ':memory:'):
            # shared-cache in-memory DB so every thread sees the same data
            self._target = f'file:blogmd-mem-{next(self._mem_counter)}?mode=memory&cache=shared'
            self._uri = True
            self._anchor = self._connect()      # keeps the in-memory DB alive
        else:
            p = pathlib.Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._target, self._uri = str(p), False

        self.users = SQLiteUsers(self)
        self.posts = SQLitePosts(self)
        self.media = SQLiteMedia(self)
        self.settings = SQLiteSettings(self)

    @classmethod
    def from_url(cls, url: str) -> 'SQLiteDatabase':
        # sqlite:///relative/path.db   sqlite:////absolute/path.db   sqlite:///:memory:
        if not url.startswith('sqlite://'):
            raise ValueError(f'Not a sqlite URL: {url}')
        rest = url[len('sqlite://'):]
        return cls(rest[1:] if rest.startswith('/') else rest)

    # -- connections ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._target, uri=self._uri, isolation_level=None,
                               check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 15000')
        if not self._uri:
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA synchronous = NORMAL')
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._local.conn = self._connect()
            self._local.depth = 0
        return conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        conn = self.conn
        depth = self._local.depth
        sp = f'sp_{depth}'
        conn.execute('BEGIN IMMEDIATE' if depth == 0 else f'SAVEPOINT {sp}')
        self._local.depth += 1
        try:
            yield
        except BaseException:
            conn.execute('ROLLBACK' if depth == 0 else f'ROLLBACK TO {sp}')
            if depth:
                conn.execute(f'RELEASE {sp}')
            raise
        else:
            conn.execute('COMMIT' if depth == 0 else f'RELEASE {sp}')
        finally:
            self._local.depth -= 1

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, tuple(params))
        except sqlite3.IntegrityError as e:
            if 'UNIQUE' in str(e):
                raise Conflict(str(e)) from e
            raise

    def migrate(self) -> None:
        with self.transaction():
            version = self.conn.execute('PRAGMA user_version').fetchone()[0]
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                for statement in filter(str.strip, script.split(';')):
                    self.conn.execute(statement)
                self.conn.execute(f'PRAGMA user_version = {i}')

    @property
    def schema_version(self) -> int:
        return self.conn.execute('PRAGMA user_version').fetchone()[0]

    def close(self) -> None:
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None


class _Repo:
    def __init__(self, db: SQLiteDatabase):
        self.db = db

    def _one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- users

class SQLiteUsers(_Repo, UserRepository):
    def get(self, user_id):
        r = self._one('SELECT * FROM users WHERE id = ?', (user_id,))
        return _row_to_user(r) if r else None

    def get_by_username(self, username):
        r = self._one('SELECT * FROM users WHERE username = ?', (username,))
        return _row_to_user(r) if r else None

    def list(self):
        return [_row_to_user(r) for r in self._all('SELECT * FROM users ORDER BY username')]

    def create(self, user):
        cur = self.db.execute(
            'INSERT INTO users (username, display_name, role, avatar_url, bio, password_hash, created_at)'
            ' VALUES (?,?,?,?,?,?,?)',
            (user.username, user.display_name, user.role.value, user.avatar_url, user.bio,
             user.password_hash, _dt(user.created_at)))
        user.id = cur.lastrowid
        return user

    def update(self, user):
        cur = self.db.execute(
            'UPDATE users SET username=?, display_name=?, role=?, avatar_url=?, bio=?, password_hash=?'
            ' WHERE id=?',
            (user.username, user.display_name, user.role.value, user.avatar_url, user.bio,
             user.password_hash, user.id))
        if cur.rowcount == 0:
            raise NotFound(f'user {user.id}')
        return user

    def delete(self, user_id):
        self.db.execute('DELETE FROM users WHERE id = ?', (user_id,))

    def count(self):
        return self._one('SELECT COUNT(*) FROM users')[0]


# --------------------------------------------------------------------------- posts

class SQLitePosts(_Repo, PostRepository):
    def get(self, post_id):
        r = self._one('SELECT * FROM posts WHERE id = ?', (post_id,))
        return _row_to_post(r) if r else None

    def get_by_path(self, path):
        r = self._one('SELECT * FROM posts WHERE path = ?', (path,))
        return _row_to_post(r) if r else None

    def list(self, *, statuses=None, author_id=None, prefix=None, search=None):
        where, params = [], []
        if statuses is not None:
            statuses = [PostStatus(s).value for s in statuses]
            where.append(f'status IN ({",".join("?" * len(statuses))})' if statuses else '0')
            params += statuses
        if author_id is not None:
            where.append('author_id = ?')
            params.append(author_id)
        if prefix:
            where.append("(path = ? OR substr(path, 1, ?) = ?)")
            params += [prefix, len(prefix) + 1, prefix + '/']
        if search:
            where.append("(title LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
            params += [_like(search)] * 3
        sql = 'SELECT * FROM posts' + (' WHERE ' + ' AND '.join(where) if where else '')
        sql += ' ORDER BY path'
        return [_row_to_post(r) for r in self._all(sql, params)]

    def create(self, post):
        cur = self.db.execute(
            'INSERT INTO posts (path, title, description, content, author_id, status, nav_order,'
            ' created_at, updated_at, published_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (post.path, post.title, post.description, post.content, post.author_id,
             post.status.value, post.nav_order, _dt(post.created_at), _dt(post.updated_at),
             _dt(post.published_at)))
        post.id = cur.lastrowid
        return post

    def update(self, post):
        cur = self.db.execute(
            'UPDATE posts SET path=?, title=?, description=?, content=?, author_id=?, status=?,'
            ' nav_order=?, created_at=?, updated_at=?, published_at=? WHERE id=?',
            (post.path, post.title, post.description, post.content, post.author_id,
             post.status.value, post.nav_order, _dt(post.created_at), _dt(post.updated_at),
             _dt(post.published_at), post.id))
        if cur.rowcount == 0:
            raise NotFound(f'post {post.id}')
        return post

    def delete(self, post_id):
        self.db.execute('DELETE FROM posts WHERE id = ?', (post_id,))

    def count(self):
        return self._one('SELECT COUNT(*) FROM posts')[0]

    def set_media(self, post_id, media_ids):
        with self.db.transaction():
            self.db.execute('DELETE FROM post_media WHERE post_id = ?', (post_id,))
            for mid in set(media_ids):
                self.db.execute(
                    'INSERT OR IGNORE INTO post_media (post_id, media_id)'
                    ' SELECT ?, id FROM media WHERE id = ?', (post_id, mid))

    def media_ids(self, post_id):
        return [r[0] for r in self._all(
            'SELECT media_id FROM post_media WHERE post_id = ? ORDER BY media_id', (post_id,))]


# --------------------------------------------------------------------------- media

class SQLiteMedia(_Repo, MediaRepository):
    def get(self, media_id):
        r = self._one('SELECT * FROM media WHERE id = ?', (media_id,))
        return _row_to_media(r) if r else None

    def find_by_hash(self, owner_id, sha256):
        r = self._one('SELECT * FROM media WHERE owner_id = ? AND sha256 = ? ORDER BY id LIMIT 1',
                      (owner_id, sha256))
        return _row_to_media(r) if r else None

    def list(self, *, owner_id=None, kind=None, limit=100, offset=0):
        where, params = [], []
        if owner_id is not None:
            where.append('owner_id = ?')
            params.append(owner_id)
        if kind:
            where.append("substr(mime_type, 1, ?) = ?")
            params += [len(kind) + 1, kind + '/']
        sql = 'SELECT * FROM media' + (' WHERE ' + ' AND '.join(where) if where else '')
        sql += ' ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?'
        return [_row_to_media(r) for r in self._all(sql, params + [int(limit), int(offset)])]

    def create(self, media):
        cur = self.db.execute(
            'INSERT INTO media (owner_id, original_name, storage_key, mime_type, size_bytes, sha256,'
            ' width, height, duration_s, alt_text, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (media.owner_id, media.original_name, media.storage_key, media.mime_type,
             media.size_bytes, media.sha256, media.width, media.height, media.duration_s,
             media.alt_text, _dt(media.created_at)))
        media.id = cur.lastrowid
        return media

    def update(self, media):
        cur = self.db.execute(
            'UPDATE media SET original_name=?, alt_text=?, width=?, height=?, duration_s=? WHERE id=?',
            (media.original_name, media.alt_text, media.width, media.height, media.duration_s,
             media.id))
        if cur.rowcount == 0:
            raise NotFound(f'media {media.id}')
        return media

    def delete(self, media_id):
        self.db.execute('DELETE FROM media WHERE id = ?', (media_id,))

    def usage(self, media_id):
        return [r[0] for r in self._all(
            'SELECT post_id FROM post_media WHERE media_id = ? ORDER BY post_id', (media_id,))]


# --------------------------------------------------------------------------- settings

class SQLiteSettings(_Repo, SettingsRepository):
    def get(self, key, default=None):
        r = self._one('SELECT value FROM settings WHERE key = ?', (key,))
        return r[0] if r else default

    def set(self, key, value):
        self.db.execute('INSERT INTO settings (key, value) VALUES (?, ?)'
                        ' ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
