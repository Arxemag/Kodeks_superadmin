"""
Схемы запросов и ответов Auth API.

Используются в app.py: OkResponse — успешный ответ логина (куки), ErrorResponse — тело ошибки
для всех обработчиков исключений. LoginRequest объявлен для контракта (эндпоинт принимает reg и name из path/query).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from common.types import Cookies


class LoginRequest(BaseModel):
    """Параметры логина: reg обязателен, name опционален (при наличии — логин под пользователем)."""
    reg: str = Field(..., min_length=1)
    name: str | None = Field(default=None, min_length=1)


class OkResponse(BaseModel):
    """Успешный ответ POST /api/expert/reg/{reg} (опционально ?name=): статус ok и словарь куков каталога."""
    status: str = "ok"
    cookies: Cookies


class ErrorResponse(BaseModel):
    """Тело ошибки для всех исключений API: status error, код и сообщение."""
    status: str = "error"
    code: str
    message: str
