"""
Добавить группу к кабинету (ACL) в каталоге.

Запуск:
  python scripts/add_cabinet_group.py --reg 350832 --list-cabinets   # список кабинетов
  python scripts/add_cabinet_group.py --reg 350832 --list-groups    # список групп
  python scripts/add_cabinet_group.py --reg 350832 --cabinet TEST --group "СМК"
  python scripts/add_cabinet_group.py --reg 350832 --cabinet TEST --group "СМК" --dry-run

  # Если форма ACL рендерится через JS (в браузере видна, в коде страницы — нет):
  python scripts/add_cabinet_group.py --reg 350832 --cabinet TEST --group "СМК" --use-browser
  # Или с явным URL:
  python scripts/add_cabinet_group.py --reg 350832 --cabinet TEST --group "СМК" --admin-url "https://.../admin/dirs?base=%2Fdocs" --use-browser

  pip install playwright && playwright install chromium   # один раз для --use-browser

  # Прямой POST без парсинга (форма известна, данные есть):
  python scripts/add_cabinet_group.py --reg 350832 --cabinet TEST --group "СМК" --direct

Требует: БД с reg_services (если --reg) или --base-url напрямую.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

from common.config import get_settings
from common.exceptions import AuthError, NetworkError
from common.http import _default_headers, request_with_retry
from common.logger import get_logger
from services.infoboards_service.admin_dirs import (
    acl_to_json,
    parse_acl_from_html,
    parse_full_form_from_html,
    parse_grps_group_ids_from_html,
)
from services.infoboards_service.cabinet_acl import (
    build_acl_form_data,
    fetch_acl_form_via_http,
    get_boards,
    get_docs_n_from_dirs,
    post_acl_form,
)
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.html_parser import parse_groups_catalog


logger = get_logger("add_cabinet_group")
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Добавить группу к кабинету (ACL)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--reg", help="reg из reg_services")
    g.add_argument("--base-url", help="URL каталога напрямую")
    p.add_argument(
        "--list-cabinets",
        action="store_true",
        help="Вывести список кабинетов и выйти",
    )
    p.add_argument(
        "--list-groups",
        action="store_true",
        help="Вывести список групп (GET /users/groups) и выйти",
    )
    p.add_argument(
        "--list-groups-docs",
        action="store_true",
        help="Вывести группы каталога docs из GET /admin/dir?n=2 (поля grps_1, grps_5, …)",
    )
    p.add_argument("--cabinet", help="Название кабинета (например TEST)")
    p.add_argument("--group", help="Название группы (например СМК)")
    p.add_argument(
        "--dump-form",
        metavar="FILE",
        help="Сохранить отправляемую форму в файл для отладки",
    )
    p.add_argument(
        "--dump-html",
        metavar="FILE",
        help="Сохранить HTML страницы /admin/dir?n=X для отладки парсера",
    )
    p.add_argument(
        "--no-cmd",
        action="store_true",
        help="Не добавлять cmd=save (если форма не принимает)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Выполнить всё кроме POST: auth, группы, кабинеты, сбор формы. Форма выводится в stdout.",
    )
    p.add_argument(
        "--admin-url",
        metavar="URL",
        help="URL страницы с формой ACL кабинетов (если автоопределение не сработало). Пример: https://catalog/admin/dirs?base=%2Fusers",
    )
    p.add_argument(
        "--use-browser",
        action="store_true",
        help="Получить HTML через Playwright (для JS-рендеринга). Форма acl_infoboard_* видна в DOM, но не в исходном HTML.",
    )
    p.add_argument(
        "--no-empty-fields",
        action="store_true",
        help="Не отправлять поля с пустым значением (для обхода 500 при лишних empty params).",
    )
    p.add_argument(
        "--direct",
        action="store_true",
        help="Прямой POST без парсинга. Группы и кабинеты из API, форма строится из известной структуры.",
    )
    return p.parse_args()


def _pw_cookies(cookies: dict[str, str], base_url: str) -> list[dict]:
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    domain = parsed.netloc or parsed.path.split("/")[0] or "localhost"
    return [{"name": k, "value": v, "domain": domain, "path": "/"} for k, v in cookies.items()]


async def _fetch_html_via_browser(url: str, cookies: dict[str, str], base_url: str) -> str:
    """Получить HTML страницы после выполнения JS (Playwright)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(_pw_cookies(cookies, base_url))
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            try:
                await page.wait_for_selector('input[name^="acl_infoboard_"]', timeout=15000)
            except Exception:
                await page.wait_for_selector("div.acl, form", timeout=5000)
            await asyncio.sleep(1)
            return await page.content()
        finally:
            await browser.close()


