"""
DTO сообщений Kafka и утилита маскировки секретов в логах.

Инициализация: модели валидируются в worker при разборе payload (model_validate).
CreateUserDTO/UpdateUserDTO — топики create-user и update-user; UpdateUserDepartmentsDTO — update-user-departments.
redact_payload используется при логировании payload в worker и скриптах.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DepartmentDTO(BaseModel):
    """Один отдел в списке departments: id и title."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)


class CreateUserDTO(BaseModel):
    """Сообщение топика create-user: reg, uid, psw обязательны; остальные поля и grp опциональны. Лишние ключи игнорируются."""
    model_config = ConfigDict(extra="ignore")

    reg: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    psw: str = Field(..., min_length=1)

    name: str | None = None
    org: str | None = None
    pos: str | None = None
    mail: str | None = None
    telephon: str | None = None
    end: str | None = None
    grp: list[int] | None = None
    # Omission preserves legacy group semantics; an explicit empty snapshot
    # requests no department access. Explicit null is not a snapshot.
    departments: list[DepartmentDTO] | None = None

    @field_validator("departments")
    @classmethod
    def reject_null_departments(cls, value):
        if value is None:
            raise ValueError("Omit departments for legacy semantics; null is not a department snapshot")
        return value

    @field_validator("grp")
    @classmethod
    def validate_grp(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        normalized: list[int] = []
        seen: set[int] = set()
        for raw in value:
            if raw < 0:
                raise ValueError("grp values must be non-negative integers")
            if raw in seen:
                continue
            seen.add(raw)
            normalized.append(raw)
        return normalized


class UpdateUserDTO(CreateUserDTO):
    """Сообщение топика update-user: наследует CreateUserDTO, дополнительно опциональное поле id для POST обновления."""
    id: str | None = None
    psw: str | None = Field(None, min_length=1)

    @field_validator("psw")
    @classmethod
    def reject_null_password(cls, value):
        if value is None:
            raise ValueError("Omit psw to preserve the existing password; null is not a password")
        return value


class UpdateUserDepartmentsDTO(BaseModel):
    """Сообщение топика update-user-departments: reg, id пользователя и непустой список departments."""
    model_config = ConfigDict(extra="ignore")

    reg: str = Field(..., min_length=1)
    id: str = Field(..., min_length=1)
    departments: list[DepartmentDTO]


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Возвращает копию payload с замаскированными полями psw и mail для безопасного логирования."""
    redacted = dict(payload)
    if "psw" in redacted:
        redacted["psw"] = "***"
    if "mail" in redacted:
        redacted["mail"] = "***"
    return redacted
