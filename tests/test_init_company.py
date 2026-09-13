"""Tests for init_company DTO and service (with mocks)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from services.infoboards_service.dto import DepartmentPayload, InitCompanyDTO, is_missing_department_id_error


def test_init_company_dto() -> None:
    """Валидация InitCompanyDTO."""
    dto = InitCompanyDTO(
        id=67,
        reg="123456",
        companyName="ООО Ромашка",
        departments=[
            DepartmentPayload(
                id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
                title="Отдел продаж",
                modules=["Кабинет СМК"],
            ),
        ],
    )
    assert dto.reg == "123456"
    assert dto.companyName == "ООО Ромашка"
    assert len(dto.departments) == 1
    assert dto.departments[0].title == "Отдел продаж"
    assert dto.departments[0].modules == ["Кабинет СМК"]


def test_init_company_dto_empty_departments() -> None:
    """Пустой список departments допустим."""
    dto = InitCompanyDTO(id=1, reg="x", companyName="Y", departments=[])
    assert dto.departments == []


def test_init_company_dto_optional_company_name() -> None:
    """companyName опционален (sync_departments payload без companyName)."""
    dto = InitCompanyDTO(id=1, reg="x", departments=[])
    assert dto.companyName == ""


def test_department_payload_default_modules() -> None:
    """modules по умолчанию — пустой список."""
    dept = DepartmentPayload(id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), title="Dept")
    assert dept.modules == []


def test_is_missing_department_id_error() -> None:
    """Ошибка валидации из-за отсутствия id в departments распознаётся."""
    try:
        InitCompanyDTO(id=1, reg="x", companyName="Y", departments=[{"title": "Dept", "modules": []}])
    except ValidationError as e:
        assert is_missing_department_id_error(e) is True
    else:
        pytest.fail("Expected ValidationError")


# --- Группа-дубль главного пользователя (mainUser) ------------------------------------


def test_department_payload_main_user_optional() -> None:
    """mainUser необязателен: без него payload разбирается как раньше."""
    dept = DepartmentPayload(id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), title="Эколог")
    assert dept.mainUser is None
    with_main = DepartmentPayload(
        id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), title="Эколог", mainUser="boss"
    )
    assert with_main.mainUser == "boss"


def _service_with_stubs(groups: dict[str, int], user_html: str | None):
    """InitCompanyService с заглушками БД и каталога — проверяем только логику групп-дублей."""
    from common.config import DEFAULT_ADMIN_GROUP_SUFFIX
    from services.infoboards_service.init_company_service import InitCompanyService

    catalog = MagicMock()
    catalog.create_group = AsyncMock()
    catalog.get_groups_page = AsyncMock(return_value="")
    catalog.get_user_page = AsyncMock(return_value=user_html)
    catalog.post_user = AsyncMock()

    settings = MagicMock()
    settings.ADMIN_GROUP_SUFFIX = DEFAULT_ADMIN_GROUP_SUFFIX

    service = InitCompanyService(
        db=MagicMock(), settings=settings, http_client=MagicMock(), catalog_client=catalog
    )
    return service, catalog


def _user_page(uid: str, groups: list[int]) -> str:
    fields = f'<input name="uid" value="{uid}"><input name="psw" value="p"><input name="id" value="{uid}">'
    checks = "".join(f'<input type="checkbox" name="grp" value="{g}" checked>' for g in groups)
    return f"<html><body>{fields}{checks}</body></html>"


@pytest.mark.asyncio
async def test_admin_group_created_and_assigned(monkeypatch) -> None:
    """Для отдела с mainUser создаётся «Эколог-админ» и выдаётся пользователю поверх текущих групп."""
    import services.infoboards_service.init_company_service as mod

    service, catalog = _service_with_stubs({}, _user_page("boss", [7]))
    # после create_group каталог начинает отдавать новую группу
    monkeypatch.setattr(mod, "parse_groups_catalog", lambda html: {"Эколог": 7, "Эколог-админ": 8})

    dto = InitCompanyDTO(
        id=1, reg="465302", companyName="X",
        departments=[DepartmentPayload(
            id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), title="Эколог", mainUser="boss",
        )],
    )
    groups = await service._sync_admin_groups("https://host", {}, dto, {"Эколог": 7})

    catalog.create_group.assert_awaited_once_with("https://host", "Эколог-админ", {})
    assert groups["Эколог-админ"] == 8
    sent = dict(catalog.post_user.await_args.args[1])
    assert [v for k, v in catalog.post_user.await_args.args[1] if k == "grp"] == ["7", "8"]
    assert sent["uid"] == "boss"


@pytest.mark.asyncio
async def test_admin_group_skipped_without_main_user() -> None:
    """Без mainUser ничего не создаётся и пользователь не трогается."""
    service, catalog = _service_with_stubs({}, None)
    dto = InitCompanyDTO(
        id=1, reg="465302", companyName="X",
        departments=[DepartmentPayload(
            id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), title="Эколог",
        )],
    )
    groups = await service._sync_admin_groups("https://host", {}, dto, {"Эколог": 7})

    catalog.create_group.assert_not_awaited()
    catalog.post_user.assert_not_awaited()
    assert groups == {"Эколог": 7}


@pytest.mark.asyncio
async def test_admin_group_missing_user_does_not_fail(monkeypatch) -> None:
    """Главный пользователь ещё не заведён в каталоге — init_company не падает."""
    import services.infoboards_service.init_company_service as mod

    service, catalog = _service_with_stubs({}, None)
    monkeypatch.setattr(mod, "parse_groups_catalog", lambda html: {"Эколог-админ": 8})

    dto = InitCompanyDTO(
        id=1, reg="465302", companyName="X",
        departments=[DepartmentPayload(
            id=UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), title="Эколог", mainUser="ghost",
        )],
    )
    await service._sync_admin_groups("https://host", {}, dto, {})

    catalog.post_user.assert_not_awaited()