async def _fetch_setup_response_via_browser(
    base_url: str, cookies: dict[str, str], docs_n: str
) -> str:
    """
    В браузере: открыть /admin/dir?n=X, отправить форму с setup2=Настройки сервисов,
    вернуть HTML страницы-ответа (на ней форма ACL, после выполнения JS).
    """
    from playwright.async_api import async_playwright

    url = f"{base_url}/admin/dir?n={docs_n}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(_pw_cookies(cookies, base_url))
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("form, a", timeout=10000)
            submitted = False
            # setup2 может быть кнопка: <input type="submit" name="setup2" value="Настройки сервисов"/>
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
            return await page.content()
        finally:
            await browser.close()


async def _submit_acl_via_browser(
    base_url: str,
    cookies: dict[str, str],
    docs_n: str,
    board_key: str,
    group_id: int,
) -> bool:
    """
    В том же браузере: открыть «Настройки сервисов», подставить группу в ACL кабинета,
    нажать «Сохранить». POST уходит из сессии браузера — обходим 500 из-за проверки сессии.
    """
    from playwright.async_api import async_playwright

    url = f"{base_url}/admin/dir?n={docs_n}"
    acl_val = acl_to_json([group_id])
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(_pw_cookies(cookies, base_url))
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("form", timeout=10000)
            btn = page.locator('input[name=setup2][type=submit], input[name=setup2][value*="Настройки"]')
            if await btn.count() > 0:
                await btn.first.click()
            else:
                link = page.get_by_text("Настройки сервисов", exact=False).first
                if await link.count() > 0:
                    await link.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_selector(
                'input[name^="acl_infoboard_"]', state="attached", timeout=15000
            )
            await asyncio.sleep(0.3)
            # Скрытые поля: fill() требует видимый элемент; задаём value через JS
            set_val = "(el, val) => { el.value = val; }"
            inp = page.locator(f'input[name="acl_infoboard_{board_key}"]')
            if await inp.count() > 0:
                await inp.evaluate(set_val, acl_val)
            inp_view = page.locator(f'input[name="acl_infoboard_{board_key}_view"]')
            if await inp_view.count() > 0:
                await inp_view.evaluate(set_val, acl_val)
            infoboard = page.locator(f'input[name="infoboard_{board_key}"]')
            if await infoboard.count() > 0:
                try:
                    await infoboard.check()
                except Exception:
                    pass
            infoboard_view = page.locator(f'input[name="infoboard_{board_key}_view"]')
            if await infoboard_view.count() > 0:
                try:
                    await infoboard_view.check()
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
                try:
                    alt_btn = page.get_by_role("button", name="Сохранить").or_(
                        page.get_by_role("button", name="Save")
                    ).first
                    if await alt_btn.count() > 0:
                        await alt_btn.click(timeout=15000)
                    else:
                        raise click_err
                except Exception:
                    logger.debug("Кнопка по селектору/роли не сработала, отправляем форму через JS: %s", click_err)
                    submitted = await page.evaluate(
                        """() => {
                            const form = document.querySelector('form[action*="admin/dirs"]') || document.querySelector('form');
                            if (form) { form.requestSubmit(); return true; }
                            return false;
                        }"""
                    )
                    if not submitted:
                        raise RuntimeError(
                            f"Не удалось нажать «Сохранить» и отправить форму. Ошибка: {click_err!r}"
                        ) from click_err
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(0.5)
            return True
        finally:
            await browser.close()


async def _get_base_url(reg: str) -> str:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.ext.asyncio import AsyncSession

    settings = get_settings()
    tbl = settings.DB_TABLE_REG_SERVICES
    engine = create_async_engine(settings.DB_URL, pool_size=1)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as session:
        res = await session.execute(
            text(f"SELECT base_url FROM {tbl} WHERE reg_number = :r"),
            {"r": reg},
        )
        val = res.scalar_one_or_none()
    await engine.dispose()
    if not val:
        raise AuthError(code="REG_NOT_FOUND", message=f"reg {reg!r} not found")
    return str(val).rstrip("/")


