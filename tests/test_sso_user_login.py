"""Пользовательский вход SSO с реальным HTTP cookie jar и изолированным транспортом."""
from __future__ import annotations

import asyncio
import base64
import html
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

import httpx
import pytest

from common.exceptions import AuthError, NetworkError, ParseError
from services.auth_service.service import AuthService
import services.auth_service.service as auth_module


@pytest.fixture
def setup_login(monkeypatch):
    def create(*, uid="employee", password="user&secret", session=True,
               login_form=False, slow=False, base="https://catalog.test", legacy=False,
               form_action=None, form_method="post", malformed=False,
               form_status=200, extra_fields="", ajax_path="/docs", ajax_present=True):
        settings = SimpleNamespace(ADMIN_LOGIN="admin", ADMIN_PASSWORD="admin-secret",
                                   AUTH_HARD_TIMEOUT=0.02 if slow else 5)
        service = AuthService(db=None, settings=settings)
        monkeypatch.setattr(AuthService, "_get_base_url", AsyncMock(return_value=base))
        requests = []
        cancelled = []
        action = f' action="{form_action}"' if form_action is not None else ""
        login_html = (f'<form id="authForm"{action} method="{form_method}">'
                      '<input id="user" name="login" value="" type="text">'
                      '<input id="pass" name="password" value="" type="password">'
                      f'{extra_fields}<input id="submitButton" type="submit" value="Войти"></form>')
        if ajax_present:
            login_html += ("<script>function formSubmit(){var url='/users/login.asp';"
                           "var data={user:$('#user').val(),pass:$('#pass').val(),"
                           f"path:'{ajax_path}'" + "};$.ajax({type:'post',url:url,data:data,dataType:'json'});return false;}"
                           "$(\"#authForm\").bind('submit', formSubmit);</script>")
        if malformed:
            login_html = '<html>Unknown catalog page</html>'

        async def handle(request):
            requests.append(request)
            if slow:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            if request.url.path == "/users/login.asp":
                fields = parse_qs(request.content.decode(), keep_blank_values=True)
                if fields["user"] == ["admin"]:
                    return httpx.Response(200, headers={"Set-Cookie": "admin_session=admin-only; Path=/"})
                assert fields == {"user": ["employee"], "pass": [password], "path": ["/" if legacy else "/docs"]}
                assert "admin_session" not in request.headers.get("cookie", "")
                headers = [("Location", "/" if legacy else "/docs/"), ("Set-Cookie", "Kodeks=visitor-only; Path=/")]
                if session:
                    headers.append(("Set-Cookie", "Auth=user-only; Path=/"))
                return httpx.Response(302, headers=headers)
            if request.url.path == "/users/usr":
                assert request.url.params["id"] == "employee"
                assert request.headers.get("cookie") == "admin_session=admin-only"
                return httpx.Response(200, text=(
                    '<form action="../users/users" method="post">'
                    f'<input name="uid" value="{html.escape(uid, quote=True)}">'
                    f'<input name="psw" value="{html.escape(password, quote=True)}">'
                    '<input name="set" value=""></form>'))
            if request.url.path == "/docs/":
                assert not legacy
                assert "admin_session" not in request.headers.get("cookie", "")
                assert request.method == "GET"  # Native POST /docs/ is not the AJAX contract.
                if "Auth=user-only" not in request.headers.get("cookie", "") or login_form:
                    return httpx.Response(form_status, text=login_html,
                                          headers={"Set-Cookie": "Kodeks=visitor-only; Path=/"})
            else:
                assert request.url.path == "/"
            return httpx.Response(200, text="Catalog home")

        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(handle), **kwargs))
        logs = []
        monkeypatch.setattr(auth_module.logger, "debug", lambda message, *args:
                            logs.append(message % args if args else message))
        return service, requests, logs, cancelled
    return create


