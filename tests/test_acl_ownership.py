"""Owned grants may be removed; pre-existing manual ACLs remain independent."""
import json
from contextlib import asynccontextmanager
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl

import pytest
from sqlalchemy.exc import OperationalError

from common import acl_ownership
from common.acl_ownership import AclOwnershipRepository, plan_acl_assignments
from common.exceptions import NetworkError, ParseError
from services.infoboards_service import init_company_service as company
from services.infoboards_service.admin_dirs import acl_values, build_admin_dirs_form, parse_form_pairs


A = "acl_infoboard_p1"
V = "acl_infoboard_p1_view"


def form(access=(), view=(), *, second=None):
    fields = [(A, access), (V, view)]
    if second is not None:
        fields.extend([("acl_infoboard_p2", second), ("acl_infoboard_p2_view", second)])
    html = '<form action="/admin/dirs"><input name=unrelated value="">'
    for name, ids in fields:
        html += f'<input type=hidden name="{name}" value="{escape(json.dumps({"kw": [str(i) for i in ids]}), quote=True)}">'
        html += f'<input type=checkbox name="{name.removeprefix("acl_")}" {"checked" if ids else ""}>'
    return html + '<input type=submit name=cmd value=save></form>'


@pytest.fixture
def configure(monkeypatch):
    def setup(before, after=None, owned=None):
        snapshots = [before] if after is None else [before, after]
        fetch = AsyncMock(side_effect=[(html, "https://catalog/admin/dirs", "", "2") for html in snapshots])
        monkeypatch.setattr(company, "fetch_acl_form_via_http", fetch)
        repo = SimpleNamespace(get=AsyncMock(return_value=(owned or {}, {})), replace=AsyncMock())
        client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200)))
        return company.InitCompanyService(None, None, client, None, repo), repo, client, fetch
    return setup


@pytest.mark.asyncio
async def test_manual_acl_grants_are_preserved_and_access_view_are_independent(configure):
    service, repo, client, _ = configure(form((99,), (98,)), form((3, 99), (3, 98)))
    await service._apply_acl("https://catalog", {}, {"P1": [3]}, {}, reg="reg-a")
    sent = parse_qsl(client.post.await_args.kwargs["content"], keep_blank_values=True)
    assert set(acl_values(sent)[A]["kw"]) == {"3", "99"}
    assert set(acl_values(sent)[V]["kw"]) == {"3", "98"}
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {A: {3}, V: {3}})


@pytest.mark.asyncio
async def test_remove_only_owned_grants_while_retaining_unmanaged_view_grant(configure):
    service, repo, _, _ = configure(form((3, 99), (3, 98)), form((99,), (3, 98)), {A: {3}})
    await service._apply_acl("https://catalog", {}, {}, {}, reg="reg-a")
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {})


@pytest.mark.asyncio
async def test_noop_does_not_adopt_existing_manual_permissions(configure):
    service, repo, client, fetch = configure(form((3, 99), (3, 98)))
    await service._apply_acl("https://catalog", {}, {"P1": [3]}, {}, reg="reg-a")
    client.post.assert_not_awaited()
    assert fetch.await_count == 1
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {})


@pytest.mark.asyncio
async def test_previously_managed_board_removed_from_desired_is_cleaned(configure):
    service, repo, _, _ = configure(
        form((3, 99), (3, 98), second=(7,)),
        form((99,), (98,), second=(7,)),
        {A: {3}, V: {3}},
    )
    await service._apply_acl("https://catalog", {}, {"P2": [7]}, {}, reg="reg-a")
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {})


@pytest.mark.asyncio
async def test_failed_post_verification_does_not_record_ownership(configure):
    service, repo, _, _ = configure(form((99,), (98,)), form((99,), (98,)))
    with pytest.raises(ParseError) as caught:
        await service._apply_acl("https://catalog", {}, {"P1": [3]}, {}, reg="reg-a")
    assert caught.value.code == "ACL_WRITE_NOT_CONFIRMED"
    repo.replace.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_previously_owned_control_fails_before_write(configure):
    service, repo, client, _ = configure(form(), owned={"acl_infoboard_missing": {3}})
    with pytest.raises(ParseError) as caught:
        await service._apply_acl("https://catalog", {}, {}, {}, reg="reg-a")
    assert caught.value.code == "ACL_BOARD_NOT_FOUND"
    client.post.assert_not_awaited()
    repo.replace.assert_not_awaited()


def test_old_owned_and_newly_added_grants_only_become_next_owned():
    overrides, owned = plan_acl_assignments({A: {1, 2, 99}}, {A: {1, 2}}, {A: {2, 3, 99}})
    assert overrides == {A: [2, 3, 99]}
    assert owned == {A: {2, 3}}


