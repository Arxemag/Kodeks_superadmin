"""Group synchronization must reuse catalog IDs and verify writes before ACL planning."""
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.exceptions import ParseError
from services.infoboards_service.init_company_service import InitCompanyService
from services.infoboards_service.dto import InitCompanyDTO


def groups_page(*groups: tuple[int, str]) -> str:
    links = "".join(
        f'<a href="grp?grp={group_id}">{escape(title)}</a>'
        for group_id, title in groups
    )
    return f"<html><body>{links}</body></html>"


@pytest.fixture
def service() -> InitCompanyService:
    instance = InitCompanyService(
        db=SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock()),
        settings=SimpleNamespace(DB_TABLE_DEPARTMENT_MAPPING="department_service_mapping", DB_TABLE_REG_SERVICES="reg_services"),
        http_client=MagicMock(),
        catalog_client=SimpleNamespace(
            get_groups_page=AsyncMock(), create_group=AsyncMock(), rename_group=AsyncMock(),
        ),
    )
    async def execute(sql, *args, **kwargs):
        if "LEFT JOIN" in str(sql):
            return []
        return instance.db.execute.return_value
    instance.db.execute.side_effect = execute
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ОТДЕЛ ПРОДАЖ", "  Отдел\u00a0  продаж  "])
async def test_existing_normalized_group_is_reused(service, name):
    service.db.execute.return_value = [("dept-id", name, None, name, "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Отдел продаж"))

    groups = await service._sync_groups("https://catalog", {}, "reg")

    assert groups == {"Отдел продаж": 17}
    service.catalog_client.create_group.assert_not_awaited()
    service.catalog_client.rename_group.assert_not_awaited()
    assert service.db.execute.await_args.kwargs == {}
    assert service.db.execute.await_args.args[1]["group_id"] == 17


@pytest.mark.asyncio
async def test_creation_refreshes_normalized_index(service):
    service.db.execute.return_value = [("dept-a", "Alpha", None, "Alpha", "adopted", False), ("dept-b", "Beta", None, "Beta", "adopted", False)]
    service.catalog_client.get_groups_page.side_effect = [
        groups_page((1, "Other")),
        groups_page((1, "Other"), (29, "alpha"), (30, "beta")),
    ]

    groups = await service._sync_groups("https://catalog", {}, "reg")

    assert groups["alpha"] == 29
    assert service.catalog_client.create_group.await_count == 1
    assert service.catalog_client.get_groups_page.await_count == 2


