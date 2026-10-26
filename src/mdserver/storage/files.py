"""Local-disk FileStore (file:// URLs). Swap for an S3/R2 store by implementing FileStore."""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
from typing import BinaryIO
from urllib.parse import urlparse

from .base import FileStore, NotFound, StorageError


class LocalFileStore(FileStore):
    schemes = ('file',)

    def __init__(self, root: str | os.PathLike):
        self.root = pathlib.Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_url(cls, url: str) -> 'LocalFileStore':
        parsed = urlparse(url)
        # file://content/media  -> netloc 'content', path '/media' (relative)
        # file:///srv/media     -> netloc '',        path '/srv/media' (absolute)
        path = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
        return cls(path or 'media')

    def _resolve(self, key: str) -> pathlib.Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError(f'Refusing key outside the store root: {key!r}')
        return path

    def save(self, key: str, stream: BinaryIO) -> None:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp file then rename, so readers never see a partial file
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix='.upload-')
        try:
            with os.fdopen(fd, 'wb') as out:
                shutil.copyfileobj(stream, out, 1024 * 256)
            os.replace(tmp, dest)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise

    def open(self, key: str) -> BinaryIO:
        try:
            return self._resolve(key).open('rb')
        except FileNotFoundError as e:
            raise NotFound(key) from e

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def local_path(self, key: str):
        return self._resolve(key)
