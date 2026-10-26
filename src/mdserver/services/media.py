"""Upload handling: validation, hashing, metadata extraction, storage."""
from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import uuid
from typing import BinaryIO, Iterable

from mdserver.domain import Media, User, utcnow
from mdserver.storage import Database, FileStore

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 80_000_000      # decompression-bomb guard
except ImportError:  # Pillow is optional; dimensions are just left empty
    Image = None


# extension -> canonical MIME type. The client's Content-Type is never trusted.
ALLOWED_TYPES: dict[str, str] = {
    # images
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif',
    'webp': 'image/webp', 'avif': 'image/avif', 'svg': 'image/svg+xml',
    # video
    'mp4': 'video/mp4', 'm4v': 'video/mp4', 'webm': 'video/webm', 'mov': 'video/quicktime',
    'ogv': 'video/ogg',
    # audio
    'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'ogg': 'audio/ogg', 'oga': 'audio/ogg',
    'm4a': 'audio/mp4', 'flac': 'audio/flac',
    # documents / attachments
    'pdf': 'application/pdf', 'zip': 'application/zip', 'txt': 'text/plain',
    'md': 'text/markdown', 'csv': 'text/csv', 'json': 'application/json',
}

# magic numbers for formats where a mismatch would be dangerous or misleading
_MAGIC: dict[str, tuple[bytes, ...]] = {
    'image/png': (b'\x89PNG\r\n\x1a\n',),
    'image/jpeg': (b'\xff\xd8\xff',),
    'image/gif': (b'GIF87a', b'GIF89a'),
    'application/pdf': (b'%PDF-',),
    'application/zip': (b'PK\x03\x04', b'PK\x05\x06'),
}

# served with a sandboxing CSP: these can carry script
ACTIVE_CONTENT = {'image/svg+xml'}
# rendered inline by browsers; anything else is served as an attachment
INLINE_KINDS = {'image', 'video', 'audio'}
INLINE_TYPES = {'application/pdf', 'text/plain'}


class UploadError(ValueError):
    pass


def extension_of(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def _check_magic(mime: str, head: bytes) -> None:
    sigs = _MAGIC.get(mime)
    if sigs and not head.startswith(sigs):
        raise UploadError(f'File contents do not match its {mime} extension.')
    if mime == 'image/webp' and not (head[:4] == b'RIFF' and head[8:12] == b'WEBP'):
        raise UploadError('File contents do not match its image/webp extension.')
    if mime == 'image/svg+xml' and b'<svg' not in head.lower() and b'<?xml' not in head.lower():
        raise UploadError('File does not look like an SVG.')


def _image_size(fp: BinaryIO) -> tuple[int | None, int | None]:
    if Image is None:
        return None, None
    try:
        fp.seek(0)
        with Image.open(fp) as im:
            return im.size
    except Exception:
        return None, None
    finally:
        fp.seek(0)


def _inspect(stream: BinaryIO, filename: str, max_bytes: int):
    """
    CPU/disk-bound part of an upload (run in a worker thread): validate the type, copy to a
    temp file while hashing and size-checking, sniff magic bytes, read image dimensions.
    Returns (tmp_file, mime, ext, size, sha256, width, height); the caller closes tmp_file.
    """
    ext = extension_of(filename)
    mime = ALLOWED_TYPES.get(ext)
    if not mime:
        raise UploadError(f'.{ext or "?"} files are not allowed. '
                          f'Allowed: {", ".join(sorted(ALLOWED_TYPES))}')
    sha, size, head = hashlib.sha256(), 0, b''
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        while chunk := stream.read(1024 * 256):
            if not head:
                head = chunk[:512]
            size += len(chunk)
            if size > max_bytes:
                raise UploadError(f'File is larger than {max_bytes // (1024 * 1024)} MB.')
            sha.update(chunk)
            tmp.write(chunk)
        if size == 0:
            raise UploadError('File is empty.')
        _check_magic(mime, head)
        width = height = None
        if mime.startswith('image/') and mime != 'image/svg+xml':
            width, height = _image_size(tmp)
            if Image is not None and width is None:
                raise UploadError('Image could not be decoded.')
        tmp.seek(0)
        return tmp, mime, ext, size, sha.hexdigest(), width, height
    except BaseException:
        tmp.close()
        raise


async def save_upload(db: Database, files: FileStore, owner: User, stream: BinaryIO, filename: str,
                      max_bytes: int) -> tuple[Media, bool]:
    """
    Store an uploaded file and record its metadata.
    Returns (media, created) - created is False when an identical file by the
    same owner already existed and was reused.
    """
    filename = (filename or 'upload').replace('\\', '/').rsplit('/', 1)[-1].strip()[:200] or 'upload'
    tmp, mime, ext, size, digest, width, height = await asyncio.to_thread(_inspect, stream, filename, max_bytes)
    try:
        existing = await db.media.find_by_hash(owner.id, digest)
        if existing and await files.exists(existing.storage_key):
            return existing, False
        now = utcnow()
        key = f'{now:%Y/%m}/{uuid.uuid4().hex}.{ext}'
        await files.save(key, tmp)
    finally:
        tmp.close()

    try:
        media = await db.media.create(Media(
            owner_id=owner.id, original_name=filename, storage_key=key, mime_type=mime,
            size_bytes=size, sha256=digest, width=width, height=height, created_at=now,
        ))
    except BaseException:
        await files.delete(key)
        raise
    return media, True


async def delete_media(db: Database, files: FileStore, media: Media) -> None:
    await db.media.delete(media.id)
    await files.delete(media.storage_key)


_MEDIA_REF = re.compile(r'/media/(\d+)-[0-9a-f]{16}\b')


def referenced_media_ids(markdown_text: str) -> set[int]:
    return {int(m) for m in _MEDIA_REF.findall(markdown_text or '')}


async def sync_post_media(db: Database, post_id: int, markdown_text: str,
                          allowed: Iterable[int] | None = None) -> list[int]:
    """Record which media items a post embeds (the post_media link table)."""
    ids = referenced_media_ids(markdown_text)
    if allowed is not None:
        ids &= set(allowed)
    await db.posts.set_media(post_id, ids)
    return sorted(ids)
