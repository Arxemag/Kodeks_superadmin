"""
Сервис авторизации в каталоге: получение куков админа или пользователя по reg.

Инициализация: AuthService создаётся в эндпоинте login (app.py) с передачей db и settings.
Обработка: login вызывает _login_flow; без name — _admin_login, с name — _fetch_user_password_as_admin
и _user_login. base_url берётся из БД (reg_services); запросы к каталогу через request_with_retry.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlencode

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

    async def login_for_sso(self, reg: str, name: str) -> Cookies:
        """Вход SSO только под подтверждённым пользователем, без подстановки пароля админа."""
        if not name or not name.strip():
            raise AuthError(code="SSO_USER_REQUIRED", message="A catalog user is required")

        async def flow():
            base_url = (await self._get_base_url(reg)).rstrip("/")
            if not base_url.lower().startswith("https://"):
                raise NetworkError(code="NETWORK_ERROR", message="TLS required (https://)")
            password = await self._fetch_user_password_as_admin(base_url, name, verify_user=True)
            return await self._user_login(base_url, name, password, verify_login=True)

        auth_requests_total.labels(mode="user").inc()
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(flow(), timeout=self.settings.AUTH_HARD_TIMEOUT)
        except asyncio.TimeoutError as error:
            raise NetworkError(code="TIMEOUT_ERROR", message="SSO auth operation timed out") from error
        finally:
            auth_latency_ms.observe((time.perf_counter() - started) * 1000.0)

    async def verify_sso_session(self, reg: str, name: str, cookies: Cookies) -> Cookies:
        """Проверить готовую пользовательскую сессию без нового входа в каталог."""
        allowed = {"Auth", "Kodeks", "KodeksData"}
        if (not isinstance(cookies, dict) or not cookies.get("Auth") or set(cookies) - allowed
                or any(not isinstance(value, str) or any(char in value for char in "\r\n\x00")
                       for value in cookies.values())):
            raise AuthError(code="SSO_SESSION_INVALID", message="Catalog session is invalid")
        cookies = dict(cookies)
        try:
            decoded = base64.b64decode(cookies["Auth"], validate=True).decode("utf-8")
            username, separator, password = decoded.partition(":")
            if not separator or not username or not password:
                raise ValueError
        except (ValueError, UnicodeError, binascii.Error):
            raise AuthError(code="SSO_SESSION_INVALID", message="Catalog session is invalid") from None
        if username != name:
            raise AuthError(code="SSO_USER_IDENTITY_MISMATCH", message="Catalog session belongs to another user")

        async def flow():
            from httpx import AsyncClient
            from services.users_service.catalog_client import _check_response
            from services.users_service.html_parser import _Document, _form_controls

            base_url = (await self._get_base_url(reg)).rstrip("/")
            origin = urlsplit(base_url)
            if origin.scheme != "https" or not origin.hostname:
                raise NetworkError(code="NETWORK_ERROR", message="TLS required (https://)")

            async def same_catalog(request):
                target = urlsplit(str(request.url))
                if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                    raise AuthError(code="SSO_SESSION_INVALID", message="Catalog session redirected to another host")

            async with AsyncClient(follow_redirects=True, timeout=30.0, http2=True,
                                   event_hooks={"request": [same_catalog]}) as client:
                for key, value in cookies.items():
                    client.cookies.set(key, value, domain=origin.hostname, path="/")
                response = await client.get(f"{base_url}/docs/")
                _check_response(response)
                if response.status_code != 200:
                    raise AuthError(code="SSO_USER_AUTH_FAILED", message="Catalog session was not accepted")
                root = _Document(response.text).root
                for form in (node for node in root.walk() if node.tag == "form"):
                    fields = {node.attrs.get("name") for node in _form_controls(root, form)}
                    if {"login", "password"} <= fields:
                        raise AuthError(code="SSO_USER_AUTH_FAILED", message="Catalog session was not accepted")
                verified = {key: value for key, value in dict(client.cookies).items() if key in allowed}
                if not verified.get("Auth") or verified["Auth"] != cookies["Auth"]:
                    raise AuthError(code="SSO_SESSION_INVALID", message="Catalog session was not preserved")
                return verified

        try:
            return await asyncio.wait_for(flow(), timeout=self.settings.AUTH_HARD_TIMEOUT)
        except asyncio.TimeoutError as error:
            raise NetworkError(code="TIMEOUT_ERROR", message="SSO session verification timed out") from error

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
                logger.debug("admin login failed status=%s", resp.status_code)
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

    async def _fetch_user_password_as_admin(self, base_url: str, name: str, *, verify_user: bool = False) -> str:
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
                logger.debug("user-flow admin login failed status=%s", resp.status_code)
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

            if verify_user:
                from services.users_service.catalog_client import _check_response
                from services.users_service.html_parser import parse_user_state

                _check_response(page)
                state = parse_user_state(page.text)
                if state.is_empty_form or state.fields.get("uid") != name:
                    raise AuthError(code="SSO_USER_NOT_FOUND", message="Catalog user was not confirmed")
                password = state.fields.get("psw", "")
                if not password:
                    raise AuthError(code="SSO_USER_PASSWORD_EMPTY", message="Catalog user has no password")
                return password

            # Ищем поле пароля с именем "psw"
            tag_match = _PSW_INPUT_TAG_RE.search(page.text)
            if not tag_match:
                raise ParseError(code="USER_PASSWORD_NOT_FOUND", message="Password field not found")

            # Извлекаем значение из атрибута value
            value_match = _VALUE_ATTR_RE.search(tag_match.group(0))
            if not value_match:
                raise ParseError(code="USER_PASSWORD_NOT_FOUND", message="Password value not found")

            password = value_match.group(1)

            if not password:
                # Если пароль пустой, пробуем использовать пароль из EVN как fallback
                logger.warning("Password is empty in user page, using admin password as fallback")
                password = self.settings.ADMIN_PASSWORD

            return password
        finally:
            await client.aclose()

    async def _user_login(self, base_url: str, name: str, password: str, *, verify_login: bool = False) -> Cookies:
        """POST на users/login.asp под пользователем name с переданным password; возвращаются куки сессии пользователя."""
        if verify_login:
            return await self._sso_form_login(base_url, name, password)
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
                logger.debug("user login failed status=%s", resp.status_code)
                raise AuthError(
                    code="USER_AUTH_FAILED",
                    message=f"User auth failed",
                    http_status=401,
                )

            # Извлекаем куки как dict
            cookies = dict(client.cookies)
            logger.debug(f"user login success cookies_keys={list(cookies.keys())}")
            return cookies
        finally:
            await client.aclose()

    async def _sso_form_login(self, base_url: str, name: str, password: str) -> Cookies:
        """Распознать AJAX-вход /docs/ и проверить выданную сессию свежим чтением."""
        from httpx import AsyncClient
        from services.users_service.catalog_client import _check_response
        from services.users_service.html_parser import _Document, _ancestor, _disabled, _form_controls, _successful

        async with AsyncClient(follow_redirects=True, timeout=30.0, http2=True) as client:
            page = await client.get(f"{base_url.rstrip('/')}/docs/")
            if page.status_code not in (200, 401, 403):
                _check_response(page)
            root = _Document(page.text).root
            candidates = []
            for form in (node for node in root.walk() if node.tag == "form"):
                if _ancestor(form, "form") is not None:
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Nested login forms are unsupported")
                controls = _form_controls(root, form)
                pairs = _successful(controls)
                if not {"login", "password"} <= {key for key, _ in pairs}:
                    continue
                if any(len([key for key, _ in pairs if key == field]) != 1 for field in ("login", "password")):
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Ambiguous login controls")
                # The observed form is intercepted by JS, which sends exactly
                # user/pass/path. Additional fields need a confirmed adapter.
                if any(key not in {"login", "password"} for key, _ in pairs):
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Unknown AJAX login controls")
                if (form.attrs.get("method") or "get").lower() != "post":
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Login form must use POST")
                target = urljoin(str(page.url), form.attrs.get("action") or str(page.url))
                original, destination = urlsplit(base_url), urlsplit(target)
                if (original.scheme, original.netloc) != (destination.scheme, destination.netloc):
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Login form belongs to a different catalog")
                submitters = [node for node in controls if not _disabled(node)
                              and node.tag in {"input", "button"}
                              and (node.attrs.get("type") or ("submit" if node.tag == "button" else "text")).lower() == "submit"]
                if len(submitters) != 1:
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="A unique login submitter was not found")
                submitter = submitters[0]
                if submitter.attrs.get("name"):
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Unknown AJAX login submitter")
                field_ids = {node.attrs.get("name"): node.attrs.get("id") for node in controls
                             if node.attrs.get("name") in {"login", "password"} and not _disabled(node)}
                if field_ids != {"login": "user", "password": "pass"}:
                    raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="AJAX login field mapping is unknown")
                candidates.append(form)
            if len(candidates) != 1:
                raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="A unique catalog login form was not found")
            scripts = [node.text() for node in root.walk() if node.tag == "script"]
            ajax_scripts = [script for script in scripts
                            if re.search(r"function\s+formSubmit\s*\(\s*\)", script)
                            and re.search(r"\$\.ajax\s*\(", script)
                            and re.search(r"\btype\s*:\s*(['\"])post\1", script, re.I)
                            and re.search(r"\burl\s*=\s*(['\"])/users/login\.asp\1", script)
                            and re.search(r"\bpath\s*:\s*(['\"])/docs\1", script)
                            and re.search(r"\buser\s*:\s*\$\(\s*(['\"])#user\1\s*\)\.val\(\)", script)
                            and re.search(r"\bpass\s*:\s*\$\(\s*(['\"])#pass\1\s*\)\.val\(\)", script)]
            binding = re.search(r"\$\(\s*(['\"])#authForm\1\s*\)\.bind\(\s*(['\"])submit\2\s*,\s*formSubmit\s*\)", "\n".join(scripts))
            if len(ajax_scripts) != 1 or binding is None:
                raise ParseError(code="SSO_LOGIN_FORM_INVALID", message="Catalog AJAX login contract was not confirmed")
            response = await client.post(f"{base_url.rstrip('/')}/users/login.asp",
                                        content=urlencode({"user": name, "pass": password, "path": "/docs"}), headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": base_url.rstrip("/"), "Referer": str(page.url),
            })
            _check_response(response)
            cookies = dict(client.cookies)
            if not cookies.get("Auth"):
                raise AuthError(code="SSO_USER_AUTH_FAILED", message="Catalog returned no authenticated user session")
            protected = await client.get(f"{base_url.rstrip('/')}/docs/")
            _check_response(protected)
            for html in (response.text, protected.text):
                response_root = _Document(html).root
                for form in (node for node in response_root.walk() if node.tag == "form"):
                    fields = {node.attrs.get("name") for node in _form_controls(response_root, form)}
                    if {"login", "password"} <= fields:
                        raise AuthError(code="SSO_USER_AUTH_FAILED", message="Catalog returned its user login form")
            return cookies










