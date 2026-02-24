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
