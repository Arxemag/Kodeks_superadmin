"""
Общая логика «добавить группу в кабинет» (ACL): кабинеты, форма, POST.

Используется: scripts/add_cabinet_group.py (CLI), InitCompanyService (get_boards).
Константы и HTTP-путь получения формы — единый источник правды.
При JS-рендеринге формы используется fetch_acl_form_via_browser (Playwright).
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
from typing import Awaitable, Callable
from urllib.parse import urlparse, urlencode

import httpx

from common.exceptions import NetworkError, ParseError
from common.logger import get_logger
from common.playwright_browser import open_chromium
from services.infoboards_service.admin_dirs import (
    add_group_to_acl_json,
    build_admin_dirs_form,
    parse_acl_from_html,
    parse_form_pairs,
    acl_control_names,
    acl_checkbox_values,
    setup_form_pairs,
    verify_acl_form,
    acl_values,
    acl_write_needed,
)
from services.users_service.html_parser import _Document, normalize_title


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
    try:
        if body.get("errors"):
            raise ValueError
        many = body["data"]["board"]["many"]
        if not isinstance(many, list):
            raise ValueError
        result = {}
        normalized = {}
        for item in many:
            title, board_id = str(item["title"]).strip(), str(item["id"]).strip()
            if not title or not board_id or item["title"] is None or item["id"] is None:
                raise ValueError
            key = normalize_title(title)
            if key in normalized and normalized[key] != board_id:
                raise ParseError(code="AMBIGUOUS_CABINET", message=f"Several catalog cabinets match title {title!r}")
            normalized[key] = board_id
            result[title] = board_id
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ParseError(code="INFOBOARDS_PARSE_ERROR", message="Catalog did not return a complete valid cabinet list") from error


def get_docs_n_from_dirs(dirs_html: str) -> str:
    """Из HTML страницы /admin/dirs извлекает n для ссылки на /docs/ (например '2')."""
    from urllib.parse import parse_qs, urlsplit
    matches = []
    for node in _Document(dirs_html).root.walk():
        if node.tag != "a" or node.text().strip() != "/docs/":
            continue
        target = urlsplit(node.attrs.get("href") or "")
        values = parse_qs(target.query).get("n", [])
        if target.path.rstrip("/").endswith("admin/dir") and len(values) == 1 and values[0].isdigit():
            matches.append(values[0])
    if len(matches) != 1:
        raise ParseError(code="ACL_DIRECTORY_UNKNOWN", message="A unique /docs/ directory link was not found")
    return matches[0]


async def fetch_acl_form_via_http(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    *,
    docs_n: str | None = None,
) -> tuple[str, str, str, str]:
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
    setup_form = setup_form_pairs(page_html)
    encoded_setup = urlencode(setup_form)
    logger.debug("fetch_acl_form_via_http POST %s/admin/dir setup_form_keys=%s", base_url, [name for name, _ in setup_form])
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
            message="Для загрузки формы с JS установите: pip install playwright",
        ) from e

    url = f"{base_url}/admin/dir?n={docs_n}"
    logger.info("fetch_acl_form_via_browser: загрузка формы ACL через браузер (Playwright) %s", url)
    async with async_playwright() as p:
        async with open_chromium(p, logger=logger) as browser:
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

    post_url = f"{base_url}/admin/dirs"
    acl_form_base = "%2Fdocs"
    acl_keys = list(parse_acl_from_html(html).keys())
    logger.debug("fetch_acl_form_via_browser -> html_len=%s parse_acl_keys=%s", len(html), acl_keys[:20] if acl_keys else [])
    return (html, post_url, acl_form_base, docs_n)


async def _open_acl_browser_page(page, url: str) -> str:
    """Reopen settings using the existing catalog navigation, then read fresh DOM."""
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_selector("form, a", timeout=10000)
    button = page.locator('input[name=setup2][type=submit], input[name=setup2][value*="Настройки"]')
    if await button.count() == 1:
        await button.click()
    else:
        select = page.locator("select[name=setup2]")
        if await select.count() == 1:
            await select.select_option(label="Настройки сервисов")
            await page.locator('input[type="submit"], button[type="submit"]').first.click()
        else:
            link = page.get_by_text("Настройки сервисов", exact=False)
            if await link.count() != 1:
                raise ParseError(code="ACL_SETUP_CONTROL_UNKNOWN", message="A unique service-settings navigation control was not found")
            await link.click()
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_selector('input[name^="acl_infoboard_"]', state="attached", timeout=15000)
    # JS can update .value/.checked without changing the serialized attributes.
    # Read live control properties into a detached snapshot; the original DOM
    # and its handlers remain untouched for native submission.
    html = await page.evaluate("""() => {
        const copy = document.documentElement.cloneNode(true);
        const originals = document.querySelectorAll('input, textarea, option');
        const clones = copy.querySelectorAll('input, textarea, option');
        originals.forEach((el, i) => {
            const node = clones[i];
            if (el.tagName === 'INPUT') {
                node.setAttribute('value', el.value);
                node.toggleAttribute('checked', el.checked);
            } else if (el.tagName === 'OPTION') node.toggleAttribute('selected', el.selected);
            else node.textContent = el.value;
        });
        return copy.outerHTML;
    }""")
    parse_form_pairs(html, acl=True)
    return html


async def apply_acl_via_browser(
    base_url: str,
    cookies: dict[str, str],
    docs_n: str,
    acl_matrix: dict[str, list[int]],
    *,
    field_overrides: dict[str, list[int]] | None = None,
    prepare_field_overrides: Callable[[str], Awaitable[dict[str, list[int]]]] | None = None,
) -> None:
    """Overlay a complete DOM form, submit its actual Save button and reread."""
    if not acl_matrix and not field_overrides and prepare_field_overrides is None:
        return
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise NetworkError(code="PLAYWRIGHT_NOT_INSTALLED", message="Install Playwright to load the catalog ACL form") from error
    url = f"{base_url}/admin/dir?n={docs_n}"
    async with async_playwright() as playwright:
        async with open_chromium(playwright, logger=logger) as browser:
            context = await browser.new_context()
            await context.add_cookies(_pw_cookies(cookies, base_url))
            page = await context.new_page()
            html = await _open_acl_browser_page(page, url)
            pairs = parse_form_pairs(html, acl=True, include_save=True)
            resolved_fields = field_overrides or {}
            if prepare_field_overrides is not None:
                resolved_fields = await prepare_field_overrides(html)
            expected = build_admin_dirs_form(pairs, acl_matrix, available_controls=acl_control_names(html), checkbox_values=acl_checkbox_values(html), field_overrides=resolved_fields)
            if not acl_write_needed(expected, html):
                return
            changes = {}
            expected_by_name = dict(expected)
            controls = acl_control_names(html)
            targets = set(resolved_fields)
            for board in acl_matrix:
                targets.update((f"acl_infoboard_{board.lower()}", f"acl_infoboard_{board.lower()}_view"))
            for target in targets:
                for name in (target, target.removeprefix("acl_")):
                    if name in controls:
                        changes[name] = expected_by_name.get(name)
            # Native form submission preserves repeated/unknown controls and the
            # actual clicked submitter. Mutate only existing target controls.
            await page.evaluate(
                """changes => {
                    const forms = Array.from(document.forms).filter(form =>
                        Array.from(form.elements).some(el => (el.name || '').startsWith('acl_infoboard_')));
                    if (forms.length !== 1) throw new Error('Ambiguous ACL form');
                    for (const [name, value] of Object.entries(changes)) {
                        const inputs = Array.from(forms[0].elements).filter(el => el.name === name);
                        if (!inputs.length) throw new Error('Missing target ACL control');
                        for (const el of inputs) {
                            if (el.type === 'checkbox' || el.type === 'radio') el.checked = value !== null;
                            else el.value = value;
                        }
                    }
                }""",
                changes,
            )
            form = page.locator('form').filter(has=page.locator('input[name^="acl_infoboard_"]'))
            if await form.count() != 1:
                raise ParseError(code="ACL_FORM_INVALID", message="A unique native ACL form was not found")
            save = form.locator('input[type="submit"][value*="Сохранить"], input[type="submit"][value*="Save"], input[type="submit"][value="save"], input[type="submit"][value="Установить"], button[type="submit"]')
            if await save.count() != 1:
                raise ParseError(code="ACL_SAVE_CONTROL_UNKNOWN", message="A unique actual Save button was not found")
            try:
                await save.click(timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as error:
                raise ParseError(code="ACL_WRITE_UNCERTAIN", message="Browser ACL submit outcome is unknown; inspect the catalog before retrying") from error
            try:
                actual_html = await _open_acl_browser_page(page, url)
            except Exception as error:
                raise ParseError(code="ACL_WRITE_UNCERTAIN", message="Browser ACL submit returned, but its result cannot be read; inspect the catalog before retrying") from error
            verify_acl_form(expected, actual_html)

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
    if no_cmd or no_empty_fields:
        raise ParseError(code="ACL_FORM_INVALID", message="Dropping command or empty fields is incompatible with complete-form preservation")
    full_form = parse_form_pairs(html, acl=True, include_save=True)
    target = f"acl_infoboard_{board_key.lower()}"
    values = dict(full_form)
    if target not in values:
        raise ParseError(code="ACL_BOARD_NOT_FOUND", message=f"Required ACL field is absent: {target!r}")
    new_group_ids = add_group_to_acl_json(values[target], group_id)
    form_data = build_admin_dirs_form(
        full_form, {board_key: new_group_ids}, available_controls=acl_control_names(html),
        checkbox_values=acl_checkbox_values(html),
    )
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
    before_html, _, _, docs_n = await fetch_acl_form_via_http(base_url, client, cookies)
    before_pairs = parse_form_pairs(before_html, acl=True, include_save=True)
    # Even callers passing a direct form must retain all unrelated live fields.
    unrelated = lambda pairs: Counter((name, value) for name, value in pairs if not name.startswith(("acl_infoboard_", "infoboard_")))
    if unrelated(before_pairs) - unrelated(form_data):
        raise ParseError(code="ACL_FORM_INCOMPLETE", message="Submitted form drops or changes unrelated live controls")
    before_acl = acl_values(before_pairs)
    submitted_acl = acl_values(form_data)
    if not before_acl.keys() <= submitted_acl.keys():
        raise ParseError(code="ACL_FORM_INCOMPLETE", message="Submitted form drops unrelated board ACLs")
    encoded = urlencode(form_data)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if base_url:
        headers["Referer"] = f"{base_url}/admin/dir"
        headers["Origin"] = base_url.rstrip("/")
    try:
        resp = await client.post(post_url, content=encoded, cookies=cookies, headers=headers)
    except httpx.RequestError as error:
        raise ParseError(code="ACL_WRITE_UNCERTAIN", message="ACL POST outcome is unknown; inspect the catalog before retrying") from error
    body = resp.text or ""
    (log or logger).debug("ACL POST response: status=%s body_len=%s", resp.status_code, len(body))
    if resp.status_code >= 500:
        raise ParseError(code="ACL_WRITE_UNCERTAIN", message=f"ACL POST returned HTTP {resp.status_code}; inspect the catalog before retrying")
    if resp.status_code >= 400:
        err_msg = f"POST {post_url} status={resp.status_code}"
        if body and resp.status_code >= 500:
            err_msg += f" body={repr(body[:500].strip()[:200])}"
        raise NetworkError(code="ADMIN_POST_FAILED", message=err_msg)
    try:
        actual_html, _, _, _ = await fetch_acl_form_via_http(base_url, client, cookies, docs_n=docs_n)
    except Exception as error:
        raise ParseError(code="ACL_WRITE_UNCERTAIN", message="ACL POST returned, but its result cannot be read; inspect the catalog before retrying") from error
    verify_acl_form(form_data, actual_html)


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
