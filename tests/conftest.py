import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from mdserver.storage import open_database, open_file_store  # noqa: E402

# Every backend listed here runs the full storage contract suite.
# To test a new backend, add its URL factory, e.g.
#   'postgres': lambda tmp: os.environ['TEST_POSTGRES_URL'],
BACKENDS = {
    'sqlite-file': lambda tmp: f'sqlite:///{(tmp / "test.db").as_posix()}',
    'sqlite-memory': lambda tmp: 'sqlite:///:memory:',
}


@pytest.fixture(params=sorted(BACKENDS))
def db(request, tmp_path):
    database = open_database(BACKENDS[request.param](tmp_path))
    yield database
    database.close()


@pytest.fixture
def files(tmp_path):
    return open_file_store(f'file://{(tmp_path / "media").as_posix()}')


@pytest.fixture
def content_root():
    return ROOT / 'content'
