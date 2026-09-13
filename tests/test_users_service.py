"""Tests for services.users_service.service (UserService with stubs)."""
from __future__ import annotations

import pytest

from services.users_service.dto import (
    CreateUserDTO,
    UpdateUserDTO,
    UpdateUserDepartmentsDTO,
)
from services.users_service.service import UserService
from tests.conftest import StubCatalogClient


@pytest.mark.asyncio
async def test_handle_create_new_user(user_service: UserService, stub_catalog_client: StubCatalogClient) -> None:
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Tetpo", "psw": "123"})
    )
    assert stub_catalog_client.users.get("Tetpo") is not None
    assert stub_catalog_client.users["Tetpo"]["uid"] == "Tetpo"
    assert stub_catalog_client.users["Tetpo"]["psw"] == "123"
    assert stub_catalog_client.post_calls == 1


@pytest.mark.asyncio
async def test_handle_create_then_create_again_idempotent(user_service: UserService, stub_catalog_client: StubCatalogClient) -> None:
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Tetpo", "psw": "123"})
    )
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Tetpo", "psw": "123"})
    )
    assert stub_catalog_client.post_calls_by_uid["Tetpo"] == 1
    assert stub_catalog_client.users["Tetpo"]["psw"] == "123"
    # Second create finds user exists, switches to update path, then skips POST (no changes)


@pytest.mark.asyncio
async def test_handle_update_partial(user_service: UserService, stub_catalog_client: StubCatalogClient) -> None:
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Tetpo", "psw": "123"})
    )
    await user_service.handle_update(
        UpdateUserDTO.model_validate({
            "reg": "350832", "uid": "Tetpo", "psw": "123",
            "mail": "user@example.com",
        })
    )
    assert stub_catalog_client.users["Tetpo"]["mail"] == "user@example.com"
    assert stub_catalog_client.users["Tetpo"].get("grp") == []


@pytest.mark.asyncio
async def test_handle_update_grp_overwrite(user_service: UserService, stub_catalog_client: StubCatalogClient) -> None:
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Tetpo", "psw": "123"})
    )
    await user_service.handle_update(
        UpdateUserDTO.model_validate({
            "reg": "350832", "uid": "Tetpo", "psw": "123",
            "grp": [6],
        })
    )
    assert stub_catalog_client.users["Tetpo"].get("grp") == [6]


@pytest.mark.asyncio
async def test_handle_update_user_not_found_raises(user_service: UserService) -> None:
    from common.exceptions import ParseError
    with pytest.raises(ParseError) as exc_info:
        await user_service.handle_update(
            UpdateUserDTO.model_validate({"reg": "350832", "uid": "Nonexistent", "psw": "123"})
        )
    assert "USER_NOT_FOUND" in str(exc_info.value.code) or "not found" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_handle_update_departments_creates_group_and_assigns(
    user_service: UserService,
    stub_catalog_client: StubCatalogClient,
) -> None:
    stub_catalog_client._groups_map = {"Test Dept": 7}
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "User1", "psw": "123"})
    )
    await user_service.handle_update_departments(
        UpdateUserDepartmentsDTO.model_validate({
            "reg": "350832",
            "id": "User1",
            "departments": [{"id": "d1", "title": "Test Dept"}],
        })
    )
    assert stub_catalog_client.users["User1"].get("grp") == [7]


# --- Группы-дубли главного пользователя не затираются обновлением отделов -------------


@pytest.mark.asyncio
async def test_admin_group_survives_department_update(
    user_service: UserService,
    stub_catalog_client: StubCatalogClient,
) -> None:
    """«Эколог-админ» выдана вне отделов и в payload не приходит — после смены отделов она остаётся."""
    stub_catalog_client._groups_map = {"Эколог": 7, "Эколог-админ": 8, "Новый отдел": 9}
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Boss", "psw": "123", "grp": [7, 8]})
    )

    await user_service.handle_update_departments(
        UpdateUserDepartmentsDTO.model_validate({
            "reg": "350832",
            "id": "Boss",
            "departments": [{"id": "d2", "title": "Новый отдел"}],
        })
    )

    assert stub_catalog_client.users["Boss"].get("grp") == [8, 9]


@pytest.mark.asyncio
async def test_department_group_still_replaced(
    user_service: UserService,
    stub_catalog_client: StubCatalogClient,
) -> None:
    """Обычные группы отделов по-прежнему заменяются целиком — защита не должна их удерживать."""
    stub_catalog_client._groups_map = {"Эколог": 7, "Новый отдел": 9}
    await user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Ivan", "psw": "123", "grp": [7]})
    )

    await user_service.handle_update_departments(
        UpdateUserDepartmentsDTO.model_validate({
            "reg": "350832",
            "id": "Ivan",
            "departments": [{"id": "d2", "title": "Новый отдел"}],
        })
    )

    assert stub_catalog_client.users["Ivan"].get("grp") == [9]


@pytest.mark.asyncio
async def test_empty_suffix_disables_preservation(
    stub_auth_client,
    stub_catalog_client: StubCatalogClient,
    stub_reg_resolver,
) -> None:
    """Пустой суффикс отключает механизм: иначе под правило попали бы все группы подряд."""
    service = UserService(
        auth_client=stub_auth_client,
        catalog_client=stub_catalog_client,
        reg_resolver=stub_reg_resolver,
        admin_group_suffix="",
    )
    stub_catalog_client._groups_map = {"Эколог-админ": 8, "Новый отдел": 9}
    await service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "Zero", "psw": "123", "grp": [8]})
    )

    await service.handle_update_departments(
        UpdateUserDepartmentsDTO.model_validate({
            "reg": "350832",
            "id": "Zero",
            "departments": [{"id": "d2", "title": "Новый отдел"}],
        })
    )

    assert stub_catalog_client.users["Zero"].get("grp") == [9]