@pytest.mark.asyncio
async def test_sync_one_registration_never_removes_another_registration_grants(configure):
    service, repo, client, _ = configure(
        form((3, 4, 99), (3, 4, 98)), form((4, 99), (4, 98)), {A: {3}, V: {3}},
    )
    repo.get.return_value = ({A: {3}, V: {3}}, {A: {4}, V: {4}})
    await service._apply_acl("https://catalog", {}, {}, {}, reg="reg-a")
    sent = parse_qsl(client.post.await_args.kwargs["content"], keep_blank_values=True)
    assert set(acl_values(sent)[A]["kw"]) == {"4", "99"}
    assert set(acl_values(sent)[V]["kw"]) == {"4", "98"}
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {})


@pytest.mark.asyncio
async def test_shared_grant_remains_when_first_registration_releases_it(configure):
    service, repo, client, _ = configure(form((3,), (3,)), owned={A: {3}, V: {3}})
    repo.get.return_value = ({A: {3}, V: {3}}, {A: {3}, V: {3}})
    await service._apply_acl("https://catalog", {}, {}, {}, reg="reg-a")
    client.post.assert_not_awaited()
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {})


def test_shared_grant_is_recorded_as_owned_by_each_interested_registration():
    overrides, owned = plan_acl_assignments({A: {3, 99}}, {}, {A: {3, 99}}, {A: {3}})
    assert overrides == {A: [3, 99]}
    assert owned == {A: {3}}


def test_builder_supports_separate_access_view_fields_without_synthesizing_pair():
    pairs = parse_form_pairs(form((1,), (2,)), acl=True)
    result = build_admin_dirs_form(pairs, {}, field_overrides={A: [3]})
    assert acl_values(result)[A]["kw"] == ["3"]
    assert acl_values(result)[V]["kw"] == ["2"]
    with pytest.raises(ParseError):
        build_admin_dirs_form(pairs, {}, field_overrides={"acl_infoboard_unknown": [3]})


@pytest.mark.asyncio
async def test_browser_path_builds_ownership_from_its_live_form_and_records_after_verify(monkeypatch):
    monkeypatch.setattr(company, "fetch_acl_form_via_http", AsyncMock(return_value=("<html>JS form</html>", "", "", "2")))
    repo = SimpleNamespace(get=AsyncMock(return_value=({A: {1}}, {})), replace=AsyncMock())

    async def browser(*args, prepare_field_overrides, **kwargs):
        overrides = await prepare_field_overrides(form((1, 99), (98,)))
        assert overrides == {A: [3, 99], V: [3, 98]}
        repo.replace.assert_not_awaited()

    writer = AsyncMock(side_effect=browser)
    monkeypatch.setattr(company, "apply_acl_via_browser", writer)
    service = company.InitCompanyService(None, None, None, None, repo)
    await service._apply_acl("https://catalog", {}, {"P1": [3]}, {}, reg="reg-a")
    repo.replace.assert_awaited_once_with("https://catalog", "reg-a", {A: {3}, V: {3}})


@pytest.fixture
def repository_db(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock())
    events = []

    @asynccontextmanager
    async def transaction():
        events.append("begin")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    session.begin = transaction

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(acl_ownership, "get_db_session", sessions)
    return session, events


@pytest.mark.asyncio
async def test_repository_uses_canonical_catalog_and_independent_controls(repository_db):
    session, events = repository_db
    session.execute.return_value = [("reg-a", A, [1]), ("reg-b", V, "[2]")]
    assert await AclOwnershipRepository().get("HTTPS://CATALOG:443", "reg-a") == ({A: {1}}, {V: {2}})
    assert session.execute.await_args.args[1] == {"catalog_url": "https://catalog/"}
    await AclOwnershipRepository().replace("https://catalog/", "reg-a", {A: {1}, V: {2}})
    assert events == ["begin", "commit"]
    inserts = session.execute.await_args_list[-2:]
    assert {call.args[1]["name"] for call in inserts} == {A, V}
    assert all(call.args[1]["reg"] == "reg-a" for call in inserts)
    delete = session.execute.await_args_list[-3]
    assert "AND reg = :reg" in str(delete.args[0])


@pytest.mark.asyncio
async def test_repository_replace_failure_rolls_back_and_is_retryable(repository_db):
    session, events = repository_db
    session.execute.side_effect = [None, OperationalError("insert", {}, Exception("failed"))]
    with pytest.raises(NetworkError) as caught:
        await AclOwnershipRepository().replace("https://catalog", "reg-a", {A: {1}})
    assert caught.value.code == "ACL_OWNERSHIP_WRITE_FAILED"
    assert events == ["begin", "rollback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["{}", "[true]", "[1.5]", "not-json"])
async def test_repository_rejects_corrupt_metadata_instead_of_ignoring_it(repository_db, bad):
    session, _ = repository_db
    session.execute.return_value = [("reg-a", A, bad)]
    with pytest.raises(ParseError) as caught:
        await AclOwnershipRepository().get("https://catalog", "reg-a")
    assert caught.value.code == "ACL_OWNERSHIP_INVALID"
