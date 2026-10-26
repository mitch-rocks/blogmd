"""
Import the original file-based content (content/blog/**/*.md + content/author/*.json)
into whatever storage backend is configured. Safe to run repeatedly: existing paths
and usernames are skipped unless `overwrite=True`.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import frontmatter

from mdserver.domain import Post, PostStatus, Role, User, normalise_path
from mdserver.storage import Database

log = logging.getLogger(__name__)

_ORDINAL = re.compile(r'(\d+)(st|nd|rd|th)\b', re.I)
_DATE_FORMATS = ('%d %B, %Y', '%d %B %Y', '%d %b, %Y', '%d %b %Y', '%d/%m/%Y', '%Y-%m-%d',
                 '%Y-%m-%dT%H:%M:%S', '%B %d, %Y')
_LEADING_H1 = re.compile(r'\A\s*#\s+(.+?)\s*#*\s*\n')
_SETEXT_H1 = re.compile(r'\A\s*(.+?)\n=+\s*\n')


def parse_date(value) -> datetime | None:
    """'16th July, 2026', '11th June 2026', '09/07/2026', date objects... -> aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, 'year') and hasattr(value, 'month'):   # datetime.date
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = _ORDINAL.sub(r'\1', str(value)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _title_from_content(content: str) -> tuple[str | None, str]:
    """Pull a leading H1 out of the body to use as the title (the page header shows it)."""
    for pattern in (_LEADING_H1, _SETEXT_H1):
        m = pattern.match(content)
        if m:
            return m.group(1).strip(), content[m.end():].lstrip('\n')
    return None, content


def _path_for(md_file: pathlib.Path, base: pathlib.Path) -> str:
    rel = md_file.relative_to(base).with_suffix('')
    parts = list(rel.parts)
    if parts and parts[-1] == 'index':
        parts.pop()
    return normalise_path('/'.join(parts))


@dataclass
class ImportReport:
    users_created: list[str] = field(default_factory=list)
    posts_created: list[str] = field(default_factory=list)
    posts_updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def __str__(self):
        return (f'users created: {len(self.users_created)}, posts created: {len(self.posts_created)}, '
                f'posts updated: {len(self.posts_updated)}, skipped: {len(self.skipped)}')


def import_authors(db: Database, author_dir: pathlib.Path, report: ImportReport) -> None:
    if not author_dir.is_dir():
        return
    for f in sorted(author_dir.glob('*.json')):
        username = f.stem.lower()
        if db.users.get_by_username(username):
            continue
        data = json.loads(f.read_text(encoding='utf-8'))
        db.users.create(User(
            username=username,
            display_name=data.get('name', username),
            avatar_url=data.get('avatar', '') or '',
            bio=data.get('bio', '') or '',
            # the original authors had full filesystem access, so they become admins
            role=Role.SYSTEM if username == 'system' else Role.ADMIN,
        ))
        report.users_created.append(username)


def import_markdown_file(db: Database, md_file: pathlib.Path, path: str, report: ImportReport,
                         overwrite: bool = False, status: PostStatus = PostStatus.PUBLISHED) -> None:
    existing = db.posts.get_by_path(path)
    if existing and not overwrite:
        report.skipped.append(f'{path or "/"} (already exists)')
        return

    doc = frontmatter.load(md_file)
    meta, content = doc.metadata, doc.content
    title = meta.get('title')
    if not title:
        title, content = _title_from_content(content)
    title = str(title or (path.rsplit('/', 1)[-1].replace('-', ' ').title() if path else 'Home'))

    author = None
    if meta.get('author'):
        author = db.users.get_by_username(str(meta['author']).lower())

    mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc).replace(microsecond=0)
    created = parse_date(meta.get('created_utc') or meta.get('created')) or mtime
    updated = parse_date(meta.get('updated_utc') or meta.get('updated')) or created

    post = existing or Post(path=path, title=title)
    post.title = title
    post.description = str(meta.get('description', '') or '')
    post.content = content
    post.author_id = author.id if author else None
    post.status = PostStatus(meta.get('status', status))
    post.nav_order = int(meta.get('order', meta.get('nav_order', 0)) or 0)
    post.created_at = created
    post.updated_at = max(updated, created)
    post.published_at = created if post.status != PostStatus.DRAFT else None

    if existing:
        db.posts.update(post)
        report.posts_updated.append(path or '/')
    else:
        db.posts.create(post)
        report.posts_created.append(path or '/')


def import_content(db: Database, root: pathlib.Path, overwrite: bool = False) -> ImportReport:
    report = ImportReport()
    root = pathlib.Path(root)
    with db.transaction():
        import_authors(db, root / 'author', report)

        blog_dir = root / 'blog'
        if blog_dir.is_dir():
            files = sorted(blog_dir.rglob('*.md'))
            # folder/index.md takes precedence over folder.md (matches the original behaviour)
            chosen: dict[str, pathlib.Path] = {}
            for f in files:
                p = _path_for(f, blog_dir)
                if p in chosen and chosen[p].name == 'index.md':
                    report.skipped.append(f'{f.relative_to(root)} (shadowed by index.md)')
                    continue
                chosen[p] = f
            for p, f in chosen.items():
                import_markdown_file(db, f, p, report, overwrite)

        # error pages live under the hidden _system/ prefix
        err_dir = root / 'error'
        if err_dir.is_dir():
            for f in sorted(err_dir.glob('*.md')):
                import_markdown_file(db, f, f'_system/{f.stem}', report, overwrite,
                                     status=PostStatus.UNLISTED)
    log.info('content import: %s', report)
    return report
