"""
Auth API: FastAPI-приложение для логина в каталог и эндпоинта инфобордов.

Инициализация: create_app() создаёт приложение с lifespan (старт/стоп), middleware для trace_id
и метрик, обработчиками исключений и маршрутами. app = create_app() — точка входа для uvicorn.
Обработка: эндпоинты получают db и settings через Depends; логин и инфоборды делегируют в сервисы.
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from common import COMMON_VERSION
from common.config import Settings, get_settings
from common.db import get_db, healthcheck_db, shutdown_db
from common.exceptions import ServiceError
from common.http import shutdown_http
from common.logger import get_logger, get_trace_id, set_trace_id
from services.auth_service.metrics import active_requests, auth_errors_total
from services.auth_service.schemas import ErrorResponse, LoginRequest, OkResponse
from services.auth_service.service import AuthService
from services.infoboards_service.schemas import (
    ErrorResponse as InfoboardsErrorResponse,
    InfoboardLinkResponse,
    InfoboardsListResponse,
)
from services.infoboards_service.service import InfoboardsService


logger = get_logger("auth")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Жизненный цикл приложения: при старте проверяем конфиг (get_settings) и пишем лог,
    при завершении — закрываем HTTP-транспорт и пул БД.
    """
    _ = get_settings()
    logger.info(f"service started (common={COMMON_VERSION})")
    yield
    await shutdown_http()
    await shutdown_db()


def create_app() -> FastAPI:
    """Создаёт и возвращает экземпляр FastAPI с маршрутами и обработчиками ошибок."""
    app = FastAPI(title="Auth Service", lifespan=lifespan)

    @app.middleware("http")
    async def trace_and_metrics_mw(request: Request, call_next):
        """Для каждого запроса: увеличиваем счётчик активных запросов, выставляем trace_id, замеряем время, прокидываем trace_id в ответ."""
        active_requests.inc()
        try:
            incoming = request.headers.get("X-Trace-Id")
            set_trace_id(incoming or str(uuid.uuid4()))
            start = time.perf_counter()
            resp = await call_next(request)
            resp.headers["X-Trace-Id"] = get_trace_id()
            resp.headers["X-Elapsed-Ms"] = f"{(time.perf_counter() - start) * 1000.0:.2f}"
            return resp
        finally:
            active_requests.dec()

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError):
        """Все исключения-наследники ServiceError отдаются клиенту с exc.http_status и JSON {code, message}; метрика auth_errors_total."""
        logger.error(f"{exc.code}: {exc.message}")
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(code=exc.code, message=exc.message).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        """Ошибки валидации FastAPI (path/query/body) — ответ 422 и VALIDATION_ERROR."""
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(code="VALIDATION_ERROR", message=str(exc)).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception):
        """Любое необработанное исключение — лог с трейсбеком и ответ 500 INTERNAL_ERROR."""
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(code="INTERNAL_ERROR", message="Internal error").model_dump(),
        )

    @app.get("/health")
    async def health():
        """Проверка живости: выполняется healthcheck_db(); при успехе — 200 и {"status": "ok"}."""
        await healthcheck_db()
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Эндпоинт Prometheus: отдаёт сгенерированные метрики в текстовом формате."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/expert/reg/{reg}", response_model=OkResponse,
              responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def login(
            reg: str,
            name: str | None = None,
            db: AsyncSession = Depends(get_db),
            settings: Settings = Depends(get_settings),
    ):
        """Логин в каталог по reg: без name — куки админа, с name — куки пользователя (пароль забирается через админа). Обработка в AuthService.login."""
        service = AuthService(db=db, settings=settings)
        cookies = await service.login(reg=reg, name=name)
        return OkResponse(cookies=cookies)

    @app.get(
        "/api/infoboards/link",
        response_model=InfoboardLinkResponse | InfoboardsListResponse,
        responses={404: {"model": InfoboardsErrorResponse}, 422: {"model": InfoboardsErrorResponse}},
    )
    async def get_infoboard_link(
        reg: str,
        title: str | None = None,
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
    ):
        """Ссылка на инфоборд: по reg берётся base_url, выполняется GraphQL к каталогу; при title — один кабинет, без title — список. Обработка в InfoboardsService."""
        service = InfoboardsService(db=db, settings=settings)
        data = await service.get_link_by_reg_and_title(reg=reg, title=title)
        if "items" in data:
            return InfoboardsListResponse(**data)
        return InfoboardLinkResponse(**data)

    return app


# Экземпляр приложения для запуска через uvicorn (main.py)
app = create_app()
