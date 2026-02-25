"""
Подключение к PostgreSQL и сессии.

Один пул на процесс: движок и фабрика сессий создаются при первом обращении к get_db(),
get_db_session() или healthcheck_db() через _get_engine(). Освобождение — shutdown_db()
(Auth API lifespan или воркеры при завершении).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from common.config import get_settings
from common.exceptions import DatabaseError


# Глобальные объекты: движок и фабрика сессий инициализируются лениво в _get_engine()
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    """
    Создаёт и возвращает движок БД при первом вызове; при последующих возвращает тот же экземпляр.
    Также инициализирует _sessionmaker для выдачи сессий в get_db().
    """
    global _engine, _sessionmaker
    if _engine is not None and _sessionmaker is not None:
        return _engine

    settings = get_settings()
    _engine = create_async_engine(
        settings.DB_URL,
        pool_size=settings.POOL_SIZE,
        pool_timeout=settings.POOL_TIMEOUT,
        pool_pre_ping=True,
        future=True,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_db() -> AsyncIterator[AsyncSession]:
    """
    Dependency для FastAPI: выдаёт одну сессию на запрос. После выхода из контекста сессия закрывается.
    Используется в эндпоинтах Auth API (login, infoboards).
    """
    global _sessionmaker
    _get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        yield session


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """
    Контекстный менеджер сессии БД для воркеров и скриптов.
    Использует тот же пул, что и get_db().
    """
    _get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        yield session


async def healthcheck_db() -> None:
    """
    Проверка доступности БД: выполняется простой запрос SELECT 1.
    Вызывается из эндпоинта /health; при ошибке выбрасывается DatabaseError.
    """
    try:
        engine = _get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:  # pragma: no cover
        raise DatabaseError(message="DB healthcheck failed") from e


async def shutdown_db() -> None:
    """
    Освобождение пула соединений и сброс глобальных _engine и _sessionmaker.
    Вызывается при завершении приложения (lifespan Auth API).
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None

