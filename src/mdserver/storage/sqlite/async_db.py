"""
Async SQLite backend: runs the synchronous engine on one dedicated thread.

SQLite allows one writer at a time anyway, so a single thread costs nothing in throughput,
keeps a single connection, and makes transactions straightforward: an asyncio lock is held
for the whole outermost transaction so other tasks' statements can't interleave with it.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from mdserver.storage.base import (Database, MediaRepository, PostRepository, SettingsRepository,
                                   UserRepository)
from .database import SyncSQLiteDatabase


class _Proxy:
    """Exposes every method of a sync repository as a coroutine run on the DB thread."""

    def __init__(self, db: 'SQLiteDatabase', target):
        self._db, self._target = db, target

    def __getattr__(self, name):
        fn = getattr(self._target, name)
        if not callable(fn):
            return fn

        async def call(*args, **kwargs):
            return await self._db._run(fn, *args, **kwargs)
        call.__name__ = name
        return call


class _Users(_Proxy): pass
class _Posts(_Proxy): pass
class _Media(_Proxy): pass
class _Settings(_Proxy): pass


# virtual subclasses: isinstance() checks pass, while every method resolves via __getattr__
UserRepository.register(_Users)
PostRepository.register(_Posts)
MediaRepository.register(_Media)
SettingsRepository.register(_Settings)


class SQLiteDatabase(Database):
    schemes = ('sqlite',)

    def __init__(self, path: str):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sqlite')
        # build the engine on its own thread so its thread-local connection lives there
        self._sync: SyncSQLiteDatabase = self._executor.submit(SyncSQLiteDatabase, path).result()
        self._lock: asyncio.Lock | None = None
        self._in_tx = contextvars.ContextVar(f'sqlite_tx_{id(self)}', default=False)
        self.users = _Users(self, self._sync.users)
        self.posts = _Posts(self, self._sync.posts)
        self.media = _Media(self, self._sync.media)
        self.settings = _Settings(self, self._sync.settings)

    @classmethod
    def from_url(cls, url: str) -> 'SQLiteDatabase':
        if not url.startswith('sqlite://'):
            raise ValueError(f'Not a sqlite URL: {url}')
        rest = url[len('sqlite://'):]
        return cls(rest[1:] if rest.startswith('/') else rest)

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _call(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    async def _run(self, fn, *args, **kwargs):
        if self._in_tx.get():                     # already inside our transaction: lock held
            return await self._call(fn, *args, **kwargs)
        async with self._get_lock():
            return await self._call(fn, *args, **kwargs)

    @asynccontextmanager
    async def transaction(self):
        if self._in_tx.get():                     # nested: join the outer transaction
            yield
            return
        async with self._get_lock():
            cm = self._sync.transaction()
            await self._call(cm.__enter__)
            token = self._in_tx.set(True)
            try:
                yield
            except BaseException as e:
                await self._call(cm.__exit__, type(e), e, e.__traceback__)
                raise
            else:
                await self._call(cm.__exit__, None, None, None)
            finally:
                self._in_tx.reset(token)

    supports_transactions = True

    async def migrate(self) -> None:
        await self._run(self._sync.migrate)

    async def ping(self) -> None:
        await self._run(lambda: self._sync.conn.execute('SELECT 1').fetchone())

    @property
    def schema_version(self) -> int:
        return self._executor.submit(lambda: self._sync.schema_version).result()

    async def close(self) -> None:
        await self._call(self._sync.close)
        self._executor.shutdown(wait=False)
