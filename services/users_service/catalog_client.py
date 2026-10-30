"""
Клиент к каталогу: чтение и запись пользователей и групп по HTML/формам.

Инициализация: CatalogClient создаётся в worker с общим httpx.AsyncClient.
Обработка: get_user_page — GET /users/usr?id=; post_user — POST /users/users; get_groups_page — GET /users/groups;
create_group — чтение /users/groups, GET шаблона /users/grp и POST полной формы на её action.
Ответы 401/403 -> AuthError, 5xx -> NetworkError.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlencode, urlsplit, urljoin
from html.parser import HTMLParser

import httpx

from common.exceptions import AuthError, NetworkError, ParseError
from common.catalog_lock import catalog_operation
from common.logger import get_logger
from services.users_service.html_parser import (
    parse_user_state, parse_groups_catalog, parse_group_creation_details,
    group_index, normalize_title, user_search_confirms_absence,
)


logger = get_logger("users.catalog_client")


class _LoginDetector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.login = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            path = urlsplit(attributes.get("action", "")).path.lower()
            if "login" in path or path.rstrip("/").endswith("/auth"):
                self.login = True


def _check_response(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog session was rejected")
    if response.status_code >= 500:
        raise NetworkError(code="CATALOG_5XX", message=f"Catalog returned HTTP {response.status_code}")
    if response.status_code >= 400:
        raise NetworkError(code="CATALOG_HTTP_ERROR", message=f"Catalog returned HTTP {response.status_code}")
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "").lower()
        if "login" in location or "/auth" in location:
            raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog redirected to login")
        raise NetworkError(code="CATALOG_REDIRECT", message="Catalog redirect was not resolved")
    detector = _LoginDetector()
    detector.feed(response.text)
    path = response.url.path.lower()
    if detector.login or "login" in path or path.rstrip("/").endswith("/auth"):
        raise AuthError(code="CATALOG_AUTH_FAILED", message="Catalog returned a login page")


@dataclass
class CatalogClient:
    """Клиент к каталогу: один http_client для всех запросов к base_url каталога."""
    http_client: httpx.AsyncClient

    def operation(self, base_url: str):
        """Serialize read/modify/write across processes using PostgreSQL."""
        return catalog_operation(base_url)

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
            raise ParseError(code="CATALOG_WRITE_UNCERTAIN", message="The catalog write outcome is unknown; reconciliation is required") from e
        logger.debug(
            f"catalog POST {op_name} done status={response.status_code} body_snippet={(response.text or '')[:200]!r}"
        )
        if response.status_code >= 500:
            raise ParseError(code="CATALOG_WRITE_UNCERTAIN", message="The catalog write returned a server error; reconciliation is required")
        return response

    async def get_user_page(self, base_url: str, uid: str, cookies: dict[str, str]) -> str | None:
        """Read a user; absence requires 404 or a verified filtered search result."""
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
        if response.status_code == 404:
            return None
        _check_response(response)
        state = parse_user_state(response.text)
        if state.is_empty_form:
            search_url = f"{base_url.rstrip('/')}/users/users"
            try:
                search = await self.http_client.get(search_url, params={"tmpl": uid}, cookies=cookies)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                raise NetworkError(code="CATALOG_READ_FAILED", message="Cannot verify user absence") from error
            _check_response(search)
            if user_search_confirms_absence(search.text, uid, page_url=search_url):
                return None
            raise ParseError(code="USER_PRESENCE_UNCERTAIN", message="Blank user form does not confirm absence")
        if state.fields.get("uid") != uid and state.fields.get("id") != uid:
            raise ParseError(code="USER_IDENTITY_MISMATCH", message="Catalog returned another user")
        return response.text

    async def get_user_create_page(self, base_url: str, cookies: dict[str, str]) -> str:
        """Read the actual creation template; never construct a minimal form."""
        try:
            response = await self.http_client.get(f"{base_url.rstrip('/')}/users/usr", cookies=cookies)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise NetworkError(code="CATALOG_READ_FAILED", message="Cannot read user creation form") from e
        _check_response(response)
        state = parse_user_state(response.text)
        if not state.is_empty_form:
            raise ParseError(code="USER_CREATE_FORM_INVALID", message="Expected a blank user creation form")
        return response.text

    async def post_user(self, base_url: str, form_data: list[tuple[str, str]], cookies: dict[str, str], *, form_action="", form_method="post") -> None:
        """POST сохранения/обновления пользователя на /users/users; при ошибке — исключение."""
        url = f"{base_url.rstrip('/')}/users/users"
        if form_method.lower() != "post":
            raise ParseError(code="CATALOG_FORM_METHOD", message="User save form must use POST")
        if form_action:
            url = _form_target(f"{base_url.rstrip('/')}/users/usr", form_action)
        response = await self._post_form(url=url, form_data=form_data, cookies=cookies, op_name="user")
        _check_response(response)
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
        _check_response(response)
        parse_groups_catalog(response.text)
        return response.text

    async def get_group_create_page(self, base_url: str, cookies: dict[str, str]) -> str:
        """Read and validate the blank group template, separate from the groups index."""
        page_url = f"{base_url.rstrip('/')}/users/grp"
        try:
            response = await self.http_client.get(page_url, cookies=cookies)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise NetworkError(code="CATALOG_READ_FAILED", message="Cannot read group creation form") from error
        _check_response(response)
        pairs, action, method = parse_group_creation_details(response.text)
        if any(key in {"gn", "name"} and value.strip() for key, value in pairs):
            raise ParseError(code="GROUP_CREATE_FORM_INVALID", message="Expected a blank group creation form")
        if method != "post":
            raise ParseError(code="CATALOG_FORM_METHOD", message="Group save form must use POST")
        if action:
            _form_target(page_url, action)
        return response.text

    async def create_group(self, base_url: str, title: str, cookies: dict[str, str]) -> bool:
        async with self.operation(base_url):
            return await self._create_group(base_url, title, cookies)

    async def _create_group(self, base_url: str, title: str, cookies: dict[str, str]) -> bool:
        """Re-read and parse before creating; retain the complete catalog form."""
        page = await self.get_groups_page(base_url, cookies)
        groups = group_index(parse_groups_catalog(page))
        normalized = normalize_title(title)
        if not normalized:
            raise ParseError(code="INVALID_GROUP_TITLE", message="Group title is empty")
        if normalized in groups:
            return False
        url = f"{base_url.rstrip('/')}/users/grp"
        creation_page = await self.get_group_create_page(base_url, cookies)
        current_form, form_action, _ = parse_group_creation_details(creation_page)
        if form_action:
            url = _form_target(url, form_action)
        form_data = [(key, value) for key, value in current_form if key != "name"]
        form_data.append(("name", title))
        response = await self._post_form(url=url, form_data=form_data, cookies=cookies, op_name="group")
        _check_response(response)
        try:
            verified = group_index(parse_groups_catalog(await self.get_groups_page(base_url, cookies)))
        except (AuthError, NetworkError) as error:
            raise ParseError(code="GROUP_WRITE_UNCERTAIN", message="Group creation could not be verified") from error
        if normalized not in verified:
            raise ParseError(code="GROUP_CREATE_NOT_CONFIRMED", message="Group is absent after saving")
        return True

    async def rename_group(self, base_url: str, group_id: int, title: str, cookies: dict[str, str]) -> None:
        """Read the group's own full form, preserve its ID, then verify the rename."""
        async with self.operation(base_url):
            groups = parse_groups_catalog(await self.get_groups_page(base_url, cookies))
            normalized = normalize_title(title)
            if not normalized:
                raise ParseError(code="INVALID_GROUP_TITLE", message="Group title is empty")
            index = group_index(groups)
            if normalized in index and index[normalized] != group_id:
                raise ParseError(code="AMBIGUOUS_GROUP", message="Another group already has the desired title")
            if group_id not in groups.values():
                raise ParseError(code="GROUP_BINDING_BROKEN", message="The bound group no longer exists")
            if index.get(normalized) == group_id:
                return
            page_url = f"{base_url.rstrip('/')}/users/grp"
            try:
                response = await self.http_client.get(page_url, params={"grp": group_id}, cookies=cookies)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                raise NetworkError(code="CATALOG_READ_FAILED", message="Cannot read group editing form") from error
            _check_response(response)
            pairs, action, method = parse_group_creation_details(response.text)
            ids = [value for key, value in pairs if key == "grp"]
            form_names = [value for key, value in pairs if key == "name"]
            before_names = [name for name, existing_id in groups.items() if existing_id == group_id]
            if (method != "post" or ids != [str(group_id)] or len(form_names) != 1
                    or len(before_names) != 1
                    or normalize_title(form_names[0]) != normalize_title(before_names[0])):
                raise ParseError(code="GROUP_EDIT_FORM_INVALID", message="Group form does not confirm the bound group ID")
            pairs = [(key, value) for key, value in pairs if key != "name"] + [("name", title)]
            target = _form_target(page_url, action) if action else page_url
            saved = await self._post_form(url=target, form_data=pairs, cookies=cookies, op_name="group_rename")
            _check_response(saved)
            try:
                verified = group_index(parse_groups_catalog(await self.get_groups_page(base_url, cookies)))
            except (AuthError, NetworkError) as error:
                raise ParseError(code="GROUP_WRITE_UNCERTAIN", message="Group rename could not be verified") from error
            if verified.get(normalized) != group_id:
                raise ParseError(code="GROUP_RENAME_NOT_CONFIRMED", message="Group rename did not preserve the expected ID and title")


def _form_target(page_url: str, action: str) -> str:
    target = urljoin(page_url, action)
    original, destination = urlsplit(page_url), urlsplit(target)
    if (original.scheme, original.netloc) != (destination.scheme, destination.netloc):
        raise ParseError(code="CATALOG_FORM_TARGET", message="Form action belongs to a different catalog")
    return target

def _redact_form_data(form_data: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Копия form_data с заменой значений psw и mail на *** для логов."""
    redacted: list[tuple[str, str]] = []
    for key, value in form_data:
        if key in {"psw", "mail"}:
            redacted.append((key, "***"))
            continue
        redacted.append((key, value))
    return redacted
