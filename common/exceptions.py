"""
Иерархия исключений сервиса.

Обработка: в Auth API все ServiceError перехватываются в app.py и отдаются
клиенту с кодом, сообщением и http_status. В users worker исключения различаются
для retry (NetworkError, AuthError кроме REG_NOT_FOUND) и отправки в DLQ.
"""
from __future__ import annotations


class ServiceError(Exception):
    """
    Базовое исключение: код для ответа/метрик, сообщение и HTTP-статус.
    Инициализация атрибутов через именованные аргументы в __init__.
    """
    code: str = "SERVICE_ERROR"
    message: str = "Service error"
    http_status: int = 500

    def __init__(
        self,
        *,
        code: str | None = None,
        message: str | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message or self.message)
        if code is not None:
            self.code = code
        if message is not None:
            self.message = message
        if http_status is not None:
            self.http_status = http_status


class ConfigError(ServiceError):
    """Ошибка конфигурации (например, пустой DB_URL или неверный .env)."""
    code = "CONFIG_ERROR"
    message = "Invalid configuration"
    http_status = 500


class DatabaseError(ServiceError):
    """Ошибка при работе с БД (healthcheck или запрос)."""
    code = "DATABASE_ERROR"
    message = "Database error"
    http_status = 500


class NetworkError(ServiceError):
    """Сетевые ошибки, таймауты, 5xx каталога; в worker — retry, затем DLQ."""
    code = "NETWORK_ERROR"
    message = "Network error"
    http_status = 502


class AuthError(ServiceError):
    """Ошибка авторизации (reg не найден, отказ каталога). REG_NOT_FOUND в worker идёт в DLQ без retry."""
    code = "AUTH_ERROR"
    message = "Authentication error"
    http_status = 401


class ParseError(ServiceError):
    """Ошибка разбора (HTML, JSON); в worker — в DLQ без retry."""
    code = "PARSE_ERROR"
    message = "Parse error"
    http_status = 422

