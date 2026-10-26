"""
Storage backend registry.

    db    = open_database('sqlite:///content/blogmd.db')
    files = open_file_store('file://content/media')

Backends register themselves by URL scheme, so swapping databases is a config change:

    from mdserver.storage import register_backend
    register_backend(MyPostgresDatabase)       # handles postgresql://
"""
from __future__ import annotations

from urllib.parse import urlparse

from .base import (Conflict, Database, FileStore, MediaRepository, NotFound,
                   PostRepository, SettingsRepository, StorageError, UserRepository)

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
    return urlparse(url).scheme.split('+', 1)[0].lower()


def open_database(url: str, migrate: bool = True) -> Database:
    scheme = _scheme(url)
    if scheme not in _DATABASES:
        raise StorageError(f'No storage backend registered for "{scheme}://". '
                           f'Available: {", ".join(sorted(_DATABASES)) or "none"}')
    db = _DATABASES[scheme].from_url(url)
    if migrate:
        db.migrate()
    return db


def open_file_store(url: str) -> FileStore:
    scheme = _scheme(url) or 'file'
    if scheme not in _FILE_STORES:
        raise StorageError(f'No file store registered for "{scheme}://".')
    return _FILE_STORES[scheme].from_url(url)


def available_backends() -> list[str]:
    return sorted(_DATABASES)


# Built-in backends.
from .sqlite import SQLiteDatabase  # noqa: E402
from .files import LocalFileStore   # noqa: E402

register_backend(SQLiteDatabase)
register_file_store(LocalFileStore)

__all__ = [
    'Conflict', 'Database', 'FileStore', 'MediaRepository', 'NotFound', 'PostRepository',
    'SettingsRepository', 'StorageError', 'UserRepository', 'available_backends',
    'open_database', 'open_file_store', 'register_backend', 'register_file_store',
]
