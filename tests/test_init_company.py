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
