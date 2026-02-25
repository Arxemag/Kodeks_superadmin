"""
Конфигурация приложения.

Чтение переменных окружения из .env, совместимость с форматом фронта (PG_*, KAFKA_*)
и legacy-форматом (DB_URL, KAFKA_BOOTSTRAP_SERVERS). Инициализация настроек происходит
при первом вызове get_settings(); экземпляр кэшируется.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.exceptions import ConfigError


class Settings(BaseSettings):
    """
    Настройки сервиса. Загружаются из .env при создании экземпляра.
    Лишние переменные в .env игнорируются (extra="ignore").
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Подключение к БД (инициализация пула в common.db) ---
    DB_URL: str = Field("", description="Async PostgreSQL URL (SQLAlchemy format)")
    PG_HOST: str = Field("", description="PostgreSQL host")
    PG_PORT: int = Field(5432, description="PostgreSQL port")
    PG_USERNAME: str = Field("", description="PostgreSQL username")
    PG_PASSWORD: str = Field("", description="PostgreSQL password")
    PG_DATABASE: str = Field("", description="PostgreSQL database name")
    POOL_SIZE: int = Field(10, description="DB pool size")
    POOL_TIMEOUT: int = Field(30, description="DB pool timeout seconds")

    # --- Учётные данные каталога (используются в auth_service при логине) ---
    ADMIN_LOGIN: str = Field(..., description="Catalog admin login")
    ADMIN_PASSWORD: str = Field(..., description="Catalog admin password")

    HTTP_TIMEOUT: float = Field(10.0, description="HTTP timeout seconds")
    LOG_LEVEL: str = Field("INFO", description="Log level")

    PORT: int = Field(8000, description="HTTP server port (Auth API)")
    AUTH_SERVICE_URL: str = Field("http://127.0.0.1:8000", description="Auth service base URL")

    # --- Kafka (единый consumer в unified_worker или отдельные воркеры) ---
    KAFKA_BROKER: str = Field("", description="Kafka broker address")
    KAFKA_BOOTSTRAP_SERVERS: str = Field("", description="Kafka bootstrap servers")
    # Одна группа на все топики при использовании unified worker (main_unified_worker.py)
    KAFKA_GROUP_ID: str = Field("superadmin-workers", description="Kafka consumer group id (unified worker)")
    KAFKA_CREATE_TOPIC: str = Field("create-user", description="Kafka topic for user creation")
    KAFKA_UPDATE_TOPIC: str = Field("update-user", description="Kafka topic for user updates")
    KAFKA_UPDATE_DEPARTMENTS_TOPIC: str = Field(
        "update-user-departments",
        description="Kafka topic for user departments update",
    )
    KAFKA_DLQ_TOPIC: str = Field("users-dlq", description="Kafka dead-letter queue topic")
    KAFKA_MAX_CONCURRENCY: int = Field(200, description="Max in-flight kafka jobs")
    KAFKA_POLL_TIMEOUT_MS: int = Field(500, description="Kafka poll timeout (ms)")
    KAFKA_MAX_BATCH: int = Field(500, description="Max records per poll")

    USERS_RETRY_ATTEMPTS: int = Field(3, description="Retry attempts for retryable errors")
    USERS_RETRY_BASE_DELAY: float = Field(0.2, description="Retry base delay in seconds")
    USERS_RETRY_MAX_DELAY: float = Field(3.0, description="Retry max delay in seconds")
    USERS_METRICS_PORT: int = Field(9101, description="Prometheus metrics port for users worker")

    # --- INFOBOARDS init_company Kafka worker ---
    KAFKA_INIT_COMPANY_TOPIC: str = Field(
        "init_company",
        description="Kafka topic for company initialization (departments, groups, ACL)",
    )
    KAFKA_INIT_COMPANY_DLQ_TOPIC: str = Field(
        "init_company-dlq",
        description="Kafka DLQ topic for init_company",
    )
    KAFKA_INIT_COMPANY_GROUP_ID: str = Field(
        "infoboards-init-company",
        description="Kafka consumer group for init_company",
    )
    INIT_COMPANY_METRICS_PORT: int = Field(
        9102,
        description="Prometheus metrics port for init_company worker",
    )

    # --- Reg company (enable/disable) Kafka worker — пока только приём и лог ---
    KAFKA_ENABLE_REG_COMPANY_TOPIC: str = Field(
        "enable_reg_company",
        description="Kafka topic: включение рега (reg, oldReg, companyName)",
    )
    KAFKA_DISABLE_REG_COMPANY_TOPIC: str = Field(
        "disable_reg_company",
        description="Kafka topic: выключение рега (reg, companyName)",
    )
    KAFKA_REG_COMPANY_GROUP_ID: str = Field(
        "reg-company",
        description="Kafka consumer group for enable/disable reg company",
    )
    REG_COMPANY_METRICS_PORT: int = Field(
        9103,
        description="Prometheus metrics port for reg_company worker",
    )

    # Единый воркер (одна группа, все топики)
    UNIFIED_WORKER_METRICS_PORT: int = Field(
        9100,
        description="Prometheus metrics port for unified Kafka worker",
    )

    # Жёсткий таймаут на операцию логина (секунды).
    AUTH_HARD_TIMEOUT: float = Field(2.0, description="Hard timeout for auth/login operation seconds")
    AUTH_HARD_TIMEOUT_DEBUG: float = Field(
        20.0,
        description="Hard timeout for auth/login operation in DEBUG (seconds)",
    )

    @model_validator(mode="after")
    def _resolve_compat_values(self) -> "Settings":
        """
        Совместимость форматов: подставляем KAFKA_BOOTSTRAP_SERVERS из KAFKA_BROKER,
        собираем DB_URL из PG_* при отсутствии DB_URL. Проверка непустого DB_URL — fail-fast при старте.
        """
        if not self.KAFKA_BOOTSTRAP_SERVERS:
            self.KAFKA_BOOTSTRAP_SERVERS = self.KAFKA_BROKER or "127.0.0.1:9092"

        if not self.DB_URL:
            parts = [self.PG_HOST, self.PG_USERNAME, self.PG_PASSWORD, self.PG_DATABASE]
            if all(parts):
                self.DB_URL = (
                    f"postgresql+asyncpg://{self.PG_USERNAME}:{self.PG_PASSWORD}"
                    f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"
                )
        if not (self.DB_URL or "").strip():
            raise ValueError(
                "DB_URL is empty; set DB_URL or all of PG_HOST, PG_USERNAME, PG_PASSWORD, PG_DATABASE"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Возвращает единственный экземпляр настроек (кэш по одному объекту).
    Инициализация при первом вызове; при ошибке валидации .env выбрасывается ConfigError.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as e:  # pragma: no cover
        raise ConfigError(message=str(e)) from e