@pytest.mark.asyncio
async def test_sso_uses_confirmed_user_and_an_isolated_session(setup_login):
    service, requests, logs, _ = setup_login(password='user&"<secret>')
    assert await service.login_for_sso("465304", "employee") == {"Kodeks": "visitor-only", "Auth": "user-only"}
    assert len(requests) == 6
    assert all(secret not in "\n".join(logs) for secret in
               ['user&"<secret>', 'user&amp;', 'admin-secret', 'user-only', 'admin-only'])


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", ["", "someone-else"])
async def test_sso_never_uses_another_or_blank_user_form(setup_login, uid):
    service, requests, _, _ = setup_login(uid=uid)
    with pytest.raises(AuthError) as error:
        await service.login_for_sso("465304", "employee")
    assert error.value.code == "SSO_USER_NOT_FOUND"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_sso_empty_password_does_not_fall_back_to_admin(setup_login):
    service, requests, _, _ = setup_login(password="")
    with pytest.raises(AuthError) as error:
        await service.login_for_sso("465304", "employee")
    assert error.value.code == "SSO_USER_PASSWORD_EMPTY"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_sso_rejects_200_login_form(setup_login):
    service, _, _, _ = setup_login(login_form=True)
    with pytest.raises(AuthError) as error:
        await service.login_for_sso("465304", "employee")
    assert error.value.code == "SSO_USER_AUTH_FAILED"


@pytest.mark.asyncio
async def test_sso_rejects_visitor_only_cookies_without_auth(setup_login):
    service, _, _, _ = setup_login(session=False)
    with pytest.raises(AuthError) as error:
        await service.login_for_sso("465304", "employee")
    assert error.value.code == "SSO_USER_AUTH_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"form_action": "https://other.test/docs/"},
    {"form_method": "get"},
    {"malformed": True},
    {"ajax_path": "/docs/"},
    {"ajax_present": False},
    {"extra_fields": '<input name="csrf" type="hidden" value="unverified">'},
    {"extra_fields": '<input name="cmd" type="submit" value="unknown">'},
])
async def test_sso_rejects_unverified_login_forms_before_post(setup_login, options):
    service, requests, _, _ = setup_login(**options)
    with pytest.raises(ParseError) as error:
        await service.login_for_sso("465304", "employee")
    assert error.value.code == "SSO_LOGIN_FORM_INVALID"
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_sso_accepts_a_403_authentication_challenge_with_the_actual_form(setup_login):
    service, _, _, _ = setup_login(form_status=403)
    assert (await service.login_for_sso("465304", "employee"))["Auth"] == "user-only"


@pytest.mark.asyncio
async def test_sso_hard_timeout_cancels_the_http_operation(setup_login):
    service, _, _, cancelled = setup_login(slow=True)
    with pytest.raises(NetworkError) as error:
        await service.login_for_sso("465304", "employee")
    assert error.value.code == "TIMEOUT_ERROR"
    assert cancelled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "  "])
async def test_sso_missing_user_never_enters_the_admin_flow(setup_login, name):
    service, requests, _, _ = setup_login()
    with pytest.raises(AuthError) as error:
        await service.login_for_sso("465304", name)
    assert error.value.code == "SSO_USER_REQUIRED"
    assert requests == []


@pytest.mark.asyncio
async def test_sso_requires_https_before_sending_credentials(setup_login):
    service, requests, _, _ = setup_login(base="http://catalog.test")
    with pytest.raises(NetworkError):
        await service.login_for_sso("465304", "employee")
    assert requests == []


@pytest.mark.asyncio
async def test_legacy_password_fallback_is_unchanged_and_not_logged(setup_login):
    service, _, logs, _ = setup_login(password="")
    assert await service._fetch_user_password_as_admin("https://catalog.test", "employee") == "admin-secret"
    assert "admin-secret" not in "\n".join(logs)


@pytest.mark.asyncio
async def test_legacy_user_login_signature_remains_compatible(setup_login):
    service, _, _, _ = setup_login(legacy=True)
    assert await service._user_login("https://catalog.test", "employee", "user&secret") == {"Kodeks": "visitor-only", "Auth": "user-only"}


def session_auth(value="employee:catalog-secret"):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


