"""Editor provisioning must fail closed and preserve ordering on reconciliation."""
from contextlib import asynccontextmanager
from html import escape
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl

import pytest

from common.editor_portal import EditorModule, EditorSnapshot
from common.exceptions import ParseError
from services.infoboards_service import editor_service as editor
from services.infoboards_service.editor_scope import EditorScope
from services.infoboards_service import init_company_service as company
from services.infoboards_service.admin_dirs import acl_values
from services.users_service.html_parser import normalize_title


BASE = 'https://catalog'
REG = '465304'
ROOT = 'P001'
BRANCH = 'P002'
PAGE = 'P003'


def snapshot(*uids):
    return EditorSnapshot(tuple(EditorModule(f'{BASE}/docs/?infoboard={ROOT}', uid) for uid in uids))


@pytest.fixture
def harness(monkeypatch):
    events, bindings = [], {}
    names = {normalize_title(editor._WEBMASTER): 1, normalize_title('Администраторы подсистем'): 2}

    class Groups:
        async def get(self, base, reg):
            return dict(bindings)

        async def put(self, base, reg, binding):
            events.append(('binding', binding))
            bindings[binding.branch_id] = binding

    async def create(base, title, cookies):
        events.append(('create_group', title))
        names[normalize_title(title)] = 77
        return True

    catalog = SimpleNamespace(http_client=object(), create_group=AsyncMock(side_effect=create),
                              get_user_page=AsyncMock(return_value=None), post_user=AsyncMock())
    users = SimpleNamespace(catalog_client=catalog,
                            auth_client=SimpleNamespace(get_admin_cookies=AsyncMock(return_value={})),
                            _verify_user=AsyncMock())
    service = editor.EditorService(SimpleNamespace(), SimpleNamespace(resolve_base_url=AsyncMock(return_value=BASE)),
                                   users, groups=Groups())
    service._group_index = AsyncMock(side_effect=lambda *args: dict(names))

    async def member(base, cookies, uid, gid, allowed):
        events.append(('member', uid, gid, allowed))
    service._member = AsyncMock(side_effect=member)

    async def apply(*args, **kwargs):
        events.append(('acl', args, kwargs))
    acl = AsyncMock(side_effect=apply)
    monkeypatch.setattr(editor, 'InitCompanyService', lambda *args, **kwargs: SimpleNamespace(_apply_acl=acl))
    scopes = AsyncMock(return_value={ROOT: EditorScope(BRANCH, frozenset({BRANCH, PAGE}))})
    monkeypatch.setattr(editor, 'resolve_editor_scopes', scopes)

    @asynccontextmanager
    async def lock(base):
        yield

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(editor, 'catalog_operation', lock)
    monkeypatch.setattr(editor, 'get_db_session', session)
    return SimpleNamespace(service=service, events=events, bindings=bindings, names=names,
                           acl=acl, scopes=scopes, catalog=catalog, users=users)


@pytest.mark.asyncio
async def test_create_group_then_confirm_acl_before_membership_and_repeat(harness):
    h = harness
    await h.service.sync(REG, snapshot('alice'))
    kinds = [e[0] for e in h.events]
    assert kinds.count('create_group') == 1
    assert kinds.index('binding') < kinds.index('create_group') < kinds.index('acl') < kinds.index('member')
    assert ('member', 'alice', 77, True) in h.events
    call = h.acl.await_args
    assert call.args[2] == {BRANCH: [77], PAGE: [77]}
    assert call.kwargs['view_acl_matrix'] == {BRANCH: [77], PAGE: [77], ROOT: [77]}
    assert call.kwargs['exclusive_edit'] == {BRANCH: {1, 2, 77}, PAGE: {1, 2, 77}}
    await h.service.sync(REG, snapshot('alice'))
    h.catalog.create_group.assert_awaited_once()
    assert h.bindings[BRANCH].group_id == 77


@pytest.mark.asyncio
async def test_revocation_removes_member_before_acl_and_leaves_admin_whitelist(harness):
    h = harness
    await h.service.sync(REG, snapshot('alice'))
    h.events.clear()
    h.scopes.return_value = {}
    await h.service.sync(REG, snapshot(None))
    assert h.events[0] == ('member', 'alice', 77, False)
    call = h.acl.await_args
    assert call.args[2] == {}
    assert call.kwargs['view_acl_matrix'] == {}
    assert call.kwargs['exclusive_edit'] == {BRANCH: {1, 2}, PAGE: {1, 2}}
    assert all(e[0] != 'member' or e[3] is False for e in h.events)


