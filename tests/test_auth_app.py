"""Tests for Auth API (FastAPI) with mocked DB and config."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import Settings, get_settings
from services.auth_service.app import create_app


@pytest.fixture
def mock_settings() -> Settings:
    """Полный mock настроек для тестов Auth API и воркеров."""
    return Settings.model_construct(
        DB_URL="postgresql+asyncpg://u:p@localhost:5432/testdb",
        PG_HOST="",
        PG_PORT=5432,
        PG_USERNAME="",
        PG_PASSWORD="",
        PG_DATABASE="",
        POOL_SIZE=1,
        POOL_TIMEOUT=5,
        ADMIN_LOGIN="admin",
        ADMIN_PASSWORD="secret",
        HTTP_TIMEOUT=10.0,
        LOG_LEVEL="INFO",
        PORT=8000,
        AUTH_SERVICE_URL="http://127.0.0.1:8000",
        KAFKA_BROKER="",
        KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:9092",
        KAFKA_GROUP_ID="superadmin-workers",
        KAFKA_CREATE_TOPIC="create-user",
        KAFKA_UPDATE_TOPIC="update-user",
        KAFKA_UPDATE_DEPARTMENTS_TOPIC="update-user-departments",
        KAFKA_DLQ_TOPIC="users-dlq",
        KAFKA_MAX_CONCURRENCY=200,
        KAFKA_POLL_TIMEOUT_MS=500,
        KAFKA_MAX_BATCH=500,
        USERS_RETRY_ATTEMPTS=3,
        USERS_RETRY_BASE_DELAY=0.2,
        USERS_RETRY_MAX_DELAY=3.0,
        USERS_METRICS_PORT=9101,
        KAFKA_INIT_COMPANY_TOPIC="init_company",
        KAFKA_INIT_COMPANY_DLQ_TOPIC="init_company-dlq",
        KAFKA_INIT_COMPANY_GROUP_ID="infoboards-init-company",
        INIT_COMPANY_METRICS_PORT=9102,
        KAFKA_ENABLE_REG_COMPANY_TOPIC="enable_reg_company",
        KAFKA_DISABLE_REG_COMPANY_TOPIC="disable_reg_company",
        KAFKA_REG_COMPANY_GROUP_ID="reg-company",
        REG_COMPANY_METRICS_PORT=9103,
        UNIFIED_WORKER_METRICS_PORT=9100,
        AUTH_HARD_TIMEOUT=2.0,
        AUTH_HARD_TIMEOUT_DEBUG=20.0,
    )


@pytest.fixture
def mock_db_session() -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    return session


@pytest.fixture
def auth_client(mock_settings: Settings, mock_db_session: MagicMock) -> TestClient:
    get_settings.cache_clear()
    with (
        patch("services.auth_service.app.get_settings", return_value=mock_settings),
        patch("common.db.healthcheck_db", new_callable=AsyncMock),
        patch("common.db.shutdown_db", new_callable=AsyncMock),
        patch("common.http.shutdown_http", new_callable=AsyncMock),
    ):
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: mock_settings
        with TestClient(app) as client:
            yield client


def test_health(auth_client: TestClient) -> None:
    r = auth_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_metrics(auth_client: TestClient) -> None:
    r = auth_client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")


# Рег, которого нет в БД (в реальной базе есть, например, 350832).
REG_NOT_IN_DB = "999999"


def test_login_reg_not_found(auth_client: TestClient) -> None:
    """Проверяем, что при AuthError(REG_NOT_FOUND) API возвращает 404 (без реального get_db)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from common.exceptions import AuthError, ServiceError
    from services.auth_service.schemas import ErrorResponse

    app = FastAPI()

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: object, exc: ServiceError):
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(code=exc.code, message=exc.message).model_dump(),
        )

    @app.post("/api/expert/reg/{reg}")
    async def login(reg: str) -> None:
        raise AuthError(code="REG_NOT_FOUND", message="reg отсутствует в БД", http_status=404)

    with TestClient(app) as client:
        r = client.post(f"/api/expert/reg/{REG_NOT_IN_DB}")
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    data = r.json()
    assert data.get("code") == "REG_NOT_FOUND"
    assert data.get("status") == "error"
