"""
Backend-agnostic domain objects.

Nothing in here knows about SQL, files or HTTP. Storage backends translate to and
from these dataclasses, and the web layer only ever sees these types.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Role(str, Enum):
    ADMIN = 'admin'     # can edit every post, raw HTML trusted
    WRITER = 'writer'   # can edit own posts, HTML sanitised
    SYSTEM = 'system'   # non-login account used for built-in pages


class PostStatus(str, Enum):
    DRAFT = 'draft'          # only visible in the writers area
    PUBLISHED = 'published'  # public and listed in the navigation
    UNLISTED = 'unlisted'    # public by URL, hidden from the navigation


@dataclass
class User:
    username: str
    display_name: str
    role: Role = Role.WRITER
    avatar_url: str = ''
    bio: str = ''
    password_hash: str | None = None   # None = cannot sign in
    created_at: datetime = field(default_factory=utcnow)
    id: int | None = None

    @property
    def can_login(self) -> bool:
        return bool(self.password_hash) and self.role != Role.SYSTEM

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def trusted_html(self) -> bool:
        """Raw HTML in this user's markdown is rendered unsanitised."""
        return self.role in (Role.ADMIN, Role.SYSTEM)


@dataclass
class Post:
    path: str                       # URL path below /blog/, '' is the blog root
    title: str
    content: str = ''               # markdown body (no frontmatter)
    author_id: int | None = None
    description: str = ''
    status: PostStatus = PostStatus.DRAFT
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    published_at: datetime | None = None
    nav_order: int = 0              # lower sorts first within a folder
    id: int | None = None

    @property
    def url(self) -> str:
        return f'/blog/{self.path}/' if self.path else '/blog/'

    @property
    def is_public(self) -> bool:
        return self.status in (PostStatus.PUBLISHED, PostStatus.UNLISTED)


@dataclass
class Media:
    owner_id: int
    original_name: str
    storage_key: str                # opaque key understood by the FileStore
    mime_type: str
    size_bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    alt_text: str = ''
    created_at: datetime = field(default_factory=utcnow)
    id: int | None = None

    @property
    def kind(self) -> str:
        return self.mime_type.split('/', 1)[0] if '/' in self.mime_type else 'file'

    @property
    def public_key(self) -> str:
        # id + part of the content hash: stable, but not enumerable by counting ids
        return f'{self.id}-{self.sha256[:16]}'

    @property
    def url(self) -> str:
        return f'/media/{self.public_key}/{safe_filename(self.original_name)}'

    def markdown(self) -> str:
        """Markdown/HTML snippet that embeds this media item in a post."""
        name = self.alt_text or self.original_name.rsplit('.', 1)[0]
        name = name.replace('[', '').replace(']', '')
        if self.kind == 'image':
            return f'![{name}]({self.url})'
        if self.kind == 'video':
            return f'<video controls preload="metadata" src="{self.url}"></video>'
        if self.kind == 'audio':
            return f'<audio controls preload="metadata" src="{self.url}"></audio>'
        return f'[{self.original_name}]({self.url})'


# --------------------------------------------------------------------------- helpers

_PATH_SEGMENT = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]*$')
_USERNAME = re.compile(r'^[a-z0-9_][a-z0-9_.-]{1,31}$')


def normalise_path(path: str) -> str:
    """Canonical post path: no leading/trailing slashes, no empty segments."""
    return '/'.join(p for p in (path or '').strip().split('/') if p)


def validate_path(path: str) -> str | None:
    """Return an error message, or None if the path is acceptable."""
    path = normalise_path(path)
    if len(path) > 200:
        return 'Path is too long (200 characters max).'
    for seg in path.split('/') if path else []:
        if not _PATH_SEGMENT.match(seg) or seg in ('.', '..'):
            return f'Invalid path segment "{seg}". Use letters, numbers, ".", "-" and "_".'
    return None


def validate_username(username: str) -> str | None:
    if not _USERNAME.match(username or ''):
        return 'Usernames are 2-32 chars: lowercase letters, numbers, ".", "-", "_".'
    return None


def slugify(text: str) -> str:
    text = re.sub(r'[^A-Za-z0-9]+', '-', (text or '').lower()).strip('-')
    return text[:80] or 'untitled'


def safe_filename(name: str) -> str:
    base = re.sub(r'[^A-Za-z0-9._-]+', '-', name or '').strip('.-')
    return base[:120] or 'file'