@pytest.mark.asyncio
async def test_transfer_revokes_old_before_new_grant_without_new_group(harness):
    h = harness
    await h.service.sync(REG, snapshot('alice'))
    h.events.clear()
    await h.service.sync(REG, snapshot('bob'))
    old = h.events.index(('member', 'alice', 77, False))
    acl = next(i for i, event in enumerate(h.events) if event[0] == 'acl')
    new = h.events.index(('member', 'bob', 77, True))
    assert old < acl < new
    h.catalog.create_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_acl_never_grants_editor_membership(harness):
    h = harness
    h.acl.side_effect = ParseError(code='ACL_WRITE_UNCERTAIN', message='uncertain')
    with pytest.raises(ParseError, match='uncertain'):
        await h.service.sync(REG, snapshot('alice'))
    h.service._member.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_native_user_is_not_created_or_granted(harness):
    h = harness
    await editor.EditorService._member(h.service, BASE, {}, 'missing', 77, True)
    h.catalog.post_user.assert_not_awaited()
    h.users._verify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_unowned_reserved_name_is_a_conflict_before_acl_or_membership(harness):
    h = harness
    h.names[normalize_title(editor._group_title(REG, BRANCH))] = 77
    with pytest.raises(ParseError) as caught:
        await h.service.sync(REG, snapshot('alice'))
    assert caught.value.code == 'EDITOR_GROUP_NAME_CONFLICT'
    h.catalog.create_group.assert_not_awaited()
    h.acl.assert_not_awaited()
    h.service._member.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_binding_refuses_acl_and_new_grant(harness):
    h = harness
    h.bindings[BRANCH] = editor.EditorGroup(BRANCH, ROOT, 77, (BRANCH,), ('alice',))
    h.names[normalize_title(editor._group_title(REG, BRANCH))] = 78
    with pytest.raises(ParseError) as caught:
        await h.service.sync(REG, snapshot('alice'))
    assert caught.value.code == 'EDITOR_GROUP_BINDING_BROKEN'
    h.acl.assert_not_awaited()
    h.service._member.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_failure_cannot_grant_new_editor(harness):
    h = harness
    h.scopes.side_effect = ParseError(code='EDITOR_SCOPE_INVALID', message='ambiguous scope')
    with pytest.raises(ParseError) as caught:
        await h.service.sync(REG, snapshot('alice'))
    assert caught.value.code == 'EDITOR_SCOPE_INVALID'
    h.catalog.create_group.assert_not_awaited()
    h.acl.assert_not_awaited()
    h.service._member.assert_not_awaited()


def _acl_form(edit, view, unrelated):
    html = '<form action="/admin/dirs"><input name="n" value="2">'
    for name, values in [('acl_infoboard_p002', edit), ('acl_infoboard_p002_view', view),
                         ('acl_infoboard_other_view', unrelated)]:
        value = escape(json.dumps({'kw': [str(v) for v in values]}), quote=True)
        html += f'<input type="hidden" name="{name}" value="{value}">'
        html += f'<input type="checkbox" name="{name[4:]}" {"checked" if values else ""}>'
    return html + '<input type="submit" name="action" value="save"></form>'


@pytest.mark.asyncio
async def test_exclusive_edit_preserves_unrelated_view_and_confirms_full_form(monkeypatch):
    before = _acl_form([1, 8], [8, 99], [44])
    after = _acl_form([1, 77], [8, 77, 99], [44])
    fetch = AsyncMock(side_effect=[(before, BASE + '/admin/dirs', '', '2'), (after, BASE + '/admin/dirs', '', '2')])
    monkeypatch.setattr(company, 'fetch_acl_form_via_http', fetch)
    ownership = SimpleNamespace(get=AsyncMock(return_value=({}, {})), replace=AsyncMock())
    client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    service = company.InitCompanyService(None, SimpleNamespace(ACL_WRITE_TIMEOUT=10), client, None, ownership)
    await service._apply_acl(BASE, {}, {BRANCH: [77]}, {}, reg=REG,
                             view_acl_matrix={BRANCH: [77]}, exclusive_edit={BRANCH: {1, 77}})
    sent = acl_values(parse_qsl(client.post.await_args.kwargs['content'], keep_blank_values=True))
    assert set(sent['acl_infoboard_p002']['kw']) == {'1', '77'}
    assert set(sent['acl_infoboard_p002_view']['kw']) == {'8', '77', '99'}
    assert sent['acl_infoboard_other_view']['kw'] == ['44']
    assert fetch.await_count == 2
    ownership.replace.assert_awaited_once_with(BASE, REG, {'acl_infoboard_p002': {77}, 'acl_infoboard_p002_view': {77}})


@pytest.mark.asyncio
async def test_exclusive_edit_refuses_other_owned_groups_before_write(monkeypatch):
    html = _acl_form([1, 8], [8], [44])
    monkeypatch.setattr(company, 'fetch_acl_form_via_http', AsyncMock(return_value=(html, '', '', '2')))
    ownership = SimpleNamespace(get=AsyncMock(return_value=({}, {'acl_infoboard_p002': {8}})), replace=AsyncMock())
    client = SimpleNamespace(post=AsyncMock())
    service = company.InitCompanyService(None, None, client, None, ownership)
    with pytest.raises(ParseError) as caught:
        await service._apply_acl(BASE, {}, {BRANCH: [77]}, {}, reg=REG,
                                 view_acl_matrix={}, exclusive_edit={BRANCH: {1, 77}})
    assert caught.value.code == 'EDITOR_ACL_CONFLICT'
    client.post.assert_not_awaited()
    ownership.replace.assert_not_awaited()
