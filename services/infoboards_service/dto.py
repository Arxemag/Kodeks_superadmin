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

from pydantic import BaseModel, Field, field_validator


class CorrectCabinetQuestion(BaseModel):
    """Вопрос опросника с выбранными клиентом ответами."""
    question: str
    answers: list[str]

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question cannot be blank")
        return value


class CorrectCabinetDTO(BaseModel):
    """
    DTO топика correct_cabinet: уточнение кабинета по опроснику.
    userId — пользователь, которому правим (эхо в ответ); reg — рег; link — ссылка на родительский кабинет;
    questions — вопросы с выбранными ответами, включая вариант «Выбрать всё».
    """
    userId: int | None = None
    reg: str
    link: str
    questions: list[CorrectCabinetQuestion]

    @field_validator("reg", mode="before")
    @classmethod
    def _reg_to_str(cls, v: object) -> str:
        return str(v) if v is not None else v


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
    allow_empty_snapshot: bool = False
    deferred_cabinets: list[str] = Field(default_factory=list)

    @field_validator("deferred_cabinets")
    @classmethod
    def validate_deferred_cabinets(cls, titles: list[str]) -> list[str]:
        if any(not title.strip() for title in titles):
            raise ValueError("deferred_cabinets cannot contain blank titles")
        return titles


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
