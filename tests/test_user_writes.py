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
