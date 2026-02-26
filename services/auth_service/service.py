"""
Сервис авторизации в каталоге: получение куков админа или пользователя по reg.

Инициализация: AuthService создаётся в эндпоинте login (app.py) с передачей db и settings.
Обработка: login вызывает _login_flow; без name — _admin_login, с name — _fetch_user_password_as_admin
и _user_login. base_url берётся из БД (reg_services); запросы к каталогу через request_with_retry.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import Settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.http import create_http_client, request_with_retry
from common.logger import get_logger
from common.types import Cookies
from services.auth_service.metrics import (
    auth_latency_ms,
    auth_requests_total,
    catalog_latency_ms,
)

logger = get_logger("auth.service")

# Регулярки для извлечения поля пароля из HTML страницы пользователя каталога
_PSW_INPUT_TAG_RE = re.compile(
    r"""<input\b[^>]*\bname\s*=\s*["']psw["'][^>]*>""",
    re.IGNORECASE,
)
_VALUE_ATTR_RE = re.compile(r"""\bvalue\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _cookies_dict(cookies) -> Cookies:
    """Преобразует объект куков httpx в словарь имя -> значение."""
    return {name: cookies.get(name) for name in cookies.keys()}


@dataclass(frozen=True)
class AuthService:
    """Сервис логина в каталог: БД-сессия для reg_services и настройки (учётные данные, таймауты)."""
    db: AsyncSession
    settings: Settings

    async def login(self, reg: str, name: str | None) -> Cookies:
        """Вход в каталог: ограничение по AUTH_HARD_TIMEOUT, делегирование в _login_flow, метрики auth_latency_ms и auth_requests_total."""
        mode = "user" if name else "admin"
        auth_requests_total.labels(mode=mode).inc()

        hard_timeout = self.settings.AUTH_HARD_TIMEOUT
        if self.settings.LOG_LEVEL.upper() == "DEBUG":
            hard_timeout = self.settings.AUTH_HARD_TIMEOUT_DEBUG

        start = time.perf_counter()
        try:
            logger.debug(f"login start mode={mode} reg={reg!r} name={name!r}")
            cookies = await asyncio.wait_for(self._login_flow(reg=reg, name=name), timeout=hard_timeout)
            logger.debug(f"login success mode={mode} reg={reg!r} name={name!r} cookies_keys={list(cookies.keys())}")
            return cookies
        except asyncio.TimeoutError as e:
            raise NetworkError(code="TIMEOUT_ERROR", message="Auth operation timed out") from e
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            auth_latency_ms.observe(elapsed_ms)

    async def _login_flow(self, *, reg: str, name: str | None) -> Cookies:
        """Определяет base_url по reg, проверяет HTTPS, затем либо админ-логин, либо получение пароля пользователя и логин под ним."""
        base_url = await self._get_base_url(reg)
        if not base_url.lower().startswith("https://"):
            raise NetworkError(code="NETWORK_ERROR", message="TLS required (https://)")

        # Убираем дефолтные порты и завершающий слэш для единообразия URL
        if base_url.endswith((":443", ":80")):
            base_url = base_url.rsplit(":", 1)[0]
        base_url = base_url.rstrip("/")
        logger.debug(f"resolved base_url={base_url!r} for reg={reg!r}")

        if not name:
            # Если имя пользователя не передано, просто возвращаем куки администратора
            return await self._admin_login(base_url)

        # Если имя пользователя передано, получаем пароль через админский доступ и логинимся под пользователем
        password = await self._fetch_user_password_as_admin(base_url, name)
        return await self._user_login(base_url, name, password)

    async def _get_base_url(self, reg: str) -> str:
        """Читает base_url из таблицы (DB_TABLE_REG_SERVICES) по reg_number; при отсутствии — AuthError REG_NOT_FOUND."""
        tbl = self.settings.DB_TABLE_REG_SERVICES
        res = await self.db.execute(
            text(f"SELECT base_url FROM {tbl} WHERE reg_number = :reg"),
            {"reg": reg},
        )
        base_url = res.scalar_one_or_none()
        if not base_url:
            raise AuthError(code="REG_NOT_FOUND", message="reg отсутствует в БД", http_status=404)
        return str(base_url)

    async def _admin_login(self, base_url: str) -> Cookies:
        """POST на users/login.asp с учётными данными админа; куки возвращаются из клиента. Для изоляции сессии создаётся отдельный AsyncClient."""
        from httpx import AsyncClient
        client = AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            http2=True,
        )
        try:
            login_url = f"{base_url.rstrip('/')}/users/login.asp"
            logger.debug("admin login POST url=%r user=%r", login_url, self.settings.ADMIN_LOGIN)
            t0 = time.perf_counter()
            resp = await request_with_retry(
                client,
                "POST",
                login_url,
                data={
                    "user": self.settings.ADMIN_LOGIN,
                    "pass": self.settings.ADMIN_PASSWORD,
                    "path": "/admin",
                },
                headers={"Origin": base_url, "Referer": f"{base_url}/"},
            )
            catalog_latency_ms.labels(op="admin_login").observe((time.perf_counter() - t0) * 1000.0)

            if resp.status_code < 200 or resp.status_code >= 400:
                logger.debug(
                    "admin login failed status=%s body_snippet=%r",
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                raise AuthError(
                    code="ADMIN_AUTH_FAILED",
                    message=f"Admin auth failed (status={resp.status_code})",
                    http_status=401,
                )
            # Извлекаем куки как dict
            cookies = dict(client.cookies)
            return cookies
        finally:
            await client.aclose()

    async def _fetch_user_password_as_admin(self, base_url: str, name: str) -> str:
        """Логин админа, затем GET страницы пользователя по name; пароль извлекается из HTML (поле psw). При пустом пароле используется пароль админа (для скрещивания с фронтом)."""
        from httpx import AsyncClient
        client = AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            http2=True,
        )
        try:
            # Шаг 1 — логин админа через users/login.asp
            login_url = f"{base_url.rstrip('/')}/users/login.asp"
            logger.debug("user-flow admin login POST url=%r user=%r", login_url, self.settings.ADMIN_LOGIN)
            t0 = time.perf_counter()
            resp = await request_with_retry(
                client,
                "POST",
                login_url,
                data={
                    "user": self.settings.ADMIN_LOGIN,
                    "pass": self.settings.ADMIN_PASSWORD,
                    "path": "/admin",
                },
                headers={"Origin": base_url, "Referer": f"{base_url}/"},
            )
            catalog_latency_ms.labels(op="admin_login").observe((time.perf_counter() - t0) * 1000.0)
            if resp.status_code < 200 or resp.status_code >= 400:
                logger.debug(
                    "user-flow admin login failed status=%s body_snippet=%r",
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                raise AuthError(
                    code="ADMIN_AUTH_FAILED",
                    message=f"Admin auth failed (status={resp.status_code})",
                    http_status=401,
                )

            # Шаг 2 — получить страницу пользователя и вытащить пароль
            user_url = f"{base_url}/users/usr"
            logger.debug("user page GET url=%r id=%r", user_url, name)
            t1 = time.perf_counter()
            page = await request_with_retry(client, "GET", user_url, params={"id": name})
            catalog_latency_ms.labels(op="user_page").observe((time.perf_counter() - t1) * 1000.0)

            if page.status_code != 200:
                raise NetworkError(code="USER_PAGE_ERROR", message="User page not available", http_status=502)

            # Парсим страницу пользователя, чтобы извлечь пароль
            logger.debug(f"Получена страница пользователя: {page.text[:500]}...")

            # Ищем поле пароля с именем "psw"
            tag_match = _PSW_INPUT_TAG_RE.search(page.text)
            if not tag_match:
                # Ищем все input поля для отладки
                all_inputs = re.findall(r'<input[^>]*>', page.text)
                logger.debug(f"Все input поля: {all_inputs}")
                raise ParseError(code="USER_PASSWORD_NOT_FOUND", message="Password field not found")

            logger.debug(f"Найдено поле пароля: {tag_match.group(0)}")

            # Извлекаем значение из атрибута value
            value_match = _VALUE_ATTR_RE.search(tag_match.group(0))
            if not value_match:
                logger.debug(f"Не найден атрибут value в поле пароля: {tag_match.group(0)}")
                raise ParseError(code="USER_PASSWORD_NOT_FOUND", message="Password value not found")

            password = value_match.group(1)
            logger.debug(f"Извлечённое значение пароля: '{password}'")

            if not password:
                # Если пароль пустой, пробуем использовать пароль из EVN как fallback
                logger.warning("Password is empty in user page, using admin password as fallback")
                password = self.settings.ADMIN_PASSWORD

            logger.debug(f"Используемый пароль для входа: '{password}'")
            return password
        finally:
            await client.aclose()

    async def _user_login(self, base_url: str, name: str, password: str) -> Cookies:
        """POST на users/login.asp под пользователем name с переданным password; возвращаются куки сессии пользователя."""
        from httpx import AsyncClient
        client = AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            http2=True,
        )
        try:
            login_url = f"{base_url.rstrip('/')}/users/login.asp"
            logger.debug("user login POST url=%r login=%r", login_url, name)
            t0 = time.perf_counter()
            resp = await client.post(
                login_url,
                data={
                    "user": name,
                    "pass": password,
                    "path": "/",
                },
                headers={"Origin": base_url, "Referer": f"{base_url}/"},
            )
            catalog_latency_ms.labels(op="user_login").observe((time.perf_counter() - t0) * 1000.0)

            if resp.status_code < 200 or resp.status_code >= 400:
                logger.debug(
                    "user login failed status=%s body_snippet=%r",
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                raise AuthError(
                    code="USER_AUTH_FAILED",
                    message=f"User auth failed",
                    http_status=401,
                )

            # Извлекаем куки как dict
            cookies = dict(client.cookies)
            logger.debug(f"user login success cookies_keys={list(cookies.keys())}")
            logger.debug(f"user login success cookies_values={cookies}")
            return cookies
        finally:
            await client.aclose()










