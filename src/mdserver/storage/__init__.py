"""
Storage backend registry.

    db    = await open_database('mongodb://mongo:27017/blogmd')
    files = open_file_store('gridfs://', db)        # or 'file:///data/media'

Backends register themselves by URL scheme, so swapping databases is a config change:

    from mdserver.storage import register_backend
    register_backend(MyPostgresDatabase)            # handles postgresql://
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

from .base import (Conflict, Database, FileStore, MediaRepository, NotFound, PostRepository,
                   SettingsRepository, StorageError, UserRepository)

log = logging.getLogger(__name__)

_DATABASES: dict[str, type[Database]] = {}
_FILE_STORES: dict[str, type[FileStore]] = {}


def register_backend(cls: type[Database]) -> type[Database]:
    """Register a Database implementation for each of its `schemes`. Usable as a decorator."""
    for scheme in cls.schemes:
        _DATABASES[scheme] = cls
    return cls


def register_file_store(cls: type[FileStore]) -> type[FileStore]:
    for scheme in cls.schemes:
        _FILE_STORES[scheme] = cls
    return cls


def _scheme(url: str) -> str:
    return urlparse(url).scheme.lower()


async def open_database(url: str, migrate: bool = True, wait_seconds: float = 0) -> Database:
    """
    Open (and migrate) a database. With wait_seconds > 0, keep retrying while the server is
    still starting - e.g. the app container coming up before MongoDB in docker compose.
    """
    scheme = _scheme(url)
    if scheme not in _DATABASES:
        raise StorageError(f'No storage backend registered for "{scheme}://". '
                           f'Available: {", ".join(sorted(_DATABASES)) or "none"}')
    db = _DATABASES[scheme].from_url(url)
    if not migrate:
        return db
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            await db.migrate()
            return db
        except StorageError:
            raise
        except Exception as e:  # connection refused, no primary yet, ...
            if time.monotonic() >= deadline:
                raise StorageError(f'Database at {_redact(url)} is not reachable: {e}') from e
            log.warning('waiting for database (%s)...', type(e).__name__)
            await asyncio.sleep(2)


def _redact(url: str) -> str:
    p = urlparse(url)
    return url.replace(f':{p.password}@', ':***@') if p.password else url


def open_file_store(url: str, db: Database | None = None) -> FileStore:
    scheme = _scheme(url) or 'file'
    if scheme not in _FILE_STORES:
        raise StorageError(f'No file store registered for "{scheme}://".')
    return _FILE_STORES[scheme].from_url(url, db)


def available_backends() -> list[str]:
    return sorted(_DATABASES)


# Built-in backends.
from .sqlite import SQLiteDatabase           # noqa: E402
from .mongo import MongoDatabase             # noqa: E402
from .files import GridFSStore, LocalFileStore   # noqa: E402

register_backend(SQLiteDatabase)
register_backend(MongoDatabase)
register_file_store(LocalFileStore)
register_file_store(GridFSStore)

__all__ = [
    'Conflict', 'Database', 'FileStore', 'MediaRepository', 'NotFound', 'PostRepository',
    'SettingsRepository', 'StorageError', 'UserRepository', 'available_backends',
    'open_database', 'open_file_store', 'register_backend', 'register_file_store',
]
