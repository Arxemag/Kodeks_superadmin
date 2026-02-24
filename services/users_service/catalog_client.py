"""
Клиент к каталогу: чтение и запись пользователей и групп по HTML/формам.

Инициализация: CatalogClient создаётся в worker с общим httpx.AsyncClient.
Обработка: get_user_page — GET /users/usr?id=; post_user — POST /users/users; get_groups_page — GET /users/groups;
create_group — POST /users/groups (при 404/405 — fallback на /users/grp). Ответы 401/403 -> AuthError, 5xx -> NetworkError.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlencode

import httpx

from common.exceptions import AuthError, NetworkError
from common.logger import get_logger


logger = get_logger("users.catalog_client")


@dataclass
class CatalogClient:
    """Клиент к каталогу: один http_client для всех запросов к base_url каталога."""
    http_client: httpx.AsyncClient

    async def _post_form(
        self,
        *,
        url: str,
        form_data: list[tuple[str, str]],
        cookies: dict[str, str],
        op_name: str,
    ) -> httpx.Response:
        """Общий POST application/x-www-form-urlencoded; логирование с маскировкой psw/mail; при сетевой ошибке — NetworkError."""
        safe_form = _redact_form_data(form_data)
        logger.debug(
            f"catalog POST {op_name} url={url!r} fields={safe_form} cookie_keys={sorted(cookies.keys())}"
        )
        encoded_form = urlencode(form_data)
        try:
            response = await self.http_client.post(
                url,
                content=encoded_form,
                cookies=cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.debug(f"catalog POST {op_name} failed: {e!r}")
            raise NetworkError(code="CATALOG_WRITE_FAILED", message="Failed to persist user data") from e
        logger.debug(
            f"catalog POST {op_name} done status={response.status_code} body_snippet={(response.text or '')[:200]!r}"
        )
        return response

    async def get_user_page(self, base_url: str, uid: str, cookies: dict[str, str]) -> str | None:
        """GET страницы пользователя по uid; при 4xx (кроме 401/403) возвращает None (пользователь не найден)."""
        url = f"{base_url.rstrip('/')}/users/usr"
        logger.debug(f"catalog GET user page uid={uid!r} url={url!r} cookie_keys={sorted(cookies.keys())}")
        try:
            response = await self.http_client.get(url, params={"id": uid}, cookies=cookies)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.debug(f"catalog GET failed uid={uid!r}: {e!r}")
            raise NetworkError(code="CATALOG_READ_FAILED", message="Failed to read user page") from e

        logger.debug(
            f"catalog GET done uid={uid!r} status={response.status_code} len={len(response.text or '')}"
        )
        if response.status_code in (401, 403):
            raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog admin cookies are not accepted")
        if response.status_code >= 500:
            raise NetworkError(code="CATALOG_5XX", message="Catalog temporary failure")
        if response.status_code >= 400:
            return None
        return response.text

    async def post_user(self, base_url: str, form_data: list[tuple[str, str]], cookies: dict[str, str]) -> None:
        """POST сохранения/обновления пользователя на /users/users; при ошибке — исключение."""
        url = f"{base_url.rstrip('/')}/users/users"
        response = await self._post_form(url=url, form_data=form_data, cookies=cookies, op_name="user")
        if response.status_code in (401, 403):
            raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog admin cookies are not accepted")
        if response.status_code >= 500:
            raise NetworkError(code="CATALOG_5XX", message="Catalog temporary failure")
        if response.status_code >= 400:
            raise NetworkError(
                code="CATALOG_4XX",
                message=f"Catalog rejected request status={response.status_code}",
                http_status=502,
            )

    async def get_groups_page(self, base_url: str, cookies: dict[str, str]) -> str:
        """GET страницы групп каталога с no-cache и query _ для обхода кэша."""
        url = f"{base_url.rstrip('/')}/users/groups"
        logger.debug(f"catalog GET groups url={url!r} cookie_keys={sorted(cookies.keys())}")
        try:
            response = await self.http_client.get(
                url,
                cookies=cookies,
                params={"_": str(time.time_ns())},
                headers={"Cache-Control": "no-cache"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.debug(f"catalog GET groups failed: {e!r}")
            raise NetworkError(code="CATALOG_READ_FAILED", message="Failed to read groups page") from e

        logger.debug(f"catalog GET groups done status={response.status_code} len={len(response.text or '')}")
        if response.status_code in (401, 403):
            raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog admin cookies are not accepted")
        if response.status_code >= 500:
            raise NetworkError(code="CATALOG_5XX", message="Catalog temporary failure")
        if response.status_code >= 400:
            raise NetworkError(code="CATALOG_4XX", message=f"Groups page rejected status={response.status_code}")
        return response.text

    async def create_group(self, base_url: str, title: str, cookies: dict[str, str]) -> None:
        """Создание группы: POST на /users/groups; при 404/405 — повтор на /users/grp (legacy)."""
        url = f"{base_url.rstrip('/')}/users/groups"
        form_data = [("name", title), ("gn", ""), ("cmd", "")]
        response = await self._post_form(url=url, form_data=form_data, cookies=cookies, op_name="group")
        if response.status_code in (404, 405):
            legacy_url = f"{base_url.rstrip('/')}/users/grp"
            response = await self._post_form(
                url=legacy_url,
                form_data=form_data,
                cookies=cookies,
                op_name="group_legacy",
            )
        if response.status_code in (401, 403):
            raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog admin cookies are not accepted")
        if response.status_code >= 500:
            raise NetworkError(code="CATALOG_5XX", message="Catalog temporary failure")
        if response.status_code >= 400:
            raise NetworkError(code="CATALOG_4XX", message=f"Group create rejected status={response.status_code}")

def _redact_form_data(form_data: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Копия form_data с заменой значений psw и mail на *** для логов."""
    redacted: list[tuple[str, str]] = []
    for key, value in form_data:
        if key in {"psw", "mail"}:
            redacted.append((key, "***"))
            continue
        redacted.append((key, value))
    return redacted