@pytest.mark.asyncio
async def test_missing_group_after_create_is_failure(service):
    service.db.execute.return_value = [("dept-id", "New Group", None, "New Group", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((1, "Other"))

    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")

    assert caught.value.code == "GROUP_CREATE_NOT_CONFIRMED"
    assert service.catalog_client.create_group.await_count == 1


@pytest.mark.asyncio
async def test_existing_duplicate_group_names_fail_before_create(service):
    service.db.execute.return_value = [("dept-id", "Missing", None, "Missing", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page(
        (1, "Отдел продаж"), (2, "ОТДЕЛ\u00a0ПРОДАЖ"),
    )

    with pytest.raises(ParseError):
        await service._sync_groups("https://catalog", {}, "reg")

    service.catalog_client.create_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrecognized_groups_page_does_not_trigger_create(service):
    service.db.execute.return_value = [("dept-id", "Missing", None, "Missing", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = "<html><h1>Please sign in</h1></html>"

    with pytest.raises(ParseError):
        await service._sync_groups("https://catalog", {}, "reg")

    service.catalog_client.create_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_acl_uses_normalized_group_identity(service):
    service.db.execute.return_value = [("dept-id", 17)]

    acl = await service._build_acl_async(
        "reg", {"Отдел продаж": 17}, {"Кабинет": "P1"}, {"dept-id": ["Кабинет"]},
    )

    assert acl == {"P1": [17]}


@pytest.mark.asyncio
async def test_acl_rejects_ambiguous_group_ids(service):
    service.db.execute.return_value = [("dept-id", 17)]

    with pytest.raises(ParseError):
        await service._build_acl_async(
            "reg", {"Sales": 17, "SALES": 29}, {"Board": "P1"}, {"dept-id": ["Board"]},
        )


@pytest.mark.asyncio
async def test_acl_does_not_silently_skip_missing_group(service):
    service.db.execute.return_value = [("dept-id", None)]

    with pytest.raises(ParseError) as caught:
        await service._build_acl_async(
            "reg", {"Other": 17}, {"Board": "P1"}, {"dept-id": ["Board"]},
        )

    assert caught.value.code == "GROUP_BINDING_BROKEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_id", [17, None])
async def test_rename_keeps_group_identity_and_never_creates_duplicate(service, saved_id):
    service.db.execute.return_value = [("dept-id", "New Department", saved_id, "Old Department", "adopted", True)]
    service.catalog_client.get_groups_page.side_effect = [
        groups_page((17, "Old Department")), groups_page((17, "New Department")),
    ]

    await service._sync_groups("https://catalog", {}, "reg")

    service.catalog_client.create_group.assert_not_awaited()
    service.catalog_client.rename_group.assert_awaited_once_with("https://catalog", 17, "New Department", {})
    assert service.db.execute.await_args.args[1] == {
        "group_id": 17, "confirmed_name": "New Department", "origin": "adopted", "reg": "reg", "department_id": "dept-id",
    }


@pytest.mark.asyncio
async def test_adoption_is_committed_before_external_rename(service):
    service.db.execute.return_value = [("dept-id", "New", None, "Old", "adopted", True)]
    service.catalog_client.get_groups_page.side_effect = [groups_page((17, "Old")), groups_page((17, "New"))]

    async def rename(*args):
        assert service.db.commit.await_count == 1
        assert any(call.args[1].get("group_id") == 17 and call.args[1].get("confirmed_name") == "Old"
                   for call in service.db.execute.await_args_list if len(call.args) > 1)

    service.catalog_client.rename_group.side_effect = rename
    await service._sync_groups("https://catalog", {}, "reg")


@pytest.mark.asyncio
async def test_missing_bound_id_is_not_replaced_by_same_name(service):
    service.db.execute.return_value = [("dept-id", "Sales", 17, "Sales", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((29, "Sales"))

    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_BINDING_BROKEN"
    service.catalog_client.create_group.assert_not_awaited()
    service.catalog_client.rename_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_original_group_for_unbound_rename_is_not_created(service):
    service.db.execute.return_value = [("dept-id", "New", None, "Old", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((29, "New"))
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_BINDING_BROKEN"
    service.catalog_client.create_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_rename_verification_does_not_persist_new_name(service):
    service.db.execute.return_value = [("dept-id", "New", 17, "Old", "created", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Old"))
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_RENAME_NOT_CONFIRMED"
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_id", [17, None])
async def test_adopted_group_is_never_renamed_without_explicit_authorization(service, saved_id):
    service.db.execute.return_value = [("dept-id", "New", saved_id, "Old", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Old"))
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "ADOPTED_GROUP_RENAME_REQUIRES_APPROVAL"
    service.catalog_client.create_group.assert_not_awaited()
    service.catalog_client.rename_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_created_by_integration_can_be_renamed_by_stable_id(service):
    service.db.execute.return_value = [("dept-id", "New", 17, "Old", "created", False)]
    service.catalog_client.get_groups_page.side_effect = [groups_page((17, "Old")), groups_page((17, "New"))]
    await service._sync_groups("https://catalog", {}, "reg")
    service.catalog_client.rename_group.assert_awaited_once_with("https://catalog", 17, "New", {})
    assert service.db.execute.await_args.args[1]["origin"] == "created"


@pytest.mark.asyncio
async def test_group_shared_by_two_registrations_is_not_renamed(service):
    service.db.execute.return_value = [("dept-id", "New", 17, "Old", "created", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Old"))
    async def execute(sql, *args, **kwargs):
        if "LEFT JOIN" in str(sql):
            return [("another-reg", "HTTPS://CATALOG:443/")]
        return service.db.execute.return_value
    service.db.execute.side_effect = execute
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_SHARED_ACROSS_REGISTRATIONS"
    service.catalog_client.rename_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_catalog_of_shared_id_blocks_rename(service):
    service.db.execute.return_value = [("dept-id", "New", 17, "Old", "created", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Old"))
    async def execute(sql, *args, **kwargs):
        if "LEFT JOIN" in str(sql):
            return [("another-reg", None)]
        return service.db.execute.return_value
    service.db.execute.side_effect = execute
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_SHARING_UNKNOWN"
    service.catalog_client.rename_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_creation_result_records_true_origin(service):
    service.db.execute.return_value = [("dept-id", "New", None, "New", None, False)]
    service.catalog_client.get_groups_page.side_effect = [groups_page((1, "Other")), groups_page((1, "Other"), (17, "New"))]
    service.catalog_client.create_group.return_value = True
    await service._sync_groups("https://catalog", {}, "reg")
    assert service.db.execute.await_args.args[1]["origin"] == "created"


@pytest.mark.asyncio
async def test_concurrent_discovery_of_existing_group_is_recorded_as_adopted(service):
    service.db.execute.return_value = [("dept-id", "New", None, "New", None, False)]
    service.catalog_client.get_groups_page.side_effect = [groups_page((1, "Other")), groups_page((1, "Other"), (17, "New"))]
    service.catalog_client.create_group.return_value = False
    await service._sync_groups("https://catalog", {}, "reg")
    assert service.db.execute.await_args.args[1]["origin"] == "adopted"


@pytest.mark.asyncio
async def test_retry_after_external_rename_uses_saved_id(service):
    service.db.execute.return_value = [("dept-id", "New", 17, "Old", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "New"))
    await service._sync_groups("https://catalog", {}, "reg")
    service.catalog_client.create_group.assert_not_awaited()
    service.catalog_client.rename_group.assert_not_awaited()
    assert service.db.execute.await_args.args[1]["confirmed_name"] == "New"


@pytest.mark.asyncio
async def test_two_departments_cannot_adopt_same_normalized_group(service):
    service.db.execute.return_value = [("dept-a", "Sales", None, "Sales", "adopted", False), ("dept-b", "SALES", None, "SALES", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Sales"))
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_BINDING_CONFLICT"
    service.catalog_client.create_group.assert_not_awaited()
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acl_uses_id_even_if_catalog_name_changed(service):
    service.db.execute.return_value = [("dept-id", 17)]
    acl = await service._build_acl_async("reg", {"Completely different name": 17}, {"Board": "P1"}, {"dept-id": ["BOARD"]})
    assert acl == {"P1": [17]}


@pytest.mark.asyncio
async def test_acl_rejects_ambiguous_normalized_cabinet_names(service):
    service.db.execute.return_value = [("dept-id", 17)]
    with pytest.raises(ParseError) as caught:
        await service._build_acl_async("reg", {"Sales": 17}, {"Board": "P1", "BOARD": "P2"}, {"dept-id": ["Board"]})
    assert caught.value.code == "AMBIGUOUS_CABINET"


@pytest.mark.asyncio
async def test_missing_required_cabinet_is_error(service):
    service.db.execute.return_value = [("dept-id", 17)]
    with pytest.raises(ParseError) as caught:
        await service._build_acl_async("reg", {"Sales": 17}, {"Other": "P1"}, {"dept-id": ["Board"]})
    assert caught.value.code == "CABINET_NOT_FOUND"


@pytest.mark.asyncio
async def test_rename_cannot_overwrite_another_groups_name(service):
    service.db.execute.return_value = [("dept-id", "New", 17, "Old", "adopted", False)]
    service.catalog_client.get_groups_page.return_value = groups_page((17, "Old"), (29, "New"))
    with pytest.raises(ParseError) as caught:
        await service._sync_groups("https://catalog", {}, "reg")
    assert caught.value.code == "GROUP_BINDING_CONFLICT"
    service.catalog_client.rename_group.assert_not_awaited()
    service.catalog_client.create_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("titles", [("Board", "Board"), ("Board", "BOARD\u00a0")])
async def test_graphql_duplicate_cabinets_are_not_silently_overwritten(service, titles):
    payload = {"data": {"board": {"many": [
        {"title": titles[0], "id": "P1"}, {"title": titles[1], "id": "P2"},
    ]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    with pytest.raises(ParseError) as caught:
        await service._get_boards("https://catalog", {})
    assert caught.value.code == "AMBIGUOUS_CABINET"


@pytest.mark.asyncio
async def test_graphql_errors_are_not_an_empty_cabinet_list(service):
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"errors": [{"message": "failed"}]})))
    with pytest.raises(ParseError) as caught:
        await service._get_boards("https://catalog", {})
    assert caught.value.code == "INFOBOARDS_PARSE_ERROR"


@pytest.mark.asyncio
async def test_duplicate_unrequested_cabinet_names_do_not_block_requested_modules(service):
    payload = {"data": {"board": {"many": [
        {"title": "Руководитель производства", "id": "P001"},
        {"title": "Рекомендованные документы", "id": "P002"},
        {"title": "РЕКОМЕНДОВАННЫЕ\u00a0ДОКУМЕНТЫ", "id": "P003"},
    ]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    assert await service._get_boards("https://catalog", {}, required_titles={"  РУКОВОДИТЕЛЬ  ПРОИЗВОДСТВА "}) == {
        "Руководитель производства": "P001",
    }


@pytest.mark.asyncio
async def test_duplicate_requested_cabinet_names_are_still_rejected(service):
    payload = {"data": {"board": {"many": [
        {"title": "Кабинет", "id": "P001"},
        {"title": "КАБИНЕТ", "id": "P002"},
    ]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    with pytest.raises(ParseError) as caught:
        await service._get_boards("https://catalog", {}, required_titles={"кабинет"})
    assert caught.value.code == "AMBIGUOUS_CABINET"


@pytest.mark.asyncio
async def test_missing_requested_cabinet_is_rejected_by_graph_lookup(service):
    payload = {"data": {"board": {"many": [{"title": "Other", "id": "P001"}]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    with pytest.raises(ParseError) as caught:
        await service._get_boards("https://catalog", {}, required_titles={"Missing"})
    assert caught.value.code == "CABINET_NOT_FOUND"


@pytest.mark.asyncio
async def test_explicitly_deferred_cabinet_can_be_missing_but_other_required_is_returned(service):
    payload = {"data": {"board": {"many": [{"title": "Production", "id": "P1"}]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    boards = await service._get_boards("https://catalog", {}, required_titles={"Production", "Кабинет СМК"},
                                      optional_titles={" КАБИНЕТ\u00a0СМК "})
    assert boards == {"Production": "P1"}


@pytest.mark.asyncio
async def test_deferred_cabinet_is_included_normally_when_it_exists(service):
    payload = {"data": {"board": {"many": [
        {"title": "Production", "id": "P1"}, {"title": "Кабинет СМК", "id": "P2"},
    ]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    boards = await service._get_boards("https://catalog", {}, required_titles={"Production", "КАБИНЕТ СМК"},
                                      optional_titles={"кабинет смк"})
    assert boards == {"Production": "P1", "Кабинет СМК": "P2"}
    service.db.execute.return_value = [("dept-id", 17)]
    assert await service._build_acl_async("reg", {"Department": 17}, boards,
                                        {"dept-id": ["Кабинет СМК"]}, deferred_cabinets={"кабинет смк"}) == {"P2": [17]}


@pytest.mark.asyncio
async def test_missing_non_deferred_cabinet_still_fails_when_another_is_deferred(service):
    payload = {"data": {"board": {"many": [{"title": "Production", "id": "P1"}]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    with pytest.raises(ParseError) as caught:
        await service._get_boards("https://catalog", {}, required_titles={"Production", "Кабинет СМК", "Other"},
                                  optional_titles={"Кабинет СМК"})
    assert caught.value.code == "CABINET_NOT_FOUND"
    assert "other" in caught.value.message


@pytest.mark.asyncio
async def test_deferred_title_if_ambiguous_still_fails(service):
    payload = {"data": {"board": {"many": [
        {"title": "Кабинет СМК", "id": "P1"}, {"title": "КАБИНЕТ СМК", "id": "P2"},
    ]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    with pytest.raises(ParseError) as caught:
        await service._get_boards("https://catalog", {}, required_titles={"Кабинет СМК"}, optional_titles={"Кабинет СМК"})
    assert caught.value.code == "AMBIGUOUS_CABINET"


@pytest.mark.asyncio
async def test_acl_planner_omits_only_explicitly_deferred_missing_cabinet(service):
    service.db.execute.return_value = [("dept-id", 17)]
    acl = await service._build_acl_async("reg", {"Department": 17}, {"Production": "P1"},
                                         {"dept-id": ["Production", "Кабинет СМК"]}, deferred_cabinets={"КАБИНЕТ СМК"})
    assert acl == {"P1": [17]}
    with pytest.raises(ParseError) as caught:
        await service._build_acl_async("reg", {"Department": 17}, {"Production": "P1"},
                                       {"dept-id": ["Production", "Other"]}, deferred_cabinets={"Кабинет СМК"})
    assert caught.value.code == "CABINET_NOT_FOUND"


def test_deferred_cabinets_reject_blank_names():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InitCompanyDTO(id=31, reg="reg", departments=[], deferred_cabinets=[" \u00a0 "])


@pytest.mark.asyncio
@pytest.mark.parametrize('deferred_present', [False, True])
async def test_handle_reports_only_actually_missing_deferred_cabinet(service, monkeypatch, deferred_present):
    from contextlib import asynccontextmanager
    from services.infoboards_service import init_company_service as company

    @asynccontextmanager
    async def lock(_base_url):
        yield

    monkeypatch.setattr(company, 'catalog_operation', lock)
    monkeypatch.setattr(company, 'AuthService', lambda **kwargs: SimpleNamespace(login=AsyncMock(return_value={})))
    service._get_base_url = AsyncMock(return_value='https://catalog')
    service._sync_mapping_table = AsyncMock()
    service._sync_groups = AsyncMock(return_value={'Department': 17})
    service._build_acl_async = AsyncMock(return_value={'P1': [17]})
    service._apply_acl = AsyncMock()
    service.acl_ownership = SimpleNamespace(get=AsyncMock(return_value=({}, {})))
    items = [{'title': 'Production', 'id': 'P1'}]
    if deferred_present:
        items.append({'title': 'Кабинет СМК', 'id': 'P2'})
    payload = {'data': {'board': {'many': items}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    dto = InitCompanyDTO(id=31, reg='reg', departments=[{
        'id': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 'title': 'Department',
        'modules': ['Production', 'Кабинет СМК'],
    }], deferred_cabinets=[' Кабинет\u00a0СМК '])
    result = await service.handle(dto)
    assert result == {'deferred_cabinets': [] if deferred_present else ['Кабинет\u00a0СМК']}
    assert service._apply_acl.await_args.kwargs['preserve_unmentioned_owned'] is not deferred_present
    assert service._build_acl_async.await_args.kwargs['deferred_cabinets'] == {'кабинет смк'}
    board_map = service._build_acl_async.await_args.args[2]
    assert ('Кабинет СМК' in board_map) is deferred_present


@pytest.mark.asyncio
async def test_deferred_allowlist_does_not_hide_unrelated_missing_module_before_writes(service, monkeypatch):
    from contextlib import asynccontextmanager
    from services.infoboards_service import init_company_service as company

    @asynccontextmanager
    async def lock(_base_url):
        yield

    monkeypatch.setattr(company, 'catalog_operation', lock)
    monkeypatch.setattr(company, 'AuthService', lambda **kwargs: SimpleNamespace(login=AsyncMock(return_value={})))
    service._get_base_url = AsyncMock(return_value='https://catalog')
    service._sync_mapping_table = AsyncMock()
    service._sync_groups = AsyncMock()
    service._apply_acl = AsyncMock()
    payload = {'data': {'board': {'many': [{'title': 'Production', 'id': 'P1'}]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    dto = InitCompanyDTO(id=31, reg='reg', departments=[{
        'id': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 'title': 'Department',
        'modules': ['Production', 'Кабинет СМК', 'Another missing cabinet'],
    }], deferred_cabinets=['Кабинет СМК'])
    with pytest.raises(ParseError) as caught:
        await service.handle(dto)
    assert caught.value.code == 'CABINET_NOT_FOUND'
    service._sync_mapping_table.assert_not_awaited()
    service._sync_groups.assert_not_awaited()
    service._apply_acl.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_unmentioned_owned_acl_stops_before_mapping_group_and_acl_writes(service, monkeypatch):
    from contextlib import asynccontextmanager
    from services.infoboards_service import init_company_service as company

    @asynccontextmanager
    async def lock(_base_url):
        yield

    monkeypatch.setattr(company, 'catalog_operation', lock)
    monkeypatch.setattr(company, 'AuthService', lambda **kwargs: SimpleNamespace(login=AsyncMock(return_value={})))
    service._get_base_url = AsyncMock(return_value='https://catalog')
    service._sync_mapping_table = AsyncMock()
    service._sync_groups = AsyncMock()
    service._apply_acl = AsyncMock()
    service.acl_ownership = SimpleNamespace(get=AsyncMock(return_value=({'acl_infoboard_p2': {7}}, {})))
    payload = {'data': {'board': {'many': [{'title': 'Production', 'id': 'P1'}]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    dto = InitCompanyDTO(id=31, reg='reg', departments=[{
        'id': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 'title': 'Department',
        'modules': ['Production', 'Кабинет СМК'],
    }], deferred_cabinets=['Кабинет СМК'])
    with pytest.raises(ParseError) as caught:
        await service.handle(dto)
    assert caught.value.code == 'DEFERRED_ACL_RECONCILIATION_REQUIRED'
    service._sync_mapping_table.assert_not_awaited()
    service._sync_groups.assert_not_awaited()
    service._apply_acl.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_requested_cabinet_stops_before_all_mapping_group_and_acl_writes(service, monkeypatch):
    from contextlib import asynccontextmanager
    from services.infoboards_service import init_company_service as company

    @asynccontextmanager
    async def lock(_base_url):
        yield

    monkeypatch.setattr(company, "catalog_operation", lock)
    monkeypatch.setattr(company, "AuthService", lambda **kwargs: SimpleNamespace(login=AsyncMock(return_value={})))
    service._get_base_url = AsyncMock(return_value="https://catalog")
    service._sync_mapping_table = AsyncMock()
    service._sync_groups = AsyncMock()
    service._apply_acl = AsyncMock()
    payload = {"data": {"board": {"many": [{"title": "Other", "id": "P001"}]}}}
    service.http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
    dto = InitCompanyDTO(id=31, reg="reg", departments=[{
        "id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "title": "Department", "modules": ["Missing"],
    }])
    with pytest.raises(ParseError) as caught:
        await service.handle(dto)
    assert caught.value.code == "CABINET_NOT_FOUND"
    service._sync_mapping_table.assert_not_awaited()
    service._sync_groups.assert_not_awaited()
    service._apply_acl.assert_not_awaited()


DEPARTMENT_ID = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"


def mapping_row(client_id="1"):
    return (1, DEPARTMENT_ID, "Sales", "reg", client_id, "Existing Company", "active", 17, "Sales")


def archived_mapping_row(client_id="24", department_id=DEPARTMENT_ID):
    return (2, department_id, "Previous Department", "reg", client_id, "Previous Company", "archived", None, "Previous Department")


@pytest.mark.asyncio
async def test_missing_department_snapshot_never_defaults_to_archive_all(service):
    dto = InitCompanyDTO(id=1, reg="reg")
    with pytest.raises(ParseError) as caught:
        await service._sync_mapping_table("reg", dto)
    assert caught.value.code == "INCOMPLETE_DEPARTMENT_SNAPSHOT"
    service.db.execute.assert_not_awaited()
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_snapshot_requires_explicit_permission_before_archiving(service):
    service.db.execute.return_value = [mapping_row()]
    with pytest.raises(ParseError) as caught:
        await service._sync_mapping_table("reg", InitCompanyDTO(id=1, reg="reg", departments=[]))
    assert caught.value.code == "EMPTY_DEPARTMENT_SNAPSHOT"
    assert service.db.execute.await_count == 1  # Only the inventory read.
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicitly_allowed_empty_snapshot_can_archive_existing_department(service):
    service.db.execute.return_value = [mapping_row()]
    await service._sync_mapping_table("reg", InitCompanyDTO(id=1, reg="reg", departments=[], allow_empty_snapshot=True))
    assert service.db.execute.await_args.args[1] == {"reg": "reg", "ids": [DEPARTMENT_ID]}
    service.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_empty_company_does_not_require_clear_permission(service):
    service.db.execute.return_value = []
    await service._sync_mapping_table("reg", InitCompanyDTO(id=1, reg="reg", departments=[]))
    assert service.db.execute.await_count == 1
    service.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_registration_cannot_be_reassigned_to_another_company_implicitly(service):
    service.db.execute.return_value = [mapping_row(client_id="2")]
    dto = InitCompanyDTO(id=1, reg="reg", departments=[{"id": DEPARTMENT_ID, "title": "Sales"}])
    with pytest.raises(ParseError) as caught:
        await service._sync_mapping_table("reg", dto)
    assert caught.value.code == "COMPANY_REG_CONFLICT"
    assert service.db.execute.await_count == 1
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_archived_previous_owner_does_not_block_new_active_owner(service):
    new_department = "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    service.db.execute.return_value = [archived_mapping_row()]
    dto = InitCompanyDTO(id=31, reg="reg", departments=[{"id": new_department, "title": "New Department"}])
    await service._sync_mapping_table("reg", dto)
    statements = service.db.execute.await_args_list
    assert len(statements) == 2
    assert "INSERT INTO" in str(statements[1].args[0])
    assert statements[1].args[1]["dept_id"] == new_department
    assert statements[1].args[1]["client_id"] == "31"
    assert statements[1].args[1]["company_name"] == ""  # Never copy the old owner's name.
    service.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_snapshot_with_only_archived_history_never_archives_anything(service):
    service.db.execute.return_value = [archived_mapping_row()]
    await service._sync_mapping_table("reg", InitCompanyDTO(id=31, reg="reg", departments=[]))
    assert service.db.execute.await_count == 1
    service.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_incoming_department_cannot_reactivate_other_company_history(service):
    service.db.execute.return_value = [archived_mapping_row()]
    dto = InitCompanyDTO(id=31, reg="reg", departments=[{"id": DEPARTMENT_ID, "title": "New Department"}])
    with pytest.raises(ParseError) as caught:
        await service._sync_mapping_table("reg", dto)
    assert caught.value.code == "DEPARTMENT_OWNERSHIP_CONFLICT"
    assert service.db.execute.await_count == 1
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_owner_event_remains_blocked_after_new_owner_is_active(service):
    old_department = DEPARTMENT_ID
    new_department = "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    new_active = (3, new_department, "New Department", "reg", "31", "New Company", "active", None, "New Department")
    service.db.execute.return_value = [archived_mapping_row(department_id=old_department), new_active]
    dto = InitCompanyDTO(id=24, reg="reg", departments=[{"id": old_department, "title": "Previous Department"}])
    with pytest.raises(ParseError) as caught:
        await service._sync_mapping_table("reg", dto)
    assert caught.value.code == "COMPANY_REG_CONFLICT"
    assert service.db.execute.await_count == 1
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_company_name_does_not_request_overwrite_with_default_empty_string(service):
    service.db.execute.return_value = [mapping_row()]
    dto = InitCompanyDTO(id=1, reg="reg", departments=[{"id": DEPARTMENT_ID, "title": "Sales"}])
    await service._sync_mapping_table("reg", dto)
    params = service.db.execute.await_args.args[1]
    assert params["has_company_name"] is False
    assert params["company_name"] == "Existing Company"


@pytest.mark.asyncio
async def test_explicit_company_name_update_is_distinguished_from_absence(service):
    service.db.execute.return_value = [mapping_row()]
    dto = InitCompanyDTO(id=1, reg="reg", companyName="New Company", departments=[{"id": DEPARTMENT_ID, "title": "Sales"}])
    await service._sync_mapping_table("reg", dto)
    params = service.db.execute.await_args.args[1]
    assert params["has_company_name"] is True
    assert params["company_name"] == "New Company"
