import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.editor_portal import EditorModule, EditorSnapshot
from common.exceptions import AuthError, NetworkError
from services.auth_service import editor_graphql_policy as policy
from services.infoboards_service.editor_scope import EditorScope
from services.infoboards_service.editor_service import EditorGroup, _group_title, _WEBMASTER
from services.users_service.html_parser import normalize_title


def cookie(uid, password):
    return base64.b64encode(f'{uid}:{password}'.encode()).decode()


@pytest.fixture
def environment(monkeypatch):
    settings = SimpleNamespace(ADMIN_LOGIN='service', ADMIN_PASSWORD='secret',
                               EDITOR_PORTAL_DB_URL=SimpleNamespace(get_secret_value=lambda: 'private-dsn'))
    state = SimpleNamespace(fields={'uid': 'alice', 'psw': 'password'}, is_empty_form=False, groups=[77])
    groups = {_WEBMASTER: 1, _group_title('123', 'B'): 77}
    catalog = SimpleNamespace(get_user_page=AsyncMock(return_value='user-form'),
                              get_groups_page=AsyncMock(return_value='groups-form'))
    login = AsyncMock(return_value={'Auth': 'administrator-cookie'})
    monkeypatch.setattr(policy, 'CatalogClient', lambda http: catalog)
    monkeypatch.setattr(policy, 'AuthService', lambda db, settings: SimpleNamespace(login=login))
    monkeypatch.setattr(policy, 'parse_user_state', lambda page: state)
    monkeypatch.setattr(policy, 'parse_groups_catalog', lambda page: groups)
    monkeypatch.setattr(policy, 'read_editor_snapshot', AsyncMock(return_value=EditorSnapshot((
        EditorModule('https://catalog/docs/?infoboard=ROOT', 'alice'),
    ))))
    scopes = AsyncMock(return_value={'ROOT': EditorScope('B', frozenset({'B', 'CHILD'}))})
    monkeypatch.setattr(policy, 'resolve_editor_scopes', scopes)
    bindings = {'B': EditorGroup('B', 'ROOT', 77, ('B', 'CHILD'), ('alice',))}
    monkeypatch.setattr(policy, 'EditorGroupRepository', lambda: SimpleNamespace(get=AsyncMock(return_value=bindings)))
    fetch = AsyncMock(return_value=SimpleNamespace(id='CHILD', settings={'boardUuid': 'board-uuid'}))
    monkeypatch.setattr(policy, 'fetch_board', fetch)
    return SimpleNamespace(settings=settings, state=state, groups=groups, bindings=bindings,
                           catalog=catalog, login=login, scopes=scopes, fetch=fetch)


async def authorize(env, *, uid='alice', password='password', action='editConfig',
                    board='CHILD', uuid=None, auth=None):
    await policy.authorize_graphql_write('123', 'https://catalog', action, board, uuid,
        cookie(uid, password) if auth is None else auth, env.settings, object(), object())


@pytest.mark.asyncio
async def test_service_admin_bypasses_all_native_io(environment):
    await authorize(environment, uid='service', password='secret', action='deleteConfig')
    environment.login.assert_not_awaited()
    environment.scopes.assert_not_awaited()


@pytest.mark.parametrize('invalid', ['', 'YTp=', '!!!', base64.b64encode(b'no-colon').decode(),
                                    cookie('alice', ''), cookie('alice', 'bad\nvalue')])
def test_cookie_must_be_valid_canonical_and_nonempty(environment, invalid):
    assert not policy.is_service_admin(invalid, environment.settings)
    assert policy._credentials(invalid) is None


@pytest.mark.asyncio
async def test_editor_authorized_only_after_current_portal_scope_binding_and_membership(environment):
    await authorize(environment, uuid='board-uuid')
    environment.scopes.assert_awaited_once()
    environment.fetch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['password', 'uid', 'missing_user', 'wrong_board', 'wrong_uuid',
                                     'no_membership', 'no_binding', 'unconfirmed_board', 'untracked_user', 'changed_group',
                                     'delete', 'create'])
async def test_editor_denied_on_missing_identity_or_authority(environment, change):
    h = environment
    args = {}
    if change == 'password': args['password'] = 'wrong'
    if change == 'uid': h.state.fields['uid'] = 'someone-else'
    if change == 'missing_user': h.catalog.get_user_page.return_value = None
    if change == 'wrong_board': args['board'] = 'OTHER'
    if change == 'wrong_uuid': args['uuid'] = 'wrong'
    if change == 'no_membership': h.state.groups = [78]
    if change == 'no_binding': h.bindings.clear()
    if change == 'unconfirmed_board': h.bindings['B'] = EditorGroup('B', 'ROOT', 77, ('B',), ('alice',))
    if change == 'untracked_user': h.bindings['B'] = EditorGroup('B', 'ROOT', 77, ('B', 'CHILD'), ())
    if change == 'changed_group': h.groups[_group_title('123', 'B')] = 78
    if change == 'delete': args['action'] = 'deleteConfig'
    if change == 'create': args['action'] = 'createConfig'
    with pytest.raises(AuthError) as caught:
        await authorize(h, **args)
    assert caught.value.code == 'EDITOR_WRITE_FORBIDDEN'
    assert caught.value.http_status == 403


@pytest.mark.asyncio
async def test_named_admin_requires_native_password_and_explicit_group(environment):
    environment.state.groups = [1]
    await authorize(environment, action='deleteConfig')
    environment.scopes.assert_not_awaited()
    with pytest.raises(AuthError):
        await authorize(environment, action='deleteConfig', password='incorrect')


@pytest.mark.asyncio
async def test_portal_unavailable_does_not_allow_write(environment, monkeypatch):
    monkeypatch.setattr(policy, 'read_editor_snapshot', AsyncMock(side_effect=NetworkError(code='EDITOR_PORTAL_READ_FAILED')))
    with pytest.raises(NetworkError):
        await authorize(environment)
    environment.scopes.assert_not_awaited()