@pytest.fixture
def setup_session(monkeypatch):
    def create(*, status=200, login_form=False, slow=False, redirect=False, clear_auth=False):
        settings = SimpleNamespace(AUTH_HARD_TIMEOUT=0.02 if slow else 5)
        service = AuthService(db=None, settings=settings)
        resolver = AsyncMock(return_value="https://tenant-465304.test")
        monkeypatch.setattr(AuthService, "_get_base_url", resolver)
        requests = []

        async def handle(request):
            requests.append(request)
            assert request.method == "GET"
            assert request.url == "https://tenant-465304.test/docs/"
            if slow:
                await asyncio.Event().wait()
            if redirect:
                return httpx.Response(302, headers={"Location": "https://another-tenant.test/docs/"})
            headers = [("Set-Cookie", "Kodeks=fresh-settings; Path=/"),
                       ("Set-Cookie", "KodeksData=fresh-data; Path=/")]
            if clear_auth:
                headers.append(("Set-Cookie", "Auth=; Path=/"))
            body = ('<form id="authForm" method="post"><input name="login">'
                    '<input name="password" type="password"></form>') if login_form else '<title>Главная страница</title>'
            return httpx.Response(status, headers=headers, text=body)

        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(handle), **kwargs))
        return service, requests, resolver
    return create


@pytest.mark.asyncio
async def test_existing_session_is_checked_without_login_and_preserves_the_input(setup_session):
    service, requests, resolver = setup_session()
    cookies = {"Auth": session_auth(), "Kodeks": "old", "KodeksData": "old-data"}
    original = dict(cookies)
    assert await service.verify_sso_session("465304", "employee", cookies) == {
        "Auth": original["Auth"], "Kodeks": "fresh-settings", "KodeksData": "fresh-data"}
    assert cookies == original
    assert len(requests) == 1
    assert f"Auth={original['Auth']}" in requests[0].headers["cookie"]
    resolver.assert_awaited_once_with("465304")


@pytest.mark.asyncio
@pytest.mark.parametrize("cookies", [
    {}, {"Kodeks": "visitor"}, {"Auth": ""}, {"Auth": "not-base64!"},
    {"Auth": base64.b64encode(b"\xff:secret").decode()},
    {"Auth": session_auth("employee:")}, {"Auth": session_auth(":secret")},
    {"Auth": session_auth("employee")}, {"Auth": session_auth(), "extra": "unknown"},
    {"Auth": session_auth(), "Kodeks": "bad\r\nvalue"},
])
async def test_invalid_session_is_rejected_before_network(setup_session, cookies):
    service, requests, _ = setup_session()
    with pytest.raises(AuthError) as error:
        await service.verify_sso_session("465304", "employee", cookies)
    assert error.value.code == "SSO_SESSION_INVALID"
    assert requests == []
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_session_identity_cannot_be_substituted(setup_session):
    service, requests, _ = setup_session()
    with pytest.raises(AuthError) as error:
        await service.verify_sso_session("465304", "different-user", {"Auth": session_auth()})
    assert error.value.code == "SSO_USER_IDENTITY_MISMATCH"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"status": 403}, {"login_form": True}, {"clear_auth": True}, {"redirect": True}])
async def test_session_requires_protected_access_on_the_authoritative_tenant(setup_session, options):
    service, requests, _ = setup_session(**options)
    with pytest.raises(AuthError):
        await service.verify_sso_session("465304", "employee", {"Auth": session_auth()})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_session_verification_has_a_hard_timeout(setup_session):
    service, requests, _ = setup_session(slow=True)
    with pytest.raises(NetworkError) as error:
        await service.verify_sso_session("465304", "employee", {"Auth": session_auth()})
    assert error.value.code == "TIMEOUT_ERROR"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_session_password_can_contain_a_colon(setup_session):
    service, _, _ = setup_session()
    cookie = session_auth("employee:secret:with:colon")
    assert (await service.verify_sso_session("465304", "employee", {"Auth": cookie}))["Auth"] == cookie
