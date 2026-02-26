"""
Разрешение reg -> base_url через БД (таблица reg_services).

Использует общий пул БД (common.db). startup() инициализирует пул при первом обращении;
shutdown() вызывает common.shutdown_db(). with_session() — контекстный менеджер сессии для init_company и др.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import get_settings
from common.db import get_db_session, shutdown_db
from common.exceptions import AuthError


class RegResolver:
    """По reg возвращает base_url каталога из таблицы (DB_TABLE_REG_SERVICES). Пул БД — общий (common.db)."""

    async def startup(self) -> None:
        """Инициализирует общий пул БД при первом обращении (ленивая инициализация в get_db_session)."""
        from common.db import _get_engine
        _get_engine()

    async def shutdown(self) -> None:
        """Освобождает общий пул БД."""
        await shutdown_db()

    async def resolve_base_url(self, reg: str) -> str:
        """Читает base_url из таблицы (DB_TABLE_REG_SERVICES) по reg_number; при отсутствии — AuthError REG_NOT_FOUND."""
        settings = get_settings()
        tbl = settings.DB_TABLE_REG_SERVICES
        async with get_db_session() as session:
            result = await session.execute(
                text(f"SELECT base_url FROM {tbl} WHERE reg_number = :reg"),
                {"reg": reg},
            )
            value = result.scalar_one_or_none()
            if not value:
                raise AuthError(code="REG_NOT_FOUND", message=f"reg '{reg}' not found", http_status=404)
            return str(value).rstrip("/")

    @asynccontextmanager
    async def with_session(self) -> AsyncIterator[AsyncSession]:
        """Контекстный менеджер сессии БД для init_company и др."""
        async with get_db_session() as session:
            yield session
