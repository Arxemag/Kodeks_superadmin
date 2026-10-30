"""Explicit, self-cleaning SSO acceptance on owner-authorized registration 465304.

Run inside auth-api with --portal-profile pointing to a private 0600 JSON file.
The profile and session secrets are never printed or copied to the report.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
import signal
from urllib.parse import urlencode, urljoin, urlsplit

import asyncpg
import httpx
from sqlalchemy import text
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from common.config import get_settings
from common.db import get_db_session, shutdown_db
from common.playwright_browser import open_chromium
from services.infoboards_service.admin_dirs import parse_form_pairs
from services.infoboards_service.cabinet_acl import apply_acl_via_browser, get_boards, get_docs_n_from_dirs
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient, _check_response
from services.users_service.dto import CreateUserDTO
from services.users_service.html_parser import (
    _Document, _form_controls, _successful, group_index, normalize_title,
    parse_groups_catalog, parse_user_state,
)
from services.users_service.reg_resolver import RegResolver
from services.users_service.service import UserService

REG = "465304"
ROLE = "Действующий пользователь"
MEMBERSHIP_TYPE = "Пользователь"
logging.disable(logging.CRITICAL)


class ProbeBlocked(Exception):
    pass


def require(condition, code):
    if not condition:
        raise AssertionError(code)


def safe_error(error):
    result = {"type": type(error).__name__}
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,100}", code):
        result["code"] = code
    if isinstance(error, (AssertionError, ProbeBlocked)):
        result["code"] = str(error)  # All these messages are local constant codes.
    state = getattr(error, "sqlstate", None)
    if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
        result["sqlstate"] = state
    return result


def delete_form(html, fields, base, expected_path):
    root = _Document(html).root
    candidates = []
    for form in (node for node in root.walk() if node.tag == "form"):
        controls = _form_controls(root, form)
        pairs = _successful(controls)
        if not fields <= {key for key, _ in pairs}:
            continue
        submitters = [node for node in controls if node.tag in {"input", "button"}
                      and (node.attrs.get("type") or "submit").lower() == "submit"
                      and node.attrs.get("name") == "del" and node.attrs.get("value") == "Удалить"]
        if len(submitters) != 1:
            continue
        target = urljoin(base + "/", (form.attrs.get("action") or "").strip())
        require(urlsplit(target).netloc == urlsplit(base).netloc, "DELETE_ORIGIN_INVALID")
        require(urlsplit(target).path == expected_path, "DELETE_PATH_INVALID")
        require((form.attrs.get("method") or "get").lower() == "post", "DELETE_METHOD_INVALID")
        candidates.append((target, pairs + [("del", "Удалить")]))
    require(len(candidates) == 1, "DELETE_FORM_UNVERIFIED")
    return candidates[0]


async def validate_enum(connection, table, column, label):
    rows = await connection.fetch("""SELECT e.enumlabel FROM pg_attribute a
        JOIN pg_enum e ON e.enumtypid=a.atttypid
        WHERE a.attrelid=$1::text::regclass AND a.attname=$2 ORDER BY e.enumsortorder""", table, column)
    if label not in {row["enumlabel"] for row in rows}:
        raise ProbeBlocked("PORTAL_ENUM_CONTRACT_UNVERIFIED")


async def probe(profile):
    uid, email = profile["uid"], profile["email"]
    require(bool(re.fullmatch(r"sso_accept_[A-Za-z0-9_]{6,64}", uid)), "TEST_UID_INVALID")
    require(email == uid + "@example.invalid", "TEST_EMAIL_INVALID")
    require(all(isinstance(profile.get(key), str) and profile[key]
                for key in ("portal_password", "password_hash", "catalog_password")), "TEST_PASSWORDS_REQUIRED")
    portal = profile.get("portal", "https://platforma.bravo-soft.ru").rstrip("/")
    require(portal in {"https://platforma.bravo-soft.ru", "https://platforma-release.bravo-soft.ru"}, "PORTAL_INVALID")
    evidence = Path(profile.get("evidence_dir", "/tmp/sso_acceptance")) / uid
    evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(evidence, 0o700)
    report = {"reg": REG, "test_uid": uid, "steps": [], "cleanup": [], "ok": False}
    stage = "preflight"
    resolver = RegResolver()
    portal_connection = None
    baseline_groups = baseline_boards = baseline_admin = None
    company_id = user_id = membership_id = board_id = group_id = None
    mutation_allowed = False
    board_title, group_title = uid + "_board", uid + "_group"
    settings = get_settings()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    signal_registered = False
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        signal_registered = True
    except (NotImplementedError, RuntimeError):
        pass

    def passed(name, **metadata):
        report["steps"].append({"name": name, "status": "PASS", **metadata})

    async def portal_db():
        nonlocal portal_connection
        if portal_connection is None or portal_connection.is_closed():
            portal_connection = await asyncpg.connect(**profile["pg"], timeout=15)
        return portal_connection

    try:
        await resolver.startup()
        base = await resolver.resolve_base_url(REG)
        require(base.rstrip("/") == "https://cabinet-05.kodeks.expert", "TEST_CATALOG_HOST_INVALID")
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            catalog = CatalogClient(http)
            auth = AuthClient(http, f"http://auth-api:{settings.PORT}")
            cookies = await auth.get_admin_cookies(REG)
            service = UserService(auth, catalog, resolver)

            async def groups():
                return parse_groups_catalog(await catalog.get_groups_page(base, cookies))

            async def boards():
                return await get_boards(base, http, cookies)

            async def admin_form(docs):
                response = await http.get(base + "/admin/dir", params={"n": docs}, cookies=cookies)
                _check_response(response)
                return parse_form_pairs(response.text, required_names={"path", "n"})

            async def graphql(query, variables):
                response = await http.post(base + "/infoboard/graphql?context=docs", cookies=cookies,
                                           json={"query": query, "variables": variables})
                _check_response(response)
                body = response.json()
                require(isinstance(body, dict) and not body.get("errors") and isinstance(body.get("data"), dict), "GRAPHQL_REJECTED")
                return body["data"]

            async def submit_delete(target, pairs):
                response = await http.post(target, content=urlencode(pairs), cookies=cookies,
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
                _check_response(response)

            docs = None
            try:
                connection = await portal_db()
                companies = await connection.fetch('''SELECT DISTINCT c.id FROM companies c
                    JOIN regs r ON r."companyId"=c.id
                    WHERE r.reg_number=$1 AND c.available IS TRUE AND c.mark_deletion IS FALSE''', REG)
                if len(companies) != 1 or companies[0]["id"] != 24:
                    raise ProbeBlocked("EXACT_ACTIVE_TEST_COMPANY_NOT_FOUND")
                company_id = companies[0]["id"]
                await validate_enum(connection, "users", "role", ROLE)
                await validate_enum(connection, "user_companies", "status", ROLE)
                await validate_enum(connection, "user_companies", "type", MEMBERSHIP_TYPE)
                require(await connection.fetchval("SELECT count(*) FROM users WHERE email=$1", email) == 0, "TEST_PORTAL_USER_COLLISION")
                baseline_groups, baseline_boards = await groups(), await boards()
                require(group_title not in baseline_groups and board_title not in baseline_boards, "TEST_CATALOG_COLLISION")
                require(await catalog.get_user_page(base, uid, cookies) is None, "TEST_CATALOG_USER_COLLISION")
                require(4 in baseline_groups.values(), "TEST_DOCS_GROUP_MISSING")
                response = await http.get(base + "/admin/dirs", cookies=cookies)
                _check_response(response)
                docs = get_docs_n_from_dirs(response.text)
                baseline_admin = await admin_form(docs)
                (evidence / "baseline.json").write_text(json.dumps({"groups": baseline_groups,
                    "boards": baseline_boards, "directory_form": baseline_admin}, ensure_ascii=False, indent=2))
                mutation_allowed = True
                passed("baseline_and_portal_enums_verified", company_id=company_id)

                stage = "catalog_entities"
                require(await catalog.create_group(base, group_title, cookies), "TEST_GROUP_NOT_CREATED")
                group_id = group_index(await groups())[normalize_title(group_title)]
                await service.handle_create(CreateUserDTO(reg=REG, uid=uid, psw=profile["catalog_password"],
                    name="SSO acceptance", grp=[4, group_id]))
                result = await graphql("mutation($input:ConfigInput){editConfig(input:$input){id title}}", {
                    "input": {"title": board_title, "author": "SSO acceptance", "widgets": "[]", "hiddenOnStartPage": True}})
                created = result.get("editConfig")
                require(isinstance(created, dict) and created.get("title") == board_title and created.get("id"), "TEST_BOARD_RESPONSE_INVALID")
                board_id = created["id"]
                require((await boards()).get(board_title) == board_id, "TEST_BOARD_NOT_CONFIRMED")
                await apply_acl_via_browser(base, cookies, docs, {board_id: [group_id]})
                passed("temporary_catalog_user_group_board_acl_created", board_id=board_id)

                stage = "portal_entities"
                async with connection.transaction():
                    user_id = await connection.fetchval('''INSERT INTO users
                        (email,password_hash,role,full_name,phone,available)
                        VALUES ($1,$2,$3,$4,$5,true) RETURNING id''', email, profile["password_hash"], ROLE, "SSO acceptance", "")
                    membership_id = await connection.fetchval('''INSERT INTO user_companies
                        ("companyId",status,mark_deletion,type,"userId","teAuth")
                        VALUES ($1,$2,false,$3,$4,$5) RETURNING id''', company_id, ROLE, MEMBERSHIP_TYPE, user_id, profile["catalog_password"])
                passed("temporary_portal_user_membership_created")

                stage = "portal_and_browser_sso"
                async with async_playwright() as playwright:
                    async with open_chromium(playwright) as browser:
                        context = await browser.new_context()
                        try:
                            require(not await context.cookies(base), "CATALOG_COOKIES_NOT_INITIAL_EMPTY")
                            page = await context.new_page()
                            captured = []

                            async def capture_login(response):
                                if (urlsplit(response.url).path == "/api/auth/login"
                                        and response.request.method == "POST" and 200 <= response.status < 300):
                                    try:
                                        body = await response.json()
                                        if isinstance(body, dict) and body.get("accessToken"):
                                            captured.append(body["accessToken"])
                                    except Exception:
                                        pass

                            page.on("response", capture_login)
                            await page.goto(portal, wait_until="domcontentloaded", timeout=45000)
                            # SSR can show usable-looking fields before the submit
                            # handler is hydrated. Never click a default GET form.
                            ui_reason = "LOGIN_CONTROLS_NOT_FOUND"
                            try:
                                await page.locator('input[type="password"]').first.wait_for(state="visible", timeout=12000)
                            except PlaywrightTimeoutError:
                                pass
                            email_input = page.locator('input[type="email"], input[name="email"]')
                            password_input = page.locator('input[type="password"]')
                            buttons = page.locator('button[type="submit"], input[type="submit"]')
                            ui_login = (await email_input.count() == 1 and await password_input.count() == 1
                                        and await buttons.count() == 1)
                            if ui_login:
                                ui_reason = "HYDRATED_LOGIN_HANDLER_NOT_CONFIRMED"
                                try:
                                    await page.wait_for_function("""() => {
                                        const input=document.querySelector('input[type="password"]');
                                        const form=input?.closest('form');
                                        if (!form) return false;
                                        if (typeof form.onsubmit === 'function') return true;
                                        for (const key of Object.keys(form)) {
                                            if (key.startsWith('__reactProps$') && typeof form[key]?.onSubmit === 'function') return true;
                                        }
                                        const vueEvents=[form._vei, ...Object.getOwnPropertySymbols(form)
                                            .filter(key => String(key).includes('_vei')).map(key => form[key])];
                                        if (vueEvents.some(events => events && Object.keys(events)
                                            .some(key => key.toLowerCase().includes('submit')))) return true;
                                        if (window.jQuery?._data?.(form,'events')?.submit?.length) return true;
                                        const action=new URL(form.action,location.href);
                                        return form.method.toLowerCase()==='post' && action.origin===location.origin
                                            && action.pathname==='/api/auth/login';
                                    }""", timeout=12000)
                                    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                                except PlaywrightTimeoutError:
                                    ui_login = False
                            if ui_login:
                                await email_input.fill(email)
                                await password_input.fill(profile["portal_password"])
                                await buttons.click()
                                for _ in range(100):
                                    if captured:
                                        break
                                    await asyncio.sleep(0.1)
                                require(bool(captured), "PORTAL_UI_LOGIN_FAILED")
                                token = captured[-1]
                            else:
                                login = await context.request.post(portal + "/api/auth/login", data={
                                    "email": email, "password": profile["portal_password"]})
                                require(200 <= login.status < 300, "PORTAL_API_LOGIN_FAILED")
                                body = await login.json()
                                require(isinstance(body, dict) and body.get("accessToken"), "PORTAL_ACCESS_TOKEN_MISSING")
                                token = body["accessToken"]
                            require(isinstance(token, str) and len(token.split(".")) == 3, "PORTAL_ACCESS_TOKEN_FORMAT_INVALID")
                            encoded_claims = token.split(".")[1]
                            claims = json.loads(base64.urlsafe_b64decode(encoded_claims + "=" * (-len(encoded_claims) % 4)))
                            require(isinstance(claims, dict) and claims.get("userId") == membership_id
                                    and claims.get("companyId") == company_id and claims.get("role") == ROLE,
                                    "PORTAL_ACCESS_TOKEN_IDENTITY_MISMATCH")
                            passed("portal_login", mode="UI" if ui_login else "API_only",
                                   **({"ui_fallback_reason": ui_reason} if not ui_login else {}))
                            require(not await context.cookies(base), "CATALOG_COOKIES_APPEARED_BEFORE_SSO")
                            if urlsplit(page.url).netloc != urlsplit(portal).netloc:
                                await page.goto(portal, wait_until="domcontentloaded")
                            target = base + "/docs/?nd=606025000&infoboard=" + board_id
                            result = await page.evaluate("""async ({token,link}) => {
                                const response=await fetch('/api/expert/sso-link',{method:'POST',credentials:'include',
                                    headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},
                                    body:JSON.stringify({link})});
                                return {status:response.status,body:await response.json()};
                            }""", {"token": token, "link": target})
                            require(200 <= result["status"] < 300 and isinstance(result.get("body"), dict), "PORTAL_SSO_LINK_REJECTED")
                            sso_url = result["body"].get("url")
                            require(isinstance(sso_url, str) and urlsplit(sso_url).netloc == urlsplit(base).netloc
                                    and urlsplit(sso_url).path == "/sso", "PORTAL_SSO_LINK_INVALID")
                            passed("portal_issued_one_time_link")
                            catalog_page = await context.new_page()
                            navigation = await catalog_page.goto(sso_url, wait_until="networkidle", timeout=45000)
                            require(navigation is not None and navigation.status == 200, "CATALOG_SSO_NAVIGATION_FAILED")
                            require(urlsplit(catalog_page.url).netloc == urlsplit(base).netloc
                                    and f"infoboard={board_id}" in catalog_page.url, "CATALOG_TARGET_MISMATCH")
                            require(await catalog_page.locator('form#authForm, form[name="authForm"]').count() == 0,
                                    "CATALOG_STILL_SHOWS_LOGIN")
                            board_result = await catalog_page.evaluate("""async () => {
                                const r=await fetch('/infoboard/graphql?context=docs',{method:'POST',credentials:'include',
                                    headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'{board{many{id title}}}'})});
                                return {status:r.status,body:await r.json()};
                            }""")
                            require(board_result["status"] == 200 and not board_result["body"].get("errors"), "CATALOG_GRAPHQL_FAILED")
                            found = board_result["body"].get("data", {}).get("board", {}).get("many", [])
                            require(any(board.get("id") == board_id and board.get("title") == board_title for board in found),
                                    "AUTHORIZED_TEST_BOARD_NOT_VISIBLE")
                            browser_cookies = await context.cookies(base)
                            sessions = [cookie for cookie in browser_cookies if cookie["name"] == "Auth"]
                            require(len(sessions) == 1, "CATALOG_AUTH_COOKIE_AMBIGUOUS")
                            session = sessions[0]
                            require(session["domain"] == urlsplit(base).hostname and session["httpOnly"]
                                    and session["secure"] and not session["value"].startswith('"'), "CATALOG_AUTH_COOKIE_ATTRIBUTES_INVALID")
                            identity = base64.b64decode(session["value"], validate=True).decode("utf-8").split(":", 1)[0]
                            require(identity == uid, "CATALOG_AUTH_IDENTITY_MISMATCH")
                            passed("fresh_browser_reached_authorized_test_board", cookie_host_only=True, cookie_http_only=True)
                            repeated = await context.request.get(sso_url, max_redirects=0)
                            require(repeated.status == 401 and repeated.headers.get("x-sso-error") == "SSO_TICKET_INVALID",
                                    "USED_TICKET_ACCEPTED")
                            passed("used_ticket_rejected")
                            other = await page.evaluate("""async ({token,link}) => {
                                const r=await fetch('/api/expert/sso-link',{method:'POST',credentials:'include',
                                    headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({link})});
                                return r.status;
                            }""", {"token": token, "link": "https://cabinet-04.kodeks.expert/docs/"})
                            require(other == 403, "CROSS_TENANT_PORTAL_LINK_ACCEPTED")
                            passed("cross_tenant_link_rejected_without_catalog_mutation")
                        finally:
                            await context.close()
            except asyncio.CancelledError:
                report["failure"] = {"stage": stage, "type": "CancelledError", "code": "PROBE_CANCELLED"}
            except Exception as error:
                report["failure"] = {"stage": stage, **safe_error(error)}
                report["blocked"] = isinstance(error, ProbeBlocked)
            finally:
                if mutation_allowed:
                    # Each cleanup step is independent; no failure skips later steps.
                    async def cleanup(name, action):
                        try:
                            await action()
                            report["cleanup"].append({"name": name, "status": "PASS"})
                        except Exception as error:
                            report["cleanup"].append({"name": name, "status": "FAIL", **safe_error(error)})

                    async def clear_acl():
                        current = await boards()
                        exact = current.get(board_title)
                        if exact is not None:
                            require(exact not in baseline_boards.values(), "CLEANUP_BOARD_EXISTED_BEFORE_TEST")
                            await apply_acl_via_browser(base, cookies, docs, {exact: []})

                    async def remove_board():
                        current = await boards()
                        exact = current.get(board_title)
                        if exact is not None:
                            require(exact not in baseline_boards.values(), "CLEANUP_BOARD_EXISTED_BEFORE_TEST")
                            if board_id is not None:
                                require(exact == board_id, "CLEANUP_BOARD_ID_CHANGED")
                            await graphql("mutation($id:String){deleteConfig(id:$id){id title}}", {"id": exact})
                        require(board_title not in await boards(), "CLEANUP_BOARD_REMAINS")

                    async def remove_catalog_user():
                        async with catalog.operation(base):
                            page = await catalog.get_user_page(base, uid, cookies)
                            if page is not None:
                                require(parse_user_state(page).fields.get("uid") == uid, "CLEANUP_USER_IDENTITY_MISMATCH")
                                target, pairs = delete_form(page, {"uid", "psw"}, base, "/users/users")
                                await submit_delete(target, pairs)
                            require(await catalog.get_user_page(base, uid, cookies) is None, "CLEANUP_CATALOG_USER_REMAINS")

                    async def remove_group():
                        async with catalog.operation(base):
                            current = await groups()
                            exact = group_index(current).get(normalize_title(group_title))
                            if exact is not None:
                                require(exact not in baseline_groups.values(), "CLEANUP_GROUP_EXISTED_BEFORE_TEST")
                                response = await http.get(base + "/users/grp", params={"grp": exact}, cookies=cookies)
                                _check_response(response)
                                target, pairs = delete_form(response.text, {"name", "gn"}, base, "/users/groups")
                                require(any(key == "name" and value == group_title for key, value in pairs), "CLEANUP_GROUP_NAME_MISMATCH")
                                require([value for key, value in pairs if key == "gn"] == [str(exact)], "CLEANUP_GROUP_ID_MISMATCH")
                                await submit_delete(target, pairs)
                            require(group_title not in await groups(), "CLEANUP_GROUP_REMAINS")

                    async def remove_catalog_ownership():
                        async with get_db_session() as db:
                            await db.execute(text("DELETE FROM catalog_user_group_ownership WHERE reg=:reg AND user_uid=:uid"), {"reg": REG, "uid": uid})
                            await db.commit()
                            count = await db.execute(text("SELECT count(*) FROM catalog_user_group_ownership WHERE reg=:reg AND user_uid=:uid"), {"reg": REG, "uid": uid})
                            require(count.scalar_one() == 0, "CLEANUP_OWNERSHIP_REMAINS")

                    async def remove_portal_membership():
                        connection = await portal_db()
                        own_user = await connection.fetchval("SELECT id FROM users WHERE email=$1", email)
                        if own_user is not None:
                            require(user_id is None or own_user == user_id, "CLEANUP_PORTAL_USER_ID_CHANGED")
                            async with connection.transaction():
                                await connection.execute('''DELETE FROM user_companies
                                    WHERE "userId"=$1 AND "companyId"=$2 AND ($3::integer IS NULL OR id=$3)''', own_user, company_id, membership_id)
                            require(await connection.fetchval('SELECT count(*) FROM user_companies WHERE "userId"=$1 AND "companyId"=$2', own_user, company_id) == 0,
                                    "CLEANUP_PORTAL_MEMBERSHIP_REMAINS")

                    async def remove_portal_user():
                        connection = await portal_db()
                        async with connection.transaction():
                            await connection.execute("DELETE FROM users WHERE email=$1 AND ($2::integer IS NULL OR id=$2)", email, user_id)
                        require(await connection.fetchval("SELECT count(*) FROM users WHERE email=$1", email) == 0, "CLEANUP_PORTAL_USER_REMAINS")

                    async def verify_baseline():
                        require(await groups() == baseline_groups, "BASELINE_GROUPS_CHANGED")
                        require(await boards() == baseline_boards, "BASELINE_BOARDS_CHANGED")
                        require(Counter(await admin_form(docs)) == Counter(baseline_admin), "BASELINE_DIRECTORY_CHANGED")

                    await cleanup("test_board_acl_cleared", clear_acl)
                    await cleanup("test_board_removed", remove_board)
                    await cleanup("test_catalog_user_removed", remove_catalog_user)
                    await cleanup("test_catalog_group_removed", remove_group)
                    await cleanup("test_catalog_ownership_removed", remove_catalog_ownership)
                    await cleanup("test_portal_membership_removed", remove_portal_membership)
                    await cleanup("test_portal_user_removed", remove_portal_user)
                    await cleanup("catalog_baseline_restored", verify_baseline)
    except asyncio.CancelledError:
        report["failure"] = {"stage": stage, "type": "CancelledError", "code": "PROBE_CANCELLED"}
    except Exception as error:
        report["failure"] = {"stage": stage, **safe_error(error)}
    finally:
        if signal_registered:
            loop.remove_signal_handler(signal.SIGTERM)
        try:
            if portal_connection is not None and not portal_connection.is_closed():
                await portal_connection.close()
        except Exception as error:
            report["cleanup"].append({"name": "portal_connection_closed", "status": "FAIL", **safe_error(error)})
        try:
            await shutdown_db()
        except Exception as error:
            report["cleanup"].append({"name": "catalog_database_closed", "status": "FAIL", **safe_error(error)})
        report["ok"] = "failure" not in report and mutation_allowed and all(
            item["status"] == "PASS" for item in report["cleanup"])
        try:
            (evidence / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        except OSError:
            report["ok"] = False
            report["report_write_failed"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["ok"] else 1


def main():
    parser = argparse.ArgumentParser(description="Owner-authorized self-cleaning SSO acceptance for reg 465304")
    parser.add_argument("--portal-profile", required=True, type=Path)
    args = parser.parse_args()
    require(args.portal_profile.is_file() and args.portal_profile.stat().st_mode & 0o077 == 0, "PROFILE_MUST_BE_PRIVATE_0600")
    profile = json.loads(args.portal_profile.read_text())
    raise SystemExit(asyncio.run(probe(profile)))


if __name__ == "__main__":
    main()
