"""
Клиент к Auth API для получения админских куков по reg.

Инициализация: AuthClient создаётся в worker с общим httpx.AsyncClient и AUTH_SERVICE_URL.
Обработка: get_admin_cookies выполняет POST (при 405 — GET) к /api/expert/reg/{reg}, парсит JSON с cookies;
при 5xx — NetworkError, при 4xx — AuthError, при отсутствии cookies — AuthError AUTH_COOKIES_MISSING.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from common.exceptions import AuthError, NetworkError
from common.logger import get_logger
from common.types import Cookies


logger = get_logger("users.auth_client")


@dataclass
class AuthClient:
    """Клиент к Auth API: один http_client и базовый URL сервиса."""
    http_client: httpx.AsyncClient
    auth_service_url: str

    async def get_admin_cookies(self, reg: str) -> Cookies:
        """Запрашивает куки администратора для reg; при успехе возвращает словарь имя куки -> значение."""
        url = f"{self.auth_service_url.rstrip('/')}/api/expert/reg/{reg}"
        logger.debug(f"request admin cookies reg={reg!r} url={url!r} method='POST'")
        try:
            response = await self.http_client.post(url)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.debug(f"auth service network failure reg={reg!r}: {e!r}")
            raise NetworkError(code="AUTH_SERVICE_UNAVAILABLE", message="Auth service request failed") from e
        if response.status_code == 405:
            logger.debug(f"auth service returned 405 for POST, trying GET reg={reg!r}")
            try:
                response = await self.http_client.get(url)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.debug(f"auth service network failure on GET fallback reg={reg!r}: {e!r}")
                raise NetworkError(code="AUTH_SERVICE_UNAVAILABLE", message="Auth service request failed") from e

        logger.debug(f"auth service response reg={reg!r} status={response.status_code}")
        if response.status_code >= 500:
            raise NetworkError(code="AUTH_SERVICE_5XX", message="Auth service temporary failure")
        if response.status_code >= 400:
            raise AuthError(code="AUTH_SERVICE_4XX", message="Auth service rejected request", http_status=401)

        payload = response.json()
        cookies = payload.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            raise AuthError(code="AUTH_COOKIES_MISSING", message="Auth cookies were not returned", http_status=401)
        logger.debug(f"admin cookies fetched reg={reg!r} keys={sorted(str(k) for k in cookies.keys())}")
        return {str(k): str(v) for k, v in cookies.items()}
