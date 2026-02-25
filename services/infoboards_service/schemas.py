"""
Схемы ответов эндпоинта GET /api/expert/infoboards/link.

InfoboardLinkResponse — один кабинет (когда передан title); InfoboardsListResponse — список кабинетов
(когда title не передан). ErrorResponse — тело ошибки 404/422 для OpenAPI.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class InfoboardItem(BaseModel):
    """Один элемент списка кабинетов: id, title, link."""
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    link: str = Field(..., min_length=1)


class InfoboardLinkResponse(BaseModel):
    """Ответ с одним кабинетом (при запросе с title)."""
    status: str = "ok"
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    link: str = Field(..., min_length=1)


class InfoboardsListResponse(BaseModel):
    """Ответ со списком кабинетов (при запросе без title)."""
    status: str = "ok"
    items: list[InfoboardItem]


class ErrorResponse(BaseModel):
    """Тело ошибки для 404/422 в документации эндпоинта."""
    status: str = "error"
    code: str
    message: str
