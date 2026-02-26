"""
DTO для Kafka init_company.

Формат входящего сообщения:
{
  "id": 67,
  "reg": "123456",
  "companyName": "ООО Ромашка",
  "departments": [
    {
      "id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
      "title": "TEST",
      "modules": ["Заведующий лабораторией", "Кабинет СМК"]
    }
  ]
}
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class DepartmentPayload(BaseModel):
    """Отдел из Kafka init_company.
    title — название группы (например TEST);
    modules — названия кабинетов.
    id обязателен (UUID) для каждого отдела.
    """
    id: UUID
    title: str  # Название группы
    modules: list[str] = Field(default_factory=list)  # Названия кабинетов


class InitCompanyDTO(BaseModel):
    """DTO входящего сообщения init_company / sync_departments. id, reg обязательны; companyName опционален (sync_departments)."""
    id: int
    reg: str
    companyName: str = ""
    departments: list[DepartmentPayload] = Field(default_factory=list)


def is_missing_department_id_error(e: BaseException) -> bool:
    """Проверяет, что ошибка валидации — отсутствие id в departments[]."""
    from pydantic import ValidationError
    if not isinstance(e, ValidationError):
        return False
    for err in e.errors():
        loc = err.get("loc", ())
        if len(loc) >= 3 and loc[0] == "departments" and loc[2] == "id" and err.get("type") == "missing":
            return True
    return False
