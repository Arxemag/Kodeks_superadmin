"""
Общая логика «добавить группу в кабинет» (ACL): кабинеты, форма, POST.

Используется: scripts/add_cabinet_group.py (CLI), InitCompanyService (get_boards).
Константы и HTTP-путь получения формы — единый источник правды.
При JS-рендеринге формы используется fetch_acl_form_via_browser (Playwright).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import unquote, urlparse, urlencode

import httpx

from common.exceptions import NetworkError
from common.logger import get_logger
from services.infoboards_service.admin_dirs import (
    acl_to_json,
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
    logger.debug("fetch_acl_form_via_http GET %s", dirs_url)
    resp = await client.get(dirs_url, cookies=cookies)
    if resp.status_code >= 400:
        raise NetworkError(
            code="ADMIN_GET_FAILED",
            message=f"GET /admin/dirs status={resp.status_code}",
        )
    dirs_html = resp.text or ""
    if docs_n is None:
        docs_n = get_docs_n_from_dirs(dirs_html)
    logger.debug("fetch_acl_form_via_http docs_n=%s", docs_n)
    dir_url = f"{base_url}/admin/dir?n={docs_n}"
    logger.debug("fetch_acl_form_via_http GET %s", dir_url)
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
    logger.debug("fetch_acl_form_via_http POST %s/admin/dir setup_form_keys=%s", base_url, list(setup_form.keys()))
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
    acl_parsed = parse_acl_from_html(setup_html)
    has_acl = "acl_infoboard_" in setup_html or bool(acl_parsed)
    logger.debug(
        "fetch_acl_form_via_http POST response setup_html_len=%s has_acl=%s parse_acl_from_html_keys=%s",
        len(setup_html), has_acl, list(acl_parsed.keys()) if acl_parsed else [],
    )
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


def _pw_cookies(cookies: dict[str, str], base_url: str) -> list[dict]:
    """Кукки в формате Playwright (name, value, domain, path)."""
    parsed = urlparse(base_url)
    domain = parsed.netloc or parsed.path.split("/")[0] or "localhost"
    return [{"name": k, "value": v, "domain": domain, "path": "/"} for k, v in cookies.items()]


async def fetch_acl_form_via_browser(
    base_url: str,
    cookies: dict[str, str],
    docs_n: str,
) -> tuple[str, str, str, str]:
    """
    Получить HTML формы ACL через Playwright (JS выполняется, форма acl_infoboard_* появляется в DOM).
    Возвращает (html, post_url, acl_form_base, docs_n). Требует: pip install playwright && playwright install chromium.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise NetworkError(
            code="PLAYWRIGHT_NOT_INSTALLED",
            message="Для загрузки формы с JS установите: pip install playwright && playwright install chromium",
        ) from e

    url = f"{base_url}/admin/dir?n={docs_n}"
    logger.info("fetch_acl_form_via_browser: загрузка формы ACL через браузер (Playwright) %s", url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(_pw_cookies(cookies, base_url))
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("form, a", timeout=10000)
            submitted = False
            btn = page.locator('input[name=setup2][type=submit], input[name=setup2][value*="Настройки"]')
            if await btn.count() > 0:
                await btn.first.click()
                submitted = True
            if not submitted:
                sel = page.locator("select[name=setup2]")
                if await sel.count() > 0:
                    await sel.select_option(label="Настройки сервисов")
                    await page.locator('input[type="submit"], button[type="submit"]').first.click()
                    submitted = True
            if not submitted:
                link = page.get_by_text("Настройки сервисов", exact=False).first
                if await link.count() > 0:
                    await link.click()
                    submitted = True
            if not submitted:
                raise ValueError("Не найдена кнопка/ссылка «Настройки сервисов»")
            await asyncio.sleep(0.5)
            await page.wait_for_load_state("networkidle", timeout=15000)
            try:
                await page.wait_for_selector('input[name^="acl_infoboard_"], div.acl', timeout=15000)
            except Exception:
                await asyncio.sleep(2)
            await asyncio.sleep(1)
            html = await page.content()
        finally:
            await browser.close()

    post_url = f"{base_url}/admin/dirs"
    acl_form_base = "%2Fdocs"
    acl_keys = list(parse_acl_from_html(html).keys())
    logger.debug("fetch_acl_form_via_browser -> html_len=%s parse_acl_keys=%s", len(html), acl_keys[:20] if acl_keys else [])
    return (html, post_url, acl_form_base, docs_n)


async def apply_acl_via_browser(
    base_url: str,
    cookies: dict[str, str],
    docs_n: str,
    acl_matrix: dict[str, list[int]],
) -> None:
    """
    Открыть форму «Настройки сервисов» в браузере, подставить ACL из acl_matrix
    (board_key -> list[group_id]), отправить форму из той же сессии. Устраняет 500 при POST через httpx.
    """
    if not acl_matrix:
        return
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise NetworkError(
            code="PLAYWRIGHT_NOT_INSTALLED",
            message="Для применения ACL через браузер: pip install playwright && playwright install chromium",
        ) from e

    url = f"{base_url}/admin/dir?n={docs_n}"
    logger.info("apply_acl_via_browser: применение ACL в браузере (POST из сессии) %s", url)
    set_val_js = "(el, val) => { if (el) el.value = val; }"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(_pw_cookies(cookies, base_url))
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("form, a", timeout=10000)
            btn = page.locator('input[name=setup2][type=submit], input[name=setup2][value*="Настройки"]')
            if await btn.count() > 0:
                await btn.first.click()
            else:
                sel = page.locator("select[name=setup2]")
                if await sel.count() > 0:
                    await sel.select_option(label="Настройки сервисов")
                    await page.locator('input[type="submit"], button[type="submit"]').first.click()
                else:
                    link = page.get_by_text("Настройки сервисов", exact=False).first
                    if await link.count() > 0:
                        await link.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_selector('input[name^="acl_infoboard_"]', state="attached", timeout=15000)
            await asyncio.sleep(0.3)
            for board_key, group_ids in acl_matrix.items():
                key_lower = board_key.lower()
                acl_val = acl_to_json(group_ids)
                for name in (f"acl_infoboard_{key_lower}", f"acl_infoboard_{key_lower}_view"):
                    inp = page.locator(f'input[name="{name}"]')
                    if await inp.count() > 0:
                        await inp.evaluate(set_val_js, acl_val)
                for name in (f"infoboard_{key_lower}", f"infoboard_{key_lower}_view"):
                    chk = page.locator(f'input[name="{name}"]')
                    if await chk.count() > 0:
                        try:
                            await chk.check()
                        except Exception:
                            pass
            await asyncio.sleep(0.2)
            save_btn = page.locator(
                'input[type="submit"][value*="Сохранить"], input[type="submit"][value*="Save"], '
                'button[type="submit"], form[action*="admin/dirs"] input[type="submit"]'
            ).first
            try:
                await save_btn.wait_for(state="visible", timeout=10000)
                await save_btn.click(timeout=15000)
            except Exception as click_err:
                alt_btn = page.get_by_role("button", name="Сохранить").or_(
                    page.get_by_role("button", name="Save")
                ).first
                if await alt_btn.count() > 0:
                    await alt_btn.click(timeout=15000)
                else:
                    submitted = await page.evaluate(
                        """() => {
                            const form = document.querySelector('form[action*="admin/dirs"]') || document.querySelector('form');
                            if (form) { form.requestSubmit(); return true; }
                            return false;
                        }"""
                    )
                    if not submitted:
                        raise NetworkError(
                            code="ACL_SAVE_FAILED",
                            message=f"Не удалось отправить форму: {click_err!r}",
                        ) from click_err
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(0.5)
            logger.debug("apply_acl_via_browser: форма отправлена успешно")
        finally:
            await browser.close()


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
