import os
import pathlib
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from mdserver.storage import open_database, open_file_store  # noqa: E402

# MongoDB used by the tests (each test gets its own throwaway database). Standalone works;
# a replica set (mongod --replSet rs0 + rs.initiate()) also runs the rollback tests.
MONGO_URL = os.environ.get('TEST_MONGO_URL', 'mongodb://127.0.0.1:27017/?directConnection=true')


def _mongo_available() -> bool:
    try:
        import pymongo
        pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=800).admin.command('ping')
        return True
    except Exception:
        return False


MONGO_OK = _mongo_available()
needs_mongo = pytest.mark.skipif(not MONGO_OK, reason=f'no MongoDB at {MONGO_URL}')


def mongo_url(name: str) -> str:
    base, _, query = MONGO_URL.partition('?')
    return f'{base.rstrip("/")}/{name}' + (f'?{query}' if query else '')


# Every backend listed here runs the full storage contract suite (tests/test_storage_contract.py).
# To test a new backend, add a factory returning its URL.
BACKENDS = {
    'sqlite-file': lambda tmp: f'sqlite:///{(tmp / "test.db").as_posix()}',
    'sqlite-memory': lambda tmp: 'sqlite:///:memory:',
    'mongodb': lambda tmp: mongo_url(f'blogmd_test_{uuid.uuid4().hex[:10]}'),
}


async def _drop(db):
    if hasattr(db, 'drop'):
        await db.drop()


@pytest.fixture(params=sorted(BACKENDS))
async def db(request, tmp_path):
    if request.param == 'mongodb' and not MONGO_OK:
        pytest.skip(f'no MongoDB at {MONGO_URL}')
    database = await open_database(BACKENDS[request.param](tmp_path))
    yield database
    await _drop(database)
    await database.close()


@pytest.fixture
def files(tmp_path):
    return open_file_store(f'file://{(tmp_path / "media").as_posix()}')


@pytest.fixture
def content_root():
    return ROOT / 'content'
