"""
Abstract storage interfaces.

The application only talks to a `Database` (made of repositories) and a `FileStore`.
To add a new backend (PostgreSQL, MySQL, MongoDB, an HTTP API, ...):

    1. subclass `Database` and implement each repository ABC below
    2. register it:   register_backend('postgresql', PostgresDatabase)
    3. point DATABASE_URL at it:   DATABASE_URL=postgresql://user:pass@host/blogmd
    4. run the shared contract tests in tests/test_storage_contract.py against it

Repositories accept and return the dataclasses from `mdserver.domain` only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import BinaryIO, Iterable, Iterator

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
    def get(self, user_id: int) -> User | None: ...

    @abstractmethod
    def get_by_username(self, username: str) -> User | None: ...

    @abstractmethod
    def list(self) -> list[User]: ...

    @abstractmethod
    def create(self, user: User) -> User:
        """Persist a new user and return it with `id` set. Raises Conflict on duplicate username."""

    @abstractmethod
    def update(self, user: User) -> User: ...

    @abstractmethod
    def delete(self, user_id: int) -> None: ...

    @abstractmethod
    def count(self) -> int: ...


class PostRepository(ABC):
    @abstractmethod
    def get(self, post_id: int) -> Post | None: ...

    @abstractmethod
    def get_by_path(self, path: str) -> Post | None: ...

    @abstractmethod
    def list(self, *, statuses: Iterable[PostStatus] | None = None,
             author_id: int | None = None, prefix: str | None = None,
             search: str | None = None) -> list[Post]:
        """
        List posts ordered by path. Filters combine with AND:
          statuses  - only these statuses
          author_id - only this author's posts
          prefix    - only posts at or below this folder path ('' = all)
          search    - case-insensitive match on title/path/description
        """

    @abstractmethod
    def create(self, post: Post) -> Post:
        """Persist a new post. Raises Conflict if the path is taken."""

    @abstractmethod
    def update(self, post: Post) -> Post:
        """Persist changes. Raises Conflict if the new path is taken, NotFound if missing."""

    @abstractmethod
    def delete(self, post_id: int) -> None: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def set_media(self, post_id: int, media_ids: Iterable[int]) -> None:
        """Replace the set of media items referenced by a post."""

    @abstractmethod
    def media_ids(self, post_id: int) -> list[int]: ...


class MediaRepository(ABC):
    @abstractmethod
    def get(self, media_id: int) -> Media | None: ...

    @abstractmethod
    def find_by_hash(self, owner_id: int, sha256: str) -> Media | None:
        """Used to de-duplicate re-uploads of identical files by the same user."""

    @abstractmethod
    def list(self, *, owner_id: int | None = None, kind: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Media]:
        """Newest first. `kind` is the MIME major type: image, video, audio, application..."""

    @abstractmethod
    def create(self, media: Media) -> Media: ...

    @abstractmethod
    def update(self, media: Media) -> Media: ...

    @abstractmethod
    def delete(self, media_id: int) -> None: ...

    @abstractmethod
    def usage(self, media_id: int) -> list[int]:
        """IDs of posts that reference this media item."""


class SettingsRepository(ABC):
    """Small key/value store for instance-level settings (e.g. the generated secret key)."""

    @abstractmethod
    def get(self, key: str, default: str | None = None) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...


# --------------------------------------------------------------------------- database

class Database(ABC):
    """A storage backend: a bundle of repositories plus lifecycle hooks."""

    #: URL scheme(s) handled by this backend, e.g. ('sqlite',)
    schemes: tuple[str, ...] = ()

    users: UserRepository
    posts: PostRepository
    media: MediaRepository
    settings: SettingsRepository

    @classmethod
    @abstractmethod
    def from_url(cls, url: str) -> 'Database': ...

    @abstractmethod
    def migrate(self) -> None:
        """Create or upgrade the schema. Must be idempotent."""

    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several repository calls into one atomic unit (nestable)."""

    @abstractmethod
    def close(self) -> None: ...


# --------------------------------------------------------------------------- file storage

class FileStore(ABC):
    """Where uploaded media bytes live. Metadata lives in the Database."""

    schemes: tuple[str, ...] = ()

    @classmethod
    @abstractmethod
    def from_url(cls, url: str) -> 'FileStore': ...

    @abstractmethod
    def save(self, key: str, stream: BinaryIO) -> None: ...

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open for reading. Raises NotFound."""

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def local_path(self, key: str):
        """Filesystem path if the store is local (lets Flask use sendfile), else None."""
        return None
