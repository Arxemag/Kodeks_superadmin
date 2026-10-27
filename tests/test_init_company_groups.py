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


DEPARTMENT_ID = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"


def mapping_row(client_id="1"):
    return (1, DEPARTMENT_ID, "Sales", "reg", client_id, "Existing Company", "active", 17, "Sales")


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
