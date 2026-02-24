"""Tests for services.users_service.dto."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.users_service.dto import (
    CreateUserDTO,
    DepartmentDTO,
    UpdateUserDTO,
    UpdateUserDepartmentsDTO,
    redact_payload,
)


def test_create_user_dto_minimal() -> None:
    d = CreateUserDTO.model_validate({"reg": "350832", "uid": "Tetpo", "psw": "123"})
    assert d.reg == "350832"
    assert d.uid == "Tetpo"
    assert d.psw == "123"
    assert d.name is None
    assert d.grp is None


def test_create_user_dto_with_optional() -> None:
    d = CreateUserDTO.model_validate({
        "reg": "1", "uid": "u", "psw": "p",
        "name": "John", "mail": "a@b.ru", "grp": [1, 2, 2],
    })
    assert d.name == "John"
    assert d.mail == "a@b.ru"
    assert d.grp == [1, 2]


def test_create_user_dto_grp_negative_invalid() -> None:
    with pytest.raises(ValidationError):
        CreateUserDTO.model_validate({"reg": "1", "uid": "u", "psw": "p", "grp": [-1]})


def test_create_user_dto_extra_ignored() -> None:
    d = CreateUserDTO.model_validate({"reg": "1", "uid": "u", "psw": "p", "unknown": "x"})
    assert not hasattr(d, "unknown") or getattr(d, "unknown", None) != "x"


def test_update_user_dto_has_id() -> None:
    d = UpdateUserDTO.model_validate({"reg": "1", "uid": "u", "psw": "p"})
    assert d.id is None
    d2 = UpdateUserDTO.model_validate({"reg": "1", "uid": "u", "psw": "p", "id": "id1"})
    assert d2.id == "id1"


def test_department_dto() -> None:
    d = DepartmentDTO.model_validate({"id": "d1", "title": "Dept"})
    assert d.id == "d1"
    assert d.title == "Dept"


def test_update_user_departments_dto() -> None:
    d = UpdateUserDepartmentsDTO.model_validate({
        "reg": "350832",
        "id": "user1",
        "departments": [{"id": "g1", "title": "Group A"}],
    })
    assert d.reg == "350832"
    assert d.id == "user1"
    assert len(d.departments) == 1
    assert d.departments[0].title == "Group A"


def test_update_user_departments_empty_invalid() -> None:
    with pytest.raises(ValidationError):
        UpdateUserDepartmentsDTO.model_validate({
            "reg": "1", "id": "u",
            "departments": [],
        })


def test_redact_payload() -> None:
    out = redact_payload({"reg": "1", "psw": "secret", "mail": "a@b.ru"})
    assert out["psw"] == "***"
    assert out["mail"] == "***"
    assert out["reg"] == "1"


def test_redact_payload_no_secrets() -> None:
    out = redact_payload({"reg": "1", "uid": "u"})
    assert "psw" not in out or out.get("psw") != "***"
    assert out["uid"] == "u"
