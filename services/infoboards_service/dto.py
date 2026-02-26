"""
DTO для Kafka init_company.

Все поля обязательны. Формат входящего сообщения:
{
  "id": 67,
  "reg": "123456",
  "companyName": "ООО Ромашка",
  "departments": [
    {
      "id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
      "title": "Отдел продаж",
      "modules": ["Кабинет СМК"]
    }
  ]
}
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class DepartmentPayload(BaseModel):
    """Отдел из Kafka init_company."""
    id: UUID
    title: str
    modules: list[str] = Field(default_factory=list)


class InitCompanyDTO(BaseModel):
    """DTO входящего сообщения init_company. Все поля обязательны."""
    id: int
    reg: str
    companyName: str
    departments: list[DepartmentPayload] = Field(default_factory=list)