async def _admin_login_direct(base_url: str, client: httpx.AsyncClient) -> dict[str, str]:
    """Прямой логин в каталог (для --base-url, без Auth API)."""
    settings = get_settings()
    url = f"{base_url}/users/login.asp"
    resp = await request_with_retry(
        client,
        "POST",
        url,
        data={
            "user": settings.ADMIN_LOGIN,
            "pass": settings.ADMIN_PASSWORD,
            "path": "/admin",
        },
        headers={"Origin": base_url, "Referer": f"{base_url}/"},
    )
    if resp.status_code >= 400:
        raise AuthError(code="ADMIN_AUTH_FAILED", message="Admin login failed")
    return dict(client.cookies)


async def _get_groups(base_url: str, cookies: dict[str, str], client: httpx.AsyncClient) -> dict[str, int]:
    url = f"{base_url}/users/groups"
    resp = await client.get(url, cookies=cookies, params={"_": str(time.time_ns())})
    if resp.status_code >= 400:
        raise NetworkError(code="GROUPS_FAILED", message=f"GET groups status={resp.status_code}")
    return parse_groups_catalog(resp.text or "")


async def _run() -> None:
    args = _parse_args()

    if args.reg:
        base_url = await _get_base_url(args.reg)
    else:
        base_url = args.base_url.rstrip("/")

    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT,
        follow_redirects=True,
        headers=_default_headers(),
    ) as client:
        if args.reg:
            auth_client = AuthClient(http_client=client, auth_service_url=settings.AUTH_SERVICE_URL)
            cookies = await auth_client.get_admin_cookies(args.reg)
        else:
            cookies = await _admin_login_direct(base_url, client)
        groups = await _get_groups(base_url, cookies, client)
        boards = await get_boards(base_url, client, cookies)

        if args.list_cabinets:
            print("Кабинеты:")
            for title, bid in sorted(boards.items(), key=lambda x: x[0].lower()):
                print(f"  {bid}: {title}")
            return
        if args.list_groups:
            print("Группы:")
            for name, gid in sorted(groups.items(), key=lambda x: x[0].lower()):
                print(f"  {gid}: {name}")
            return

        if args.list_groups_docs:
            dirs_url = f"{base_url}/admin/dirs"
            resp = await client.get(dirs_url, cookies=cookies)
            if resp.status_code >= 400:
                raise NetworkError(code="ADMIN_GET_FAILED", message=f"GET /admin/dirs status={resp.status_code}")
            dirs_html = resp.text or ""
            docs_n = get_docs_n_from_dirs(dirs_html)
            dir_url = f"{base_url}/admin/dir?n={docs_n}"
            resp = await client.get(dir_url, cookies=cookies)
            if resp.status_code >= 400:
                raise NetworkError(code="ADMIN_GET_FAILED", message=f"GET {dir_url} status={resp.status_code}")
            page_html = resp.text or ""
            doc_grp_ids = parse_grps_group_ids_from_html(page_html)
            id_to_name = {gid: name for name, gid in groups.items()}
            print("Группы каталога docs (из grps_* на GET /admin/dir?n=2):")
            for gid in doc_grp_ids:
                name = id_to_name.get(gid, "?")
                print(f"  {gid}: {name}")
            return

        if not args.cabinet or not args.group:
            raise ValueError("Укажите --cabinet и --group (или --list-cabinets / --list-groups)")

        group_id = groups.get(args.group)
        if group_id is None:
            logger.info(f"Группа {args.group!r} не найдена, создаём...")
            catalog_client = CatalogClient(http_client=client)
            await catalog_client.create_group(base_url, args.group, cookies)
            for _ in range(5):
                groups = await _get_groups(base_url, cookies, client)
                group_id = groups.get(args.group)
                if group_id is not None:
                    logger.info(f"Группа {args.group!r} создана, id={group_id}")
                    break
                await asyncio.sleep(0.3)
            if group_id is None:
                raise ValueError(
                    f"Группа {args.group!r} создана, но не появилась в списке. "
                    f"Попробуйте позже. Доступные: {list(groups.keys())[:15]}"
                )

        board_id = boards.get(args.cabinet)
        if board_id is None:
            similar = [k for k in boards if args.cabinet.lower() in k.lower()]
            if similar:
                board_id = boards.get(similar[0])
                logger.info(f"Кабинет по подстроке: {args.cabinet!r} -> {similar[0]!r} (id={board_id})")
            else:
                raise ValueError(
                    f"Кабинет {args.cabinet!r} не найден. Доступные: {list(boards.keys())}"
                )

        logger.info(f"Группа {args.group!r} -> id={group_id}, кабинет {args.cabinet!r} -> id={board_id}")

        acl_saved_via_browser = False
        if args.direct:
            post_url = f"{base_url}/admin/dirs"
            board_key = board_id.lower()
            acl_val = acl_to_json([group_id])
            form_data = [
                ("base", "/docs"),
                ("n", "2"),
                ("cmd", "save"),
                (f"acl_infoboard_{board_key}", acl_val),
                (f"acl_infoboard_{board_key}_view", acl_val),
                (f"infoboard_{board_key}", "on"),
                (f"infoboard_{board_key}_view", "on"),
            ]
            logger.info(f"--direct: POST {post_url} с {len(form_data)} полями")
        else:
            import re

            acl_form_base = ""
            if args.admin_url:
                dir_url = args.admin_url
                base_m = re.search(r"[?&]base=([^&'\"]+)", dir_url, re.I)
                if base_m:
                    acl_form_base = base_m.group(1)
                if args.use_browser:
                    logger.info("Загрузка страницы через браузер (JS-рендеринг)...")
                    html = await _fetch_html_via_browser(dir_url, cookies, base_url)
                else:
                    resp = await client.get(dir_url, cookies=cookies)
                    if resp.status_code >= 400:
                        raise NetworkError(code="ADMIN_GET_FAILED", message=f"GET {dir_url} status={resp.status_code}")
                    html = resp.text or ""
                post_url = dir_url.split("?")[0].rstrip("/") or dir_url
                has_acl = "acl_infoboard_" in html or bool(parse_acl_from_html(html))
                docs_n = "2"
                found_acl_url = dir_url if has_acl else ""
            else:
                html, post_url, acl_form_base, docs_n = await fetch_acl_form_via_http(
                    base_url, client, cookies
                )
                has_acl = "acl_infoboard_" in html or bool(parse_acl_from_html(html))
                found_acl_url = f"{base_url}/admin/dir?n={docs_n}" if has_acl else ""
                if not has_acl:
                    logger.info(
                        "В ответе POST /admin/dir нет полей acl_infoboard_ (форма может подставляться через JS). "
                        "Попробуйте --use-browser или --dump-html FILE для проверки."
                    )
                urls_to_try = [
                    f"{base_url}/admin/dirs?base=%2Fdocs",
                    f"{base_url}/admin/dirs?base=%2Fusers",
                    f"{base_url}/admin/dirs",
                ]
                dirs_base_m = re.search(r'href=["\']([^"\']*admin/dirs\?base=[^"\']*)["\']', html, re.I)
                if dirs_base_m:
                    raw = dirs_base_m.group(1)
                    abs_url = f"{base_url.rstrip('/')}{raw}" if raw.startswith("/") else (raw if raw.startswith("http") else f"{base_url}/{raw.lstrip('/')}")
                    urls_to_try.insert(0, abs_url)
                if not has_acl:
                    if args.use_browser:
                        logger.info("Браузер: открываю /admin/dir?n=%s и отправляю форму «Настройки сервисов»...", docs_n)
                        try:
                            check = await _fetch_setup_response_via_browser(base_url, cookies, docs_n)
                            if "acl_infoboard_" in check or bool(parse_acl_from_html(check)):
                                html = check
                                has_acl = True
                                found_acl_url = f"{base_url}/admin/dir?n={docs_n}"
                                logger.info("Форма ACL получена из ответа браузера (POST setup2)")
                            else:
                                logger.debug("В ответе браузера (setup2) нет acl_infoboard_, пробуем GET urls...")
                        except Exception as e:
                            logger.debug("Браузер (setup2): %s", e)
                        if not has_acl:
                            logger.info("Загрузка через браузер (GET)...")
                            for try_url in urls_to_try:
                                try:
                                    check = await _fetch_html_via_browser(try_url, cookies, base_url)
                                    if "acl_infoboard_" in check or bool(parse_acl_from_html(check)):
                                        html = check
                                        has_acl = True
                                        found_acl_url = try_url
                                        logger.info(f"Форма ACL найдена: {try_url}")
                                        break
                                except Exception as e:
                                    logger.debug(f"Браузер {try_url}: {e}")
                                    continue
                    else:
                        for try_url in urls_to_try:
                            resp = await client.get(try_url, cookies=cookies)
                            if resp.status_code == 200:
                                check = resp.text or ""
                                if "acl_infoboard_" in check or bool(parse_acl_from_html(check)):
                                    html = check
                                    has_acl = True
                                    found_acl_url = try_url
                                    logger.info(f"Форма ACL найдена: {try_url}")
                                    break
                dir_url = f"{base_url}/admin/dir (n={docs_n}, setup2=Настройки сервисов)"
                if has_acl and found_acl_url:
                    base_m = re.search(r"base=([^&'\"]+)", found_acl_url, re.I)
                    if base_m:
                        acl_form_base = base_m.group(1)
                if not acl_form_base:
                    acl_form_base = "%2Fdocs"
                logger.debug(f"POST на /admin/dirs с base={acl_form_base}")
                logger.info(f"Страница: n={docs_n} has_acl={has_acl!r} post_url={post_url}")
                if args.use_browser and has_acl:
                    try:
                        logger.info("Сохранение ACL через браузер (та же сессия)...")
                        await _submit_acl_via_browser(
                            base_url, cookies, docs_n, board_id.lower(), group_id
                        )
                        acl_saved_via_browser = True
                        print("OK")
                        logger.info("Группа %r добавлена к кабинету %r (сохранено через браузер)", args.group, args.cabinet)
                        return
                    except Exception as e:
                        logger.warning("Сохранение через браузер не удалось: %s. Пробуем POST формой.", e)
            if args.dump_html:
                from pathlib import Path
                Path(args.dump_html).write_text(html, encoding="utf-8")
                logger.info(f"HTML сохранён в {args.dump_html}")

            board_key = board_id.lower()
            extra_ids = {bid.lower() for bid in boards.values()}
            form_data = build_acl_form_data(
                html, acl_form_base, board_key, group_id, extra_ids,
                no_cmd=args.no_cmd, no_empty_fields=args.no_empty_fields,
            )
            logger.info(f"Распознано полей формы: {len(form_data)}")
            if not any(k.startswith("acl_infoboard_") for k, _ in form_data):
                raise ValueError(
                    "Форма не содержит acl_infoboard_*. Страница не та. "
                    "Укажите в каталоге URL страницы с настройками ACL кабинетов и --dump-html."
                )
        if args.no_cmd:
            form_data = [(k, v) for k, v in form_data if k != "cmd"]
        if args.no_empty_fields:
            form_data = [(k, v) for k, v in form_data if v or k in ("base", "n", "cmd")]
            logger.debug(f"--no-empty-fields: осталось {len(form_data)} полей")
        logger.debug(f"POST {post_url} form keys (все): {[k for k, _ in form_data]}")

        if args.dump_form:
            from pathlib import Path
            Path(args.dump_form).write_text(
                "\n".join(f"{k}={v}" for k, v in form_data),
                encoding="utf-8",
            )
            logger.info(f"Форма сохранена в {args.dump_form}")

        if args.dry_run:
            print("=== dry-run: форма (POST не выполнялся) ===")
            print(f"Полей в форме: {len(form_data)}")
            sys_fields = [k for k, _ in form_data if k in (
                "packprocess", "acl_packprocess", "acl_infoboardsedit",
                "acl_inspectactuallinks", "inspectionactuallinks", "n", "cmd",
            )]
            if sys_fields:
                print(f"Системные поля: {sys_fields}")
            for k, v in form_data:
                safe = "***" if k in ("psw",) else (v[:80] + "..." if len(v) > 80 else v)
                print(f"  {k}={safe}")
            print("=== конец ===")
            return

        board_key = board_id.lower()
        our_fields = [(k, v) for k, v in form_data if board_key in k.lower()]
        logger.debug(f"Поля для кабинета {board_key!r} в POST: {our_fields}")
        if args.use_browser and not acl_saved_via_browser:
            logger.info("Отправка формы через HTTP POST (fallback после неудачи браузера)")
        await post_acl_form(base_url, client, cookies, post_url, form_data, log=logger)

    print("OK")
    logger.info(
        "Группа %r добавлена к кабинету %r (сохранено через POST %s)",
        args.group, args.cabinet, post_url,
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
