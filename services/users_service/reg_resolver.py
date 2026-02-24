"""
Разрешение reg -> base_url через БД (таблица reg_services).

Инициализация: RegResolver создаётся в worker с DB_URL, pool_size, pool_timeout; startup() создаёт
движок и sessionmaker. shutdown() освобождает пул. resolve_base_url вызывается при обработке каждого сообщения.
with_session() — контекстный менеджер сессии (для init_company и др.).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from common.exceptions import AuthError


@dataclass
class RegResolver:
    """Отдельный пул БД для воркера: по reg возвращает base_url каталога из reg_services."""
    db_url: str
    pool_size: int
    pool_timeout: int
    _engine: AsyncEngine | None = None
    _sessionmaker: async_sessionmaker | None = None

    async def startup(self) -> None:
        """Создаёт движок и фабрику сессий при первом вызове; повторные вызовы не делают ничего."""
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            self.db_url,
            pool_size=self.pool_size,
            pool_timeout=self.pool_timeout,
            pool_pre_ping=True,
            future=True,
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    async def shutdown(self) -> None:
        """Освобождает пул соединений и сбрасывает _engine и _sessionmaker."""
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None

    async def resolve_base_url(self, reg: str) -> str:
        """Читает base_url из reg_services по reg_number; при отсутствии — AuthError REG_NOT_FOUND. Возвращает URL без завершающего слэша."""
        if self._sessionmaker is None:
            raise RuntimeError("RegResolver is not started")
        async with self._sessionmaker() as session:
            result = await session.execute(
                text("SELECT base_url FROM reg_services WHERE reg_number = :reg"),
                {"reg": reg},
            )
            value = result.scalar_one_or_none()
            if not value:
                raise AuthError(code="REG_NOT_FOUND", message=f"reg '{reg}' not found", http_status=404)
            return str(value).rstrip("/")

    @asynccontextmanager
    async def with_session(self) -> AsyncIterator[AsyncSession]:
        """Контекстный менеджер сессии БД для init_company и др."""
        if self._sessionmaker is None:
            raise RuntimeError("RegResolver is not started")
        async with self._sessionmaker() as session:
            yield session
