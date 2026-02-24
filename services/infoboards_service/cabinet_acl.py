"""
Общая логика «добавить группу в кабинет» (ACL): кабинеты, форма, POST.

Используется: scripts/add_cabinet_group.py (CLI), InitCompanyService (get_boards).
Константы и HTTP-путь получения формы — единый источник правды; скрипт добавляет
опциональный путь через Playwright (см. add_cabinet_group.py).
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote, urlencode

import httpx

from common.exceptions import NetworkError
from common.logger import get_logger
from services.infoboards_service.admin_dirs import (
    add_group_to_acl_json,
    build_admin_dirs_form,
    parse_acl_from_html,
    parse_full_form_from_html,
)


logger = get_logger("infoboards.cabinet_acl")

# Запрос кабинетов (docs) — общий для скрипта и InitCompanyService.
GRAPHQL_BOARDS = """
query{
  board{
    many{
      id
      title
    }
  }
}
"""

# Дефолты для POST /admin/dir (setup2=Настройки сервисов), если в GET-ответе их нет.
SETUP_DIR_DEFAULTS = {
    "path": "/docs/",
    "type": "3",
    "to": "kodeks6.dbs",
    "com": "Техэксперт",
    "trademark": "1",
    "startpage": "414100180",
    "setauth": "set",
    "auth_type": "1",
    "Support": "",
    "grps_1": "",
    "grps_3": "",
    "grps_5": "",
    "action": "save",
}


async def get_boards(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
) -> dict[str, str]:
    """GraphQL board.many → словарь title -> id кабинета."""
    url = f"{base_url}/infoboard/graphql?context=docs"
    resp = await client.post(
        url,
        json={"query": GRAPHQL_BOARDS, "variables": None},
        cookies=cookies,
        headers={"Accept": "application/json"},
    )
    if resp.status_code >= 400:
        raise NetworkError(
            code="GRAPHQL_FAILED",
            message=f"GraphQL boards status={resp.status_code}",
        )
    try:
        body = resp.json()
    except json.JSONDecodeError as e:
        snippet = (resp.text or "")[:300]
        raise NetworkError(
            code="GRAPHQL_NON_JSON",
            message=f"GraphQL вернул не JSON. Начало ответа: {snippet!r}",
        ) from e
    many = body.get("data", {}).get("board", {}).get("many", [])
    return {
        str(i.get("title", "")).strip(): str(i.get("id", "")).strip()
        for i in many
        if isinstance(i, dict) and i.get("id") and i.get("title")
    }


def get_docs_n_from_dirs(dirs_html: str) -> str:
    """Из HTML страницы /admin/dirs извлекает n для ссылки на /docs/ (например '2')."""
    m = re.search(r'admin/dir\?n=(\d+)"[^>]*>/docs/', dirs_html, re.I)
    return m.group(1) if m else "2"


async def fetch_acl_form_via_http(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    *,
    docs_n: str | None = None,
) -> tuple[str, str, str]:
    """
    Получить HTML формы ACL и post_url только по HTTP (без браузера).
    Возвращает (html, post_url, acl_form_base, docs_n).
    """
    dirs_url = f"{base_url}/admin/dirs"
    resp = await client.get(dirs_url, cookies=cookies)
    if resp.status_code >= 400:
        raise NetworkError(
            code="ADMIN_GET_FAILED",
            message=f"GET /admin/dirs status={resp.status_code}",
        )
    dirs_html = resp.text or ""
    if docs_n is None:
        docs_n = get_docs_n_from_dirs(dirs_html)
    dir_url = f"{base_url}/admin/dir?n={docs_n}"
    resp = await client.get(dir_url, cookies=cookies)
    if resp.status_code >= 400:
        raise NetworkError(
            code="ADMIN_GET_FAILED",
            message=f"GET {dir_url} status={resp.status_code}",
        )
    page_html = resp.text or ""
    setup_form = parse_full_form_from_html(page_html)
    if not setup_form:
        setup_form = {"n": docs_n, "set": ""}
    for k, v in SETUP_DIR_DEFAULTS.items():
        setup_form.setdefault(k, v)
    setup_form["setup2"] = "Настройки сервисов"
    setup_form.setdefault("n", docs_n)
    setup_form.setdefault("action", "save")
    encoded_setup = urlencode([(k, str(v)) for k, v in setup_form.items()])
    resp = await client.post(
        f"{base_url}/admin/dir",
        content=encoded_setup,
        cookies=cookies,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code >= 400:
        raise NetworkError(
            code="ADMIN_GET_FAILED",
            message=f"POST setup2 status={resp.status_code}",
        )
    setup_html = resp.text or ""
    has_acl = "acl_infoboard_" in setup_html or bool(parse_acl_from_html(setup_html))
    post_url = f"{base_url}/admin/dirs"
    acl_form_base = "%2Fdocs"
    if not has_acl:
        for try_url in [
            f"{base_url}/admin/dirs?base=%2Fdocs",
            f"{base_url}/admin/dirs?base=%2Fusers",
            f"{base_url}/admin/dirs",
        ]:
            try:
                r = await client.get(try_url, cookies=cookies)
            except (httpx.TimeoutException, httpx.NetworkError):
                continue
            if r.status_code == 200:
                ch = r.text or ""
                if "acl_infoboard_" in ch or parse_acl_from_html(ch):
                    setup_html = ch
                    has_acl = True
                    m = re.search(r"base=([^&'\"]+)", try_url, re.I)
                    if m:
                        acl_form_base = m.group(1)
                    break
    else:
        acl_form_base = "%2Fdocs"
    return (setup_html, post_url, acl_form_base, docs_n)


def build_acl_form_data(
    html: str,
    acl_form_base: str,
    board_key: str,
    group_id: int,
    extra_board_ids: set[str],
    *,
    no_cmd: bool = False,
    no_empty_fields: bool = False,
) -> list[tuple[str, str]]:
    """
    По HTML формы собрать full_form и данные для POST (одна группа в один кабинет).
    Для dry-run и dump_form; apply_acl_via_post использует ту же логику и выполняет POST.
    """
    full_form = parse_full_form_from_html(html)
    if acl_form_base and "base" not in full_form:
        base_val = unquote(acl_form_base) if acl_form_base.startswith("%") else acl_form_base
        full_form["base"] = base_val
    if not any(k.startswith("acl_infoboard_") for k in full_form):
        for k in ("action", "del", "path"):
            full_form.pop(k, None)
    if not full_form:
        acl_by_key = parse_acl_from_html(html)
        if acl_by_key:
            full_form = {f"acl_infoboard_{k}": v for k, v in acl_by_key.items()}
        if not full_form:
            raise ValueError("Страница не содержит полей формы ACL.")
    existing_acl = full_form.get(f"acl_infoboard_{board_key}", "")
    new_group_ids = add_group_to_acl_json(existing_acl, group_id)
    acl_overrides = {board_key: new_group_ids}
    form_data = build_admin_dirs_form(
        full_form,
        acl_overrides,
        extra_board_ids=extra_board_ids,
    )
    if no_cmd:
        form_data = [(k, v) for k, v in form_data if k != "cmd"]
    if no_empty_fields:
        form_data = [(k, v) for k, v in form_data if v or k in ("base", "n", "cmd")]
    return form_data


async def post_acl_form(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    post_url: str,
    form_data: list[tuple[str, str]],
    log: logging.Logger | None = None,
) -> None:
    """Отправить уже собранную форму ACL на post_url. При 4xx/5xx — NetworkError."""
    encoded = urlencode(form_data)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if base_url:
        headers["Referer"] = f"{base_url}/admin/dir"
        headers["Origin"] = base_url.rstrip("/")
    resp = await client.post(post_url, content=encoded, cookies=cookies, headers=headers)
    body = resp.text or ""
    (log or logger).debug("ACL POST response: status=%s body_len=%s", resp.status_code, len(body))
    if resp.status_code >= 400:
        err_msg = f"POST {post_url} status={resp.status_code}"
        if body and resp.status_code >= 500:
            err_msg += f" body={repr(body[:500].strip()[:200])}"
        raise NetworkError(code="ADMIN_POST_FAILED", message=err_msg)


async def apply_acl_via_post(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    html: str,
    post_url: str,
    acl_form_base: str,
    board_key: str,
    group_id: int,
    extra_board_ids: set[str],
    *,
    no_cmd: bool = False,
    no_empty_fields: bool = False,
    log: logging.Logger | None = None,
) -> None:
    """
    По HTML формы добавить группу в ACL кабинета board_key и отправить POST на post_url.
    При 4xx/5xx — NetworkError.
    """
    form_data = build_acl_form_data(
        html, acl_form_base, board_key, group_id, extra_board_ids,
        no_cmd=no_cmd, no_empty_fields=no_empty_fields,
    )
    await post_acl_form(base_url, client, cookies, post_url, form_data, log=log)
