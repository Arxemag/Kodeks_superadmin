"""SSO: одноразовая ссылка, привязка домена и выдача браузерной сессии."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from common.config import Settings, get_settings
from common.db import get_db
from common.exceptions import AuthError
from services.auth_service.service import AuthService
from services.auth_service.sso import router, get_ticket_store, validate_local_path
from services.auth_service.sso_store import MemoryTicketStore, TicketStoreFull

VALID_AUTH = "dGVzdC1sb2dpbjpwYXNzd29yZA=="
PADDED_AUTH_WITH_SYMBOLS = "dGVzdHVzZXI64KC+4KC/YQ=="


def settings(**changes):
    return Settings(_env_file=None, DB_URL="postgresql+asyncpg://stub/db", ADMIN_LOGIN="admin",
                    ADMIN_PASSWORD="unused", SSO_ENABLED=True, SSO_API_TOKEN="backend-token", **changes)


@pytest.fixture
def setup_sso(monkeypatch):
    config = settings()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_db] = lambda: object()
    store = MemoryTicketStore()
    app.dependency_overrides[get_ticket_store] = lambda: store
    resolver = AsyncMock(return_value="https://cabinet-05.kodeks.expert")
    login = AsyncMock(return_value={"Auth": VALID_AUTH, "Other": "other-value"})
    monkeypatch.setattr(AuthService, "_get_base_url", resolver)
    monkeypatch.setattr(AuthService, "login_for_sso", login, raising=False)
    monkeypatch.setattr(AuthService, "verify_sso_session", AsyncMock(return_value={
        "Auth": VALID_AUTH, "Kodeks": "verified-session", "KodeksData": "verified-data",
    }), raising=False)
    # A fallback to the old admin-capable flow must never occur.
    old_login = AsyncMock(side_effect=AssertionError("Old auth flow called"))
    monkeypatch.setattr(AuthService, "login", old_login)
    return app, config, store, resolver, login, old_login


def issue(client, **changes):
    return client.post("/api/expert/sso-ticket", headers={"X-Api-Token": "backend-token"},
                       json={"reg": "465304", "uid": "test-login", "path": "/docs/?infoboard=P000A", **changes})


def test_store_expiry_capacity_and_reclamation():
    now = [10.0]
    store = MemoryTicketStore(ttl=5, capacity=1, clock=lambda: now[0])
    first = store.issue(reg="r", uid="u", path="/", base_url="https://catalog")
    with pytest.raises(TicketStoreFull):
        store.issue(reg="r", uid="v", path="/", base_url="https://catalog")
    now[0] = 15.0
    assert store.pop(first) is None
    second = store.issue(reg="r", uid="v", path="/", base_url="https://catalog")
    assert store.pop(second).uid == "v"
    assert store.pop(second) is None


def test_store_pop_atomic_across_threads():
    store = MemoryTicketStore()
    token = store.issue(reg="r", uid="u", path="/", base_url="https://catalog")
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(store.pop, [token] * 40))
    assert sum(result is not None for result in results) == 1


def test_store_takes_private_immutable_cookie_snapshot():
    store = MemoryTicketStore()
    cookies = {"Auth": "private-original-session"}
    token = store.issue(reg="r", uid="u", path="/", base_url="https://catalog", cookies=cookies)
    cookies["Auth"] = "mutated-by-caller"
    ticket = store.pop(token)
    assert ticket.cookies == {"Auth": "private-original-session"}
    assert "private-original-session" not in repr(ticket)
    output = ticket.cookies
    output["Auth"] = "mutated-return-value"
    assert ticket.cookies == {"Auth": "private-original-session"}
    with pytest.raises(TypeError):
        ticket._cookies["Auth"] = "mutated-internal-value"


def test_login_sets_host_only_cookies_and_consumes_ticket(setup_sso):
    app, config, store, resolver, login, old_login = setup_sso
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        response = issue(client)
        assert response.status_code == 200
        body = response.json()
        assert body["expires_in"] == 60
        assert body["url"].startswith("https://cabinet-05.kodeks.expert/sso?ticket=")
        response = client.get("/api/expert/sso", params={"ticket": body["ticket"]}, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/docs/?infoboard=P000A"
        cookies = response.headers.get_list("set-cookie")
        assert len(cookies) == 2
        assert all("Secure" in c and "HttpOnly" in c and "samesite=lax" in c.lower() and "Path=/" in c and "Domain=" not in c for c in cookies)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        replay = client.get("/api/expert/sso", params={"ticket": body["ticket"]})
        assert replay.status_code == 401 and "text/html" in replay.headers["content-type"]
    login.assert_awaited_once_with(reg="465304", name="test-login")
    old_login.assert_not_awaited()


@pytest.mark.parametrize("path", ["https://evil.test", "//evil.test", "/\\evil", "/%2fevil", "/%252fevil", "/%5cevil", "/%00x", "/%0dX", "/\x7fx", "/%C2%85x", "javascript:alert(1)", " ", "/%oops", "/" + "x" * 2048])
def test_path_rejects_redirects_and_encoding_ambiguity(path, setup_sso):
    app, *_ = setup_sso
    with TestClient(app) as client:
        response = issue(client, path=path)
    assert response.status_code == 400 and response.json()["code"] == "SSO_PATH_INVALID"


def test_path_preserves_valid_query():
    path = "/docs/?nd=606025000&search=%D0%A2%D0%B5%D1%81%D1%82#section"
    assert validate_local_path(path) == path


@pytest.mark.parametrize("token", [None, "wrong"])
def test_issue_requires_backend_token(token, setup_sso):
    app, _, _, resolver, *_ = setup_sso
    with TestClient(app) as client:
        response = client.post("/api/expert/sso-ticket", json={"reg": "r", "uid": "u"},
                               headers={} if token is None else {"X-Api-Token": token})
    assert response.status_code == 401
    resolver.assert_not_awaited()


def test_missing_reg_does_not_issue_ticket(setup_sso):
    app, _, _, resolver, *_ = setup_sso
    resolver.side_effect = AuthError(code="REG_NOT_FOUND", http_status=404)
    with TestClient(app) as client:
        response = issue(client)
    assert response.status_code == 404 and response.json()["code"] == "REG_NOT_FOUND"


def test_host_mismatch_burns_ticket_before_login(setup_sso):
    app, _, _, _, login, _ = setup_sso
    with TestClient(app, base_url="https://wrong.test") as client:
        token = issue(client).json()["ticket"]
        response = client.get("/api/expert/sso", params={"ticket": token})
        assert response.status_code == 400
        assert response.headers["x-sso-error"] == "SSO_HOST_MISMATCH"
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        assert client.get("/api/expert/sso", params={"ticket": token}).status_code == 401
    login.assert_not_awaited()


def test_changed_registration_cannot_use_old_ticket(setup_sso):
    app, _, _, resolver, login, _ = setup_sso
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        token = issue(client).json()["ticket"]
        resolver.return_value = "https://cabinet-06.kodeks.expert"
        assert client.get("/api/expert/sso", params={"ticket": token}).status_code == 400
    login.assert_not_awaited()


@pytest.mark.parametrize("query", ["", "ticket=bad&path=/evil", "ticket=one&ticket=two", "ticket=bad&reg=r"])
def test_browser_rejects_extra_or_missing_params_as_html(query, setup_sso):
    app, *_ = setup_sso
    with TestClient(app) as client:
        response = client.get("/api/expert/sso?" + query)
    assert response.status_code == 401 and "text/html" in response.headers["content-type"]


def test_auth_failure_is_html_without_secret_or_fallback(setup_sso):
    app, _, _, _, login, old_login = setup_sso
    login.side_effect = AuthError(message="private-password-cookie")
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        token = issue(client).json()["ticket"]
        response = client.get("/api/expert/sso", params={"ticket": token})
        assert response.status_code == 401
        assert "private-password-cookie" not in response.text
        assert not response.headers.get_list("set-cookie")
        assert client.get("/api/expert/sso", params={"ticket": token}).status_code == 401
    old_login.assert_not_awaited()


def test_prepared_session_is_verified_before_ticket_and_consumed_without_login(setup_sso):
    app, _, _, _, login, old_login = setup_sso
    verifier = AuthService.verify_sso_session
    prepared = {"Auth": "private-backend-session"}
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        issued = issue(client, cookies=prepared)
        assert issued.status_code == 200
        assert set(issued.json()) == {"ticket", "url", "expires_in"}
        assert all(value not in issued.text for value in [*prepared.values(), VALID_AUTH, "verified-session", "verified-data"])
        verifier.assert_awaited_once_with(reg="465304", name="test-login", cookies=prepared)
        # Changes to the verifier's source dictionary cannot alter the ticket.
        verifier.return_value["Auth"] = "late-source-mutation"
        token = issued.json()["ticket"]
        response = client.get("/api/expert/sso", params={"ticket": token}, follow_redirects=False)
        assert response.status_code == 302
        assert "Auth=" + VALID_AUTH + ";" in response.headers["set-cookie"]
        assert "late-source-mutation" not in response.headers["set-cookie"]
        assert "Kodeks=verified-session;" in response.headers["set-cookie"]
        assert "KodeksData=verified-data;" in response.headers["set-cookie"]
        assert client.get("/api/expert/sso", params={"ticket": token}).status_code == 401
    login.assert_not_awaited()
    old_login.assert_not_awaited()
    assert verifier.await_count == 1


def test_prepared_profile_mismatch_aborts_before_ticket_without_login(setup_sso):
    app, _, store, _, login, old_login = setup_sso
    verifier = AuthService.verify_sso_session
    verifier.side_effect = AuthError(code="SSO_IDENTITY_MISMATCH", message="private-session-value")
    with TestClient(app) as client:
        response = issue(client, cookies={"Auth": "backend-session"})
    assert response.status_code == 401
    assert response.json()["code"] == "SSO_IDENTITY_MISMATCH"
    assert "private-session-value" not in response.text
    assert not store._tickets
    login.assert_not_awaited()
    old_login.assert_not_awaited()


@pytest.mark.parametrize("prepared", [{}, {"Auth": ""}, {"Auth": "  "}, {"Auth": "a", "Other": "private-other"}, {"Auth": "a" * 4097}, {"Auth": "a", "Kodeks": "x" * 4097}, {"Auth": "a", "KodeksData": "x\r\nheader"}])
def test_prepared_cookie_contract_rejected_without_verification_or_echo(prepared, setup_sso):
    app, _, store, _, login, _ = setup_sso
    with TestClient(app) as client:
        response = issue(client, cookies=prepared)
    assert response.status_code == 400
    assert response.json()["code"] == "SSO_COOKIES_INVALID"
    assert "private-other" not in response.text
    assert not store._tickets
    AuthService.verify_sso_session.assert_not_awaited()
    login.assert_not_awaited()


def test_prepared_session_requires_backend_token(setup_sso):
    app, _, store, *_ = setup_sso
    with TestClient(app) as client:
        response = client.post("/api/expert/sso-ticket", json={"reg": "465304", "uid": "u", "cookies": {"Auth": "private-backend-session"}})
    assert response.status_code == 401
    assert "private-backend-session" not in response.text
    assert not store._tickets
    AuthService.verify_sso_session.assert_not_awaited()


def test_invalid_cookie_shape_not_exposed_by_application_validation(setup_sso, monkeypatch):
    from services.auth_service import app as app_module
    config = setup_sso[1]
    monkeypatch.setattr(app_module, "get_settings", lambda: config)
    app = app_module.create_app()
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = issue(client, cookies={"Auth": {"nested": "private-cookie-value"}})
    assert response.status_code == 422
    assert "private-cookie-value" not in response.text
    assert response.headers["cache-control"] == "no-store"
    AuthService.verify_sso_session.assert_not_awaited()


def test_empty_verified_result_does_not_fall_back_to_login(setup_sso):
    app, _, store, _, login, old_login = setup_sso
    AuthService.verify_sso_session.return_value = {}
    with TestClient(app) as client:
        response = issue(client, cookies={"Auth": "input-session"})
    assert response.status_code == 401
    assert not store._tickets
    login.assert_not_awaited()
    old_login.assert_not_awaited()


@pytest.mark.parametrize("http_only", [True, False])
def test_padded_base64_auth_is_emitted_exactly_without_quotes(setup_sso, http_only):
    app, config, _, _, login, _ = setup_sso
    config.SSO_COOKIE_HTTPONLY = http_only
    login.return_value = {"Auth": PADDED_AUTH_WITH_SYMBOLS, "Kodeks": "native-session"}
    assert all(char in PADDED_AUTH_WITH_SYMBOLS for char in ("+", "/", "="))
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        token = issue(client).json()["ticket"]
        response = client.get("/api/expert/sso", params={"ticket": token}, follow_redirects=False)
        assert response.status_code == 302
        auth_headers = [h for h in response.headers.get_list("set-cookie") if h.startswith("Auth=")]
        assert len(auth_headers) == 1
        raw_value, *attributes = auth_headers[0].removeprefix("Auth=").split("; ")
        assert raw_value == PADDED_AUTH_WITH_SYMBOLS
        assert '"' not in auth_headers[0]
        assert "Path=/" in attributes and "Secure" in attributes and "SameSite=Lax" in attributes
        assert ("HttpOnly" in attributes) is http_only
        assert all(not item.startswith(("Domain=", "Max-Age=", "Expires=")) for item in attributes)
        assert client.cookies.get("Auth") == PADDED_AUTH_WITH_SYMBOLS
        profile_request = client.get("/user/profile")
        assert "Auth=" + PADDED_AUTH_WITH_SYMBOLS in profile_request.request.headers["cookie"]
        assert "Kodeks=native-session" in profile_request.request.headers["cookie"]


@pytest.mark.parametrize("value", ["\"dGVzdDpwYXNz\"", "dGVzdDpwYXNz\r\nInjected: yes", "dGVzdDpwYXNz ", "not-base64", "кодекс", "", "===="])
def test_invalid_catalog_auth_fails_without_cookie_or_echo(value, setup_sso):
    app, _, _, _, login, _ = setup_sso
    login.return_value = {"Kodeks": "native-session", "Auth": value}
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        token = issue(client).json()["ticket"]
        response = client.get("/api/expert/sso", params={"ticket": token}, follow_redirects=False)
        assert response.status_code in {401, 503}
        assert not response.headers.get_list("set-cookie")
        assert "Injected" not in response.text
        assert "dGVzdDpwYXNz" not in response.text
        assert client.get("/api/expert/sso", params={"ticket": token}).status_code == 401


def test_invalid_verified_auth_is_not_stored_or_returned(setup_sso):
    app, _, store, _, login, _ = setup_sso
    AuthService.verify_sso_session.return_value = {"Auth": '\"dGVzdDpwYXNz\"'}
    with TestClient(app) as client:
        response = issue(client, cookies={"Auth": VALID_AUTH})
    assert response.status_code in {401, 503}
    assert "dGVzdDpwYXNz" not in response.text
    assert not store._tickets
    login.assert_not_awaited()


def test_disabled_endpoints_return_404(setup_sso):
    app, config, *_ = setup_sso
    config.SSO_ENABLED = False
    with TestClient(app) as client:
        assert issue(client).status_code == 404
        assert client.get("/api/expert/sso?ticket=x").status_code == 404


def test_enabled_config_requires_nonempty_token():
    with pytest.raises(ValidationError, match="SSO_API_TOKEN"):
        Settings(_env_file=None, DB_URL="postgresql+asyncpg://stub/db", ADMIN_LOGIN="a", ADMIN_PASSWORD="p", SSO_ENABLED=True)


def test_expired_browser_ticket_does_not_authenticate(setup_sso):
    app, _, _, _, login, _ = setup_sso
    now = [0.0]
    store = MemoryTicketStore(ttl=10, clock=lambda: now[0])
    app.dependency_overrides[get_ticket_store] = lambda: store
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        token = issue(client).json()["ticket"]
        now[0] = 10
        assert client.get("/api/expert/sso", params={"ticket": token}).status_code == 401
    login.assert_not_awaited()


def test_httponly_can_be_disabled_after_catalog_research(setup_sso):
    app, config, *_ = setup_sso
    config.SSO_COOKIE_HTTPONLY = False
    with TestClient(app, base_url="https://cabinet-05.kodeks.expert") as client:
        token = issue(client).json()["ticket"]
        response = client.get("/api/expert/sso", params={"ticket": token}, follow_redirects=False)
    assert response.status_code == 302
    assert all("HttpOnly" not in c for c in response.headers.get_list("set-cookie"))


def test_startup_uses_configured_store_and_validation_responses_not_cached(setup_sso, monkeypatch):
    from services.auth_service import app as app_module
    config = settings(SSO_TICKET_TTL=120, SSO_TICKET_CAPACITY=1)
    monkeypatch.setattr(app_module, "get_settings", lambda: config)
    app = app_module.create_app()
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        first = issue(client)
        assert first.status_code == 200 and first.json()["expires_in"] == 120
        assert issue(client).status_code == 503
        invalid = client.post("/api/expert/sso-ticket", json={})
        assert invalid.status_code == 422
        assert invalid.headers["cache-control"] == "no-store"
        assert invalid.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_parallel_browser_requests_only_one_authenticates(setup_sso):
    app, *_ = setup_sso
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cabinet-05.kodeks.expert") as client:
        response = await client.post("/api/expert/sso-ticket", headers={"X-Api-Token": "backend-token"}, json={"reg": "465304", "uid": "u"})
        token = response.json()["ticket"]
        responses = await asyncio.gather(*(client.get("/api/expert/sso", params={"ticket": token}) for _ in range(10)))
    assert [r.status_code for r in responses].count(302) == 1
    assert [r.status_code for r in responses].count(401) == 9
