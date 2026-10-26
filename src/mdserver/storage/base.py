"""
Abstract (async) storage interfaces.

The application only talks to a `Database` (made of repositories) and a `FileStore`.
To add a new backend (PostgreSQL, DynamoDB, an HTTP API, ...):

    1. subclass `Database` and implement each repository ABC below
    2. register it:   register_backend(PostgresDatabase)      # handles e.g. postgresql://
    3. point DATABASE_URL at it
    4. add it to BACKENDS in tests/conftest.py - tests/test_storage_contract.py must pass

Repositories accept and return the dataclasses from `mdserver.domain` only, and every
method is a coroutine so I/O-bound backends never block the event loop.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import AsyncIterator, BinaryIO, Iterable

from mdserver.domain import Media, Post, PostStatus, User


class StorageError(Exception):
    """Base error raised by storage backends."""


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    """A uniqueness constraint was violated (e.g. duplicate username or post path)."""


# --------------------------------------------------------------------------- repositories

class UserRepository(ABC):
    @abstractmethod
    async def get(self, user_id: int) -> User | None: ...

    @abstractmethod
    async def get_by_username(self, username: str) -> User | None:
        """Case-insensitive."""

    @abstractmethod
    async def list(self) -> list[User]:
        """Ordered by username (case-insensitive)."""

    @abstractmethod
    async def create(self, user: User) -> User:
        """Persist a new user and return it with `id` set. Raises Conflict on duplicate username."""

    @abstractmethod
    async def update(self, user: User) -> User: ...

    @abstractmethod
    async def delete(self, user_id: int) -> None:
        """Their posts keep existing with author_id=None; their media records are removed."""

    @abstractmethod
    async def count(self) -> int: ...


class PostRepository(ABC):
    @abstractmethod
    async def get(self, post_id: int) -> Post | None: ...

    @abstractmethod
    async def get_by_path(self, path: str) -> Post | None: ...

    @abstractmethod
    async def list(self, *, statuses: Iterable[PostStatus] | None = None,
                   author_id: int | None = None, prefix: str | None = None,
                   search: str | None = None) -> list[Post]:
        """
        List posts ordered by path (binary order). Filters combine with AND:
          statuses  - only these statuses (an empty list matches nothing)
          author_id - only this author's posts
          prefix    - only posts at or below this folder path
          search    - case-insensitive substring match on title/path/description
        """

    @abstractmethod
    async def create(self, post: Post) -> Post:
        """Persist a new post. Raises Conflict if the path is taken."""

    @abstractmethod
    async def update(self, post: Post) -> Post:
        """Persist changes. Raises Conflict if the new path is taken, NotFound if missing."""

    @abstractmethod
    async def delete(self, post_id: int) -> None: ...

    @abstractmethod
    async def count(self) -> int: ...

    @abstractmethod
    async def set_media(self, post_id: int, media_ids: Iterable[int]) -> None:
        """Replace the set of media items a post embeds (unknown ids are ignored)."""

    @abstractmethod
    async def media_ids(self, post_id: int) -> list[int]:
        """Sorted."""


class MediaRepository(ABC):
    @abstractmethod
    async def get(self, media_id: int) -> Media | None: ...

    @abstractmethod
    async def find_by_hash(self, owner_id: int, sha256: str) -> Media | None:
        """Used to de-duplicate re-uploads of identical files by the same user."""

    @abstractmethod
    async def list(self, *, owner_id: int | None = None, kind: str | None = None,
                   limit: int = 100, offset: int = 0) -> list[Media]:
        """Newest first. `kind` is the MIME major type: image, video, audio, application..."""

    @abstractmethod
    async def create(self, media: Media) -> Media: ...

    @abstractmethod
    async def update(self, media: Media) -> Media: ...

    @abstractmethod
    async def delete(self, media_id: int) -> None:
        """Also removes it from every post's media links."""

    @abstractmethod
    async def usage(self, media_id: int) -> list[int]:
        """Sorted IDs of posts that embed this media item."""


class SettingsRepository(ABC):
    """Small key/value store for instance-level settings (e.g. the generated secret key)."""

    @abstractmethod
    async def get(self, key: str, default: str | None = None) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str) -> None: ...


# --------------------------------------------------------------------------- database

class Database(ABC):
    """A storage backend: a bundle of repositories plus lifecycle hooks."""

    #: URL scheme(s) handled by this backend, e.g. ('mongodb', 'mongodb+srv')
    schemes: tuple[str, ...] = ()

    users: UserRepository
    posts: PostRepository
    media: MediaRepository
    settings: SettingsRepository

    @classmethod
    @abstractmethod
    def from_url(cls, url: str) -> 'Database':
        """Construct without doing I/O (connect lazily / in migrate)."""

    @abstractmethod
    async def migrate(self) -> None:
        """Create or upgrade schema / indexes. Must be idempotent."""

    @abstractmethod
    async def ping(self) -> None:
        """Raise if the backend is unreachable (used by /healthz)."""

    @abstractmethod
    def transaction(self) -> AbstractAsyncContextManager[None]:
        """
        Group repository calls atomically: if an exception escapes the outermost block,
        none of its writes persist. Nested blocks join the enclosing transaction.
        """

    @abstractmethod
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- file storage

class FileStore(ABC):
    """Where uploaded media bytes live. Metadata lives in the Database."""

    schemes: tuple[str, ...] = ()

    @classmethod
    @abstractmethod
    def from_url(cls, url: str, db: Database | None = None) -> 'FileStore': ...

    @abstractmethod
    async def save(self, key: str, stream: BinaryIO) -> None:
        """Store the contents of a (sync) binary file object under `key`."""

    @abstractmethod
    def iter_range(self, key: str, start: int = 0, end: int | None = None,
                   chunk_size: int = 256 * 1024) -> AsyncIterator[bytes]:
        """Async iterator over bytes [start, end] inclusive. Raises NotFound."""

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    def local_path(self, key: str):
        """Filesystem path if the store is local (served with sendfile), else None."""
        return None
