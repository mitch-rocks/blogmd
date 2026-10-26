"""Admin account bootstrap used by Docker deployments (ADMIN_* environment variables)."""
import pytest

from mdserver.domain import Role, User
from mdserver.services.auth import hash_password, verify_password
from mdserver.services.bootstrap import BootstrapError, ensure_admin


async def test_generates_admin_when_nobody_can_sign_in(db, caplog):
    r = await ensure_admin(db, env={})
    u = await db.users.get_by_username('admin')
    assert r.action == 'created' and u.role == Role.ADMIN
    assert r.generated_password and verify_password(u, r.generated_password)
    assert r.generated_password in caplog.text          # printed once to the logs
    # second start: nothing changes, nothing printed
    caplog.clear()
    again = await ensure_admin(db, env={})
    assert again.action == 'none' and again.generated_password is None
    assert verify_password(await db.users.get_by_username('admin'), r.generated_password)


async def test_configured_password_and_display_name(db):
    r = await ensure_admin(db, env={'ADMIN_USERNAME': 'Boss', 'ADMIN_PASSWORD': 'a-long-password',
                              'ADMIN_DISPLAY_NAME': 'The Boss'})
    u = await db.users.get_by_username('boss')
    assert r.action == 'created' and r.generated_password is None
    assert u.display_name == 'The Boss' and verify_password(u, 'a-long-password')


async def test_password_file(db, tmp_path):
    secret = tmp_path / 'pw'
    secret.write_text('from-a-docker-secret\n')
    await ensure_admin(db, env={'ADMIN_USERNAME': 'mitch', 'ADMIN_PASSWORD_FILE': str(secret)})
    assert verify_password(await db.users.get_by_username('mitch'), 'from-a-docker-secret')


async def test_imported_author_without_password_gets_one(db):
    await db.users.create(User(username='mitch', display_name='Mitch Naake', role=Role.ADMIN))
    r = await ensure_admin(db, env={'ADMIN_USERNAME': 'mitch', 'ADMIN_PASSWORD': 'mitch-password-1'})
    assert r.action == 'password-set'
    assert verify_password(await db.users.get_by_username('mitch'), 'mitch-password-1')


async def test_existing_password_is_kept_unless_reset(db):
    await db.users.create(User(username='mitch', display_name='M', role=Role.ADMIN,
                         password_hash=hash_password('changed-in-the-ui')))
    env = {'ADMIN_USERNAME': 'mitch', 'ADMIN_PASSWORD': 'password-from-env'}
    assert (await ensure_admin(db, env=env)).action == 'none'
    assert verify_password(await db.users.get_by_username('mitch'), 'changed-in-the-ui')
    await ensure_admin(db, env={**env, 'ADMIN_RESET_PASSWORD': 'true'})
    assert verify_password(await db.users.get_by_username('mitch'), 'password-from-env')


async def test_named_writer_is_promoted(db):
    await db.users.create(User(username='wren', display_name='W', role=Role.WRITER,
                         password_hash=hash_password('writer-password')))
    r = await ensure_admin(db, env={'ADMIN_USERNAME': 'wren'})
    assert r.action == 'promoted' and (await db.users.get_by_username('wren')).role == Role.ADMIN


async def test_no_surprise_account_when_an_admin_exists(db):
    await db.users.create(User(username='mitch', display_name='M', role=Role.ADMIN,
                         password_hash=hash_password('already-set-up')))
    r = await ensure_admin(db, env={})
    assert r.action == 'none' and await db.users.get_by_username('admin') is None


@pytest.mark.parametrize('env, message', [
    ({'ADMIN_PASSWORD': 'short'}, 'at least'),
    ({'ADMIN_USERNAME': 'bad name!'}, 'ADMIN_USERNAME'),
    ({'ADMIN_PASSWORD_FILE': '/nonexistent/secret'}, 'could not be read'),
])
async def test_bad_configuration_fails_loudly(db, env, message):
    with pytest.raises(BootstrapError, match=message):
        await ensure_admin(db, env=env)


async def test_system_account_cannot_be_admin(db):
    await db.users.create(User(username='system', display_name='System', role=Role.SYSTEM))
    with pytest.raises(BootstrapError, match='system account'):
        await ensure_admin(db, env={'ADMIN_USERNAME': 'system', 'ADMIN_PASSWORD': 'whatever-long'})
