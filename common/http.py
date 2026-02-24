"""
HTTP-клиент и повторные запросы к каталогу.

Инициализация: глобальные _transport и _breaker создаются при первом использовании.
Circuit breaker открывается после серии ошибок и блокирует запросы на open_time секунд.
request_with_retry использует breaker и один повтор при 5xx/таймауте/сети.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

import httpx

from common.config import get_settings
from common.exceptions import NetworkError
from common.logger import get_logger, get_trace_id


logger = get_logger("http")


class CircuitBreaker:
    """
    Логика «разомкнутой цепи»: при накоплении ошибок выше порога все запросы
    отклоняются с NetworkError до истечения open_time. on_success сбрасывает счётчик.
    """
    def __init__(self, *, error_threshold: int, open_time: float):
        self._error_threshold = error_threshold
        self._open_time = open_time
        self._failures = 0
        self._opened_until = 0.0
        self._lock = asyncio.Lock()

    async def guard(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._opened_until:
                raise NetworkError(code="CATALOG_UNAVAILABLE", message="Catalog circuit breaker is open")

    async def on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_until = 0.0

    async def on_error(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._error_threshold:
                self._opened_until = time.monotonic() + self._open_time


# Общий транспорт без повторных попыток на уровне httpx; повтор — в request_with_retry
_transport: httpx.AsyncHTTPTransport | None = None
# Один breaker на все вызовы каталога (общий для auth_service и др.)
_breaker = CircuitBreaker(error_threshold=5, open_time=30.0)


def _get_transport() -> httpx.AsyncHTTPTransport:
    global _transport
    if _transport is None:
        _transport = httpx.AsyncHTTPTransport(retries=0)
    return _transport


async def shutdown_http() -> None:
    """Закрытие глобального транспорта; вызывается при завершении Auth API (lifespan)."""
    global _transport
    if _transport is not None:
        await _transport.aclose()
    _transport = None


def _default_headers() -> dict[str, str]:
    """Заголовки по умолчанию для запросов к каталогу. User-Agent с 'kodeks' обязателен для доступа."""
    return {
        "X-Trace-Id": get_trace_id(),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36 kodeks"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }


async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Контекстный HTTP-клиент с таймаутом и лимитами из настроек; для использования как dependency."""
    settings = get_settings()
    timeout = httpx.Timeout(settings.HTTP_TIMEOUT)
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100, keepalive_expiry=30.0)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        transport=_get_transport(),
        headers=_default_headers(),
        follow_redirects=True,
    ) as client:
        yield client


async def create_http_client() -> httpx.AsyncClient:
    """Создаёт клиент без контекста; закрытие вызывающий код делает сам (aclose). Используется в infoboards."""
    settings = get_settings()
    timeout = httpx.Timeout(settings.HTTP_TIMEOUT)
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100, keepalive_expiry=30.0)
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        transport=_get_transport(),
        headers=_default_headers(),
        follow_redirects=True,
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """
    Выполняет запрос с проверкой circuit breaker и одним повтором при 5xx/таймауте/сетевой ошибке.
    При успехе сбрасывает счётчик breaker; при ошибке увеличивает и после порога открывает цепь.
    """
    base_delay = 0.1
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            await _breaker.guard()
        except NetworkError:
            logger.error(f"circuit breaker open: {method} {url}")
            raise
        try:
            resp = await client.request(method, url, data=data, params=params, headers=headers)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            await _breaker.on_success()
            return resp
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
            last_exc = e
            await _breaker.on_error()
            if attempt == 0:
                await asyncio.sleep(base_delay)
                continue
            logger.error(f"http failed: {method} {url}")
            raise NetworkError(message="Catalog request failed") from e

    raise NetworkError(message="Catalog request failed") from last_exc