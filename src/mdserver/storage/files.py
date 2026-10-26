"""
File stores for uploaded media bytes.

    file:///data/media     LocalFileStore   - a directory (bind mount / volume)
    gridfs://              GridFSStore      - inside the MongoDB database itself (bucket "media")
    gridfs://uploads       GridFSStore      - same, custom bucket name
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile
from typing import BinaryIO
from urllib.parse import urlparse

from .base import Database, FileStore, NotFound, StorageError


class LocalFileStore(FileStore):
    schemes = ('file',)

    def __init__(self, root: str | os.PathLike):
        self.root = pathlib.Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_url(cls, url: str, db: Database | None = None) -> 'LocalFileStore':
        parsed = urlparse(url)
        # file://content/media -> netloc 'content' + path '/media' (relative); file:///srv/media -> absolute
        path = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
        return cls(path or 'media')

    def _resolve(self, key: str) -> pathlib.Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError(f'Refusing key outside the store root: {key!r}')
        return path

    def _save_sync(self, key: str, stream: BinaryIO) -> None:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix='.upload-')   # atomic: write then rename
        try:
            with os.fdopen(fd, 'wb') as out:
                shutil.copyfileobj(stream, out, 1024 * 256)
            os.replace(tmp, dest)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise

    async def save(self, key: str, stream: BinaryIO) -> None:
        await asyncio.to_thread(self._save_sync, key, stream)

    async def iter_range(self, key, start=0, end=None, chunk_size=256 * 1024):
        path = self._resolve(key)
        try:
            fh = await asyncio.to_thread(open, path, 'rb')
        except FileNotFoundError as e:
            raise NotFound(key) from e
        try:
            await asyncio.to_thread(fh.seek, start)
            remaining = None if end is None else end - start + 1
            while remaining is None or remaining > 0:
                n = chunk_size if remaining is None else min(chunk_size, remaining)
                chunk = await asyncio.to_thread(fh.read, n)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(fh.close)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._resolve(key).unlink, missing_ok=True)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._resolve(key).is_file)

    def local_path(self, key: str):
        return self._resolve(key)


class GridFSStore(FileStore):
    """Stores media in MongoDB GridFS, so the app container needs no volume of its own."""
    schemes = ('gridfs',)

    def __init__(self, db: Database, bucket: str = 'media'):
        from gridfs import AsyncGridFSBucket
        from .mongo import MongoDatabase
        if not isinstance(db, MongoDatabase):
            raise StorageError('gridfs:// media storage requires a MongoDB DATABASE_URL.')
        self.bucket = AsyncGridFSBucket(db.d, bucket_name=bucket, chunk_size_bytes=255 * 1024)
        self._files = db.d[f'{bucket}.files']

    @classmethod
    def from_url(cls, url: str, db: Database | None = None) -> 'GridFSStore':
        if db is None:
            raise StorageError('gridfs:// needs the database to be opened first.')
        return cls(db, urlparse(url).netloc or 'media')

    async def _file_id(self, key: str):
        d = await self._files.find_one({'filename': key}, {'_id': 1}, sort=[('uploadDate', -1)])
        if d is None:
            raise NotFound(key)
        return d['_id']

    async def save(self, key: str, stream: BinaryIO) -> None:
        await asyncio.to_thread(stream.seek, 0)
        upload = self.bucket.open_upload_stream(key)
        try:
            while chunk := await asyncio.to_thread(stream.read, 255 * 1024):
                await upload.write(chunk)
        except BaseException:
            await upload.abort()
            raise
        await upload.close()

    async def iter_range(self, key, start=0, end=None, chunk_size=256 * 1024):
        grid_out = await self.bucket.open_download_stream(await self._file_id(key))
        try:
            await grid_out.seek(start)
            remaining = None if end is None else end - start + 1
            while remaining is None or remaining > 0:
                n = chunk_size if remaining is None else min(chunk_size, remaining)
                chunk = await grid_out.read(n)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
        finally:
            await grid_out.close()

    async def delete(self, key: str) -> None:
        async for d in self._files.find({'filename': key}, {'_id': 1}):
            await self.bucket.delete(d['_id'])

    async def exists(self, key: str) -> bool:
        return await self._files.count_documents({'filename': key}, limit=1) > 0
