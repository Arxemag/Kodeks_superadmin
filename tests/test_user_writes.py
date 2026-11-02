"""Exercise real parser/service behaviour at the read/modify/verify boundaries."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from common.exceptions import NetworkError, ParseError
from services.users_service.dto import CreateUserDTO, UpdateUserDTO, UpdateUserDepartmentsDTO
from services.users_service.service import UserService
from tests.conftest import StubAuthClient, StubRegResolver, StubGroupOwnership, StubDepartmentGroups


class Catalog:
    def __init__(self, pages):
        self.get_user_page = AsyncMock(side_effect=pages)
        self.post_user = AsyncMock()
        self.get_user_create_page = AsyncMock(return_value=USER_TEMPLATE)
        self.get_groups_page = AsyncMock(return_value='<select name="grp"></select>')
        self.create_group = AsyncMock()

    @asynccontextmanager
    async def operation(self, base_url):
        yield


USER_TEMPLATE = '''<form action="/users/users">
<input name="uid" value=""><input name="psw" value="">
<input type="hidden" name="token" value="current">
<input name="extra" value=""><input name="repeat" value="a"><input name="repeat" value="b">
<input name="set" value="">
</form>'''


def user_page(mail="old", uid="alice", internal_id="42", groups=(3, 9)):
    return f'''<form action="/users/users">
    <input name="uid" value="{uid}"><input name="psw" value="password">
    <input name="id" value="{internal_id}"><input name="mail" value="{mail}">
    <input name="hidden_setting" value="keep"><input name="empty_setting" value="">
    <input name="repeat" value="a"><input name="repeat" value="b">
    {''.join(f'<input type="checkbox" name="grp" value="{g}" checked>' for g in groups)}
    <input name="set" value="save">
    </form>'''


def service(catalog):
    return UserService(StubAuthClient(), catalog, StubRegResolver(), StubGroupOwnership(), StubDepartmentGroups({}))


@pytest.mark.asyncio
async def test_update_preserves_all_controls_internal_id_password_and_groups():
    catalog = Catalog([user_page(), user_page(mail="new")])
    await service(catalog).handle_update(UpdateUserDTO(reg="1", uid="alice", mail="new", id="platform-id"))
    posted = catalog.post_user.call_args.args[1]
    assert ("id", "42") in posted
    assert ("id", "platform-id") not in posted
    assert ("psw", "password") in posted
    assert ("hidden_setting", "keep") in posted
    assert ("empty_setting", "") in posted
    assert [(k, v) for k, v in posted if k == "repeat"] == [("repeat", "a"), ("repeat", "b")]
    assert [(k, v) for k, v in posted if k == "grp"] == [("grp", "3"), ("grp", "9")]
    assert ("set", "save") in posted


@pytest.mark.asyncio
async def test_create_reads_template_and_verifies_write():
    catalog = Catalog([None, user_page()])
    await service(catalog).handle_create(CreateUserDTO(reg="1", uid="alice", psw="password"))
    catalog.get_user_create_page.assert_awaited_once()
    posted = catalog.post_user.call_args.args[1]
    assert ("token", "current") in posted
    assert ("extra", "") in posted
    assert [(k, v) for k, v in posted if k == "repeat"] == [("repeat", "a"), ("repeat", "b")]
    assert catalog.get_user_page.await_count == 2


@pytest.mark.asyncio
async def test_create_department_dependency_must_be_ready_before_catalog_write():
    catalog = Catalog([])
    instance = service(catalog)
    with pytest.raises(NetworkError) as error:
        await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password",
            departments=[{"id": "not-ready", "title": "Ecology"}]))
    assert error.value.code == "DEPARTMENT_GROUP_NOT_READY"
    catalog.get_user_page.assert_not_awaited()
    catalog.post_user.assert_not_awaited()
    catalog.create_group.assert_not_awaited()
    catalog.get_user_create_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_stale_department_binding_waits_before_user_is_created():
    catalog = Catalog([])
    catalog.get_groups_page.return_value = '<a href="grp?grp=8">Another department</a>'
    instance = service(catalog)
    instance.department_groups = StubDepartmentGroups({"stable-id": 7})
    with pytest.raises(NetworkError) as error:
        await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password",
            departments=[{"id": "stable-id", "title": "Ecology"}]))
    assert error.value.code == "DEPARTMENT_GROUP_NOT_READY"
    catalog.get_user_page.assert_not_awaited()
    catalog.post_user.assert_not_awaited()
    catalog.create_group.assert_not_awaited()


PORTAL_GROUPS = '<a href="grp?grp=7">Ecology</a><a href="grp?grp=83">Portal entry</a>'


@pytest.mark.asyncio
async def test_create_assigns_configured_native_portal_role_and_department_in_one_post():
    catalog = Catalog([None, user_page(groups=(7, 83))])
    catalog.get_groups_page.return_value = PORTAL_GROUPS
    instance = service(catalog)
    instance.base_group_title = "  PORTAL   ENTRY  "
    instance.department_groups = StubDepartmentGroups({"department": 7})
    await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password",
        departments=[{"id": "department", "title": "Ecology"}]))
    catalog.post_user.assert_awaited_once()
    assert {int(v) for k, v in catalog.post_user.call_args.args[1] if k == "grp"} == {7, 83}
    assert await instance.group_ownership.get("1", "alice") == {7}


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [{}, {"departments": []}])
async def test_configured_portal_only_create_never_inherits_default_admin_group(snapshot):
    catalog = Catalog([None, user_page(groups=(83,))])
    catalog.get_groups_page.return_value = PORTAL_GROUPS
    catalog.get_user_create_page.return_value = USER_TEMPLATE.replace("</form>", '<input type="checkbox" name="grp" value="1" checked></form>')
    instance = service(catalog)
    instance.base_group_title = "Portal entry"
    await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", **snapshot))
    assert [(k, v) for k, v in catalog.post_user.call_args.args[1] if k == "grp"] == [("grp", "83")]
    assert await instance.group_ownership.get("1", "alice") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["departments", "explicit_grp"])
async def test_clearing_departments_preserves_portal_and_manual_membership(operation):
    catalog = Catalog([user_page(groups=(3, 9, 83)), user_page(groups=(9, 83))])
    catalog.get_groups_page.return_value = PORTAL_GROUPS
    instance = service(catalog)
    instance.base_group_title = "Portal entry"
    # The base role may already have been recorded as owned before opt-in.
    await instance.group_ownership.set("1", "alice", {3, 83})
    if operation == "departments":
        await instance.handle_update_departments(UpdateUserDepartmentsDTO(reg="1", id="alice", departments=[]))
    else:
        await instance.handle_update(UpdateUserDTO(reg="1", uid="alice", grp=[]))
    assert {int(v) for k, v in catalog.post_user.call_args.args[1] if k == "grp"} == {9, 83}
    assert await instance.group_ownership.get("1", "alice") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_page,code", [
    ('<a href="grp?grp=7">Ecology</a>', "USER_BASE_GROUP_NOT_FOUND"),
    ('<a href="grp?grp=83">Portal</a><a href="grp?grp=84"> PORTAL </a>', "AMBIGUOUS_GROUP"),
])
@pytest.mark.parametrize("operation", ["create", "departments", "explicit_grp"])
async def test_missing_or_ambiguous_portal_group_refuses_all_user_writes(group_page, code, operation):
    catalog = Catalog([user_page()])
    catalog.get_groups_page.return_value = group_page
    instance = service(catalog)
    instance.base_group_title = "Portal"
    with pytest.raises(ParseError) as error:
        if operation == "create":
            await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", departments=[]))
        elif operation == "departments":
            await instance.handle_update_departments(UpdateUserDepartmentsDTO(reg="1", id="alice", departments=[]))
        else:
            await instance.handle_update(UpdateUserDTO(reg="1", uid="alice", grp=[]))
    assert error.value.code == code
    catalog.post_user.assert_not_awaited()
    catalog.create_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Администратор ( webmaster )", "Administrators"])
async def test_configured_portal_role_cannot_be_admin_group(title):
    catalog = Catalog([])
    catalog.get_groups_page.return_value = f'<a href="grp?grp=1">{title}</a>'
    instance = service(catalog)
    instance.base_group_title = title
    with pytest.raises(ParseError) as error:
        await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", departments=[]))
    assert error.value.code == "USER_BASE_GROUP_FORBIDDEN"
    catalog.post_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_default_title_is_allowed_only_when_explicitly_configured():
    title = "Пользователи Кодекс-клиента ( default )"
    catalog = Catalog([None, user_page(groups=(83,))])
    catalog.get_groups_page.return_value = f'<a href="grp?grp=83">{title}</a>'
    instance = service(catalog)
    instance.base_group_title = title
    await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", departments=[]))
    assert [(k, v) for k, v in catalog.post_user.call_args.args[1] if k == "grp"] == [("grp", "83")]
    assert await instance.group_ownership.get("1", "alice") == set()


@pytest.mark.asyncio
async def test_create_unions_explicit_groups_with_resolved_departments_not_template_defaults():
    catalog = Catalog([None, user_page(groups=(7, 8))])
    catalog.get_user_create_page.return_value = USER_TEMPLATE.replace("</form>", '<input type="checkbox" name="grp" value="1" checked></form>')
    instance = service(catalog)
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance.department_groups = StubDepartmentGroups({"stable-id": 7})
    payload = CreateUserDTO(reg="1", uid="alice", psw="password", grp=[8, 7],
        departments=[{"id": "stable-id", "title": "Display title may change"}])
    await instance.handle_create(payload)
    posted = catalog.post_user.call_args.args[1]
    assert {int(v) for k, v in posted if k == "grp"} == {7, 8}
    assert ("grp", "1") not in posted
    assert await instance.group_ownership.get("1", "alice") == {7, 8}
    assert payload.grp == [8, 7]


@pytest.mark.asyncio
async def test_explicit_empty_create_departments_grants_no_template_default_access():
    catalog = Catalog([None, user_page(groups=())])
    catalog.get_user_create_page.return_value = USER_TEMPLATE.replace("</form>", '<input type="checkbox" name="grp" value="1" checked></form>')
    instance = service(catalog)
    await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", departments=[]))
    assert not any(k == "grp" for k, v in catalog.post_user.call_args.args[1])
    assert await instance.group_ownership.get("1", "alice") == set()


@pytest.mark.asyncio
async def test_create_department_retry_on_existing_user_preserves_manual_groups():
    catalog = Catalog([None, user_page(groups=(7,)), user_page(groups=(7, 9))])
    instance = service(catalog)
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance.department_groups = StubDepartmentGroups({"department": 7})
    payload = CreateUserDTO(reg="1", uid="alice", psw="password",
        departments=[{"id": "department", "title": "Ecology"}])
    original_fields = set(payload.model_fields_set)
    await instance.handle_create(payload)
    await instance.handle_create(payload)
    assert catalog.post_user.await_count == 1
    assert payload.model_fields_set == original_fields and payload.grp is None
    assert await instance.group_ownership.get("1", "alice") == {7}


@pytest.mark.asyncio
async def test_create_existing_user_applies_resolved_groups_preserving_manual_membership():
    catalog = Catalog([user_page(groups=(3, 9)), user_page(groups=(7, 8, 9))])
    instance = service(catalog)
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance.department_groups = StubDepartmentGroups({"department": 7})
    await instance.group_ownership.set("1", "alice", {3})
    await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", grp=[8],
        departments=[{"id": "department", "title": "Ecology"}]))
    posted = catalog.post_user.call_args.args[1]
    assert {int(v) for k, v in posted if k == "grp"} == {7, 8, 9}
    assert ("hidden_setting", "keep") in posted
    assert await instance.group_ownership.get("1", "alice") == {7, 8}


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_grp", [False, True])
async def test_legacy_create_without_departments_preserves_existing_grp_semantics(explicit_grp):
    catalog = Catalog([user_page(groups=(3, 9))])
    instance = service(catalog)
    instance.department_groups.resolve = AsyncMock(side_effect=AssertionError("Legacy input must not resolve departments"))
    fields = {"grp": [3, 9]} if explicit_grp else {}
    await instance.handle_create(CreateUserDTO(reg="1", uid="alice", psw="password", **fields))
    catalog.post_user.assert_not_awaited()
    instance.department_groups.resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [None, user_page(uid="other"), USER_TEMPLATE])
async def test_create_never_reports_unconfirmed_write(after):
    catalog = Catalog([None, after])
    with pytest.raises(ParseError):
        await service(catalog).handle_create(CreateUserDTO(reg="1", uid="alice", psw="password"))


@pytest.mark.asyncio
async def test_existing_blank_form_does_not_trigger_creation():
    catalog = Catalog([USER_TEMPLATE])
    with pytest.raises(ParseError, match="blank response"):
        await service(catalog).handle_create(CreateUserDTO(reg="1", uid="alice", psw="password"))
    catalog.post_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_user_never_posts():
    catalog = Catalog([user_page()])
    await service(catalog).handle_update(UpdateUserDTO(reg="1", uid="alice", mail="old"))
    catalog.post_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_business_error_does_not_become_success():
    catalog = Catalog([user_page(), user_page()])
    with pytest.raises(ParseError, match="mail"):
        await service(catalog).handle_update(UpdateUserDTO(reg="1", uid="alice", mail="new"))


@pytest.mark.asyncio
async def test_user_post_followed_by_read_failure_is_uncertain():
    catalog = Catalog([user_page(), NetworkError(code="CATALOG_5XX", message="unavailable")])
    with pytest.raises(ParseError) as error:
        await service(catalog).handle_update(UpdateUserDTO(reg="1", uid="alice", mail="new"))
    assert error.value.code == "USER_WRITE_UNCERTAIN"
    catalog.post_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_assignments_never_create_missing_company_group():
    catalog = Catalog([])
    with pytest.raises(NetworkError, match="Company synchronisation"):
        await service(catalog).handle_update_departments(UpdateUserDepartmentsDTO(
            reg="1", id="alice", departments=[{"id": "department-id", "title": "Ecology"}],
        ))
    catalog.create_group.assert_not_awaited()
    catalog.post_user.assert_not_awaited()


def test_update_password_omission_and_null_are_distinct():
    from pydantic import ValidationError
    dto = UpdateUserDTO(reg="1", uid="alice")
    assert "psw" not in dto.model_fields_set
    with pytest.raises(ValidationError):
        UpdateUserDTO(reg="1", uid="alice", psw=None)


@pytest.mark.asyncio
async def test_assignment_preserves_manual_group_and_removes_only_owned_group():
    catalog = Catalog([user_page(groups=(3, 9)), user_page(groups=(7, 9))])
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Renamed department</a>'
    instance = service(catalog)
    instance.department_groups = StubDepartmentGroups({"stable-uuid": 7})
    await instance.group_ownership.set("1", "alice", {3})
    await instance.handle_update_departments(UpdateUserDepartmentsDTO(
        reg="1", id="alice", departments=[{"id": "stable-uuid", "title": "Old platform title"}],
    ))
    posted = catalog.post_user.call_args.args[1]
    assert {v for k, v in posted if k == 'grp'} == {'7', '9'}
    assert await instance.group_ownership.get("1", "alice") == {7}
    catalog.create_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_assignments_clear_only_groups_previously_written_by_integration():
    catalog = Catalog([user_page(groups=(3, 9)), user_page(groups=(9,))])
    instance = service(catalog)
    await instance.group_ownership.set("1", "alice", {3})
    await instance.handle_update_departments(UpdateUserDepartmentsDTO(reg="1", id="alice", departments=[]))
    posted = catalog.post_user.call_args.args[1]
    assert {v for k, v in posted if k == 'grp'} == {'9'}
    assert await instance.group_ownership.get("1", "alice") == set()


@pytest.mark.asyncio
async def test_preexisting_manual_group_does_not_become_owned_by_selecting_it():
    catalog = Catalog([user_page(groups=(7, 9))])
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance = service(catalog)
    instance.department_groups = StubDepartmentGroups({"department": 7})
    await instance.handle_update_departments(UpdateUserDepartmentsDTO(
        reg="1", id="alice", departments=[{"id": "department", "title": "Ecology"}],
    ))
    assert await instance.group_ownership.get("1", "alice") == set()
    catalog.post_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_group_is_not_removed_when_first_registration_clears_its_departments():
    catalog = Catalog([user_page(groups=(3, 9))])
    instance = service(catalog)
    await instance.group_ownership.set('1', 'alice', {3})
    await instance.group_ownership.set('2', 'alice', {3})
    await instance.handle_update_departments(UpdateUserDepartmentsDTO(reg='1', id='alice', departments=[]))
    catalog.post_user.assert_not_awaited()
    assert await instance.group_ownership.get('1', 'alice') == set()
    assert await instance.group_ownership.get('2', 'alice') == {3}


@pytest.mark.asyncio
async def test_second_registration_records_interest_in_existing_managed_group():
    catalog = Catalog([user_page(groups=(7, 9))])
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance = service(catalog)
    instance.department_groups = StubDepartmentGroups({'department': 7})
    await instance.group_ownership.set('2', 'alice', {7})
    await instance.handle_update_departments(UpdateUserDepartmentsDTO(
        reg='1', id='alice', departments=[{'id': 'department', 'title': 'Ecology'}],
    ))
    catalog.post_user.assert_not_awaited()
    assert await instance.group_ownership.get('1', 'alice') == {7}


@pytest.mark.asyncio
async def test_ownership_failure_after_confirmed_user_update_is_uncertain():
    catalog = Catalog([user_page(groups=(3, 9)), user_page(groups=(7, 9))])
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance = service(catalog)
    instance.department_groups = StubDepartmentGroups({'department': 7})
    await instance.group_ownership.set('1', 'alice', {3})
    instance.group_ownership.set = AsyncMock(side_effect=NetworkError(code='GROUP_OWNERSHIP_WRITE_FAILED', message='offline'))
    with pytest.raises(ParseError) as error:
        await instance.handle_update_departments(UpdateUserDepartmentsDTO(
            reg='1', id='alice', departments=[{'id': 'department', 'title': 'Ecology'}],
        ))
    assert error.value.code == 'GROUP_OWNERSHIP_PERSIST_UNCERTAIN'
    catalog.post_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_ownership_failure_after_confirmed_user_creation_is_uncertain():
    catalog = Catalog([None, user_page(groups=(7,))])
    instance = service(catalog)
    instance.group_ownership.set = AsyncMock(side_effect=NetworkError(code='GROUP_OWNERSHIP_WRITE_FAILED', message='offline'))
    with pytest.raises(ParseError) as error:
        await instance.handle_create(CreateUserDTO(reg='1', uid='alice', psw='password', grp=[7]))
    assert error.value.code == 'GROUP_OWNERSHIP_PERSIST_UNCERTAIN'
    catalog.post_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_metadata_failure_with_no_catalog_post_remains_retryable():
    catalog = Catalog([user_page(groups=(7, 9))])
    catalog.get_groups_page.return_value = '<a href="grp?grp=7">Ecology</a>'
    instance = service(catalog)
    instance.department_groups = StubDepartmentGroups({'department': 7})
    instance.group_ownership.set = AsyncMock(side_effect=NetworkError(code='GROUP_OWNERSHIP_WRITE_FAILED', message='offline'))
    with pytest.raises(NetworkError):
        await instance.handle_update_departments(UpdateUserDepartmentsDTO(
            reg='1', id='alice', departments=[{'id': 'department', 'title': 'Ecology'}],
        ))
    catalog.post_user.assert_not_awaited()
