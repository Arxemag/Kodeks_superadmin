"""Выдача одноразовой ссылки backend портала и вход браузера через nginx каталога."""
from __future__ import annotations

import hmac
import re
import time
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import Settings, get_settings
from common.db import get_db
from common.exceptions import ServiceError
from services.auth_service.metrics import sso_latency_ms, sso_requests_total, sso_tickets_issued_total
from services.auth_service.service import AuthService
from services.auth_service.sso_store import MemoryTicketStore, TicketStoreFull

router = APIRouter(prefix="/api/expert")
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reg: str = Field(min_length=1, max_length=100)
    uid: str = Field(min_length=1, max_length=255)
    path: str = "/"
    cookies: dict[str, str] | None = Field(default=None, repr=False)


def valid_prepared_cookies(cookies: dict[str, str]) -> bool:
    """Принимает только известные поля уже подготовленной сервером сессии."""
    return (
        bool(cookies.get("Auth", "").strip())
        and set(cookies) <= {"Auth", "Kodeks", "KodeksData"}
        and all(len(value.encode("utf-8")) <= 4096
                and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
                for value in cookies.values())
    )


def validate_local_path(path: str) -> str:
    """Путь остаётся локальным и после URL-декодирования браузером/прокси."""
    if len(path) > 2048 or re.search(r"%(?![0-9a-fA-F]{2})", path) or "%25" in path.lower():
        raise ValueError("Invalid local path")
    for candidate in (path, unquote(path)):
        if (not candidate.startswith("/") or candidate.startswith("//")
                or "\\" in candidate or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in candidate)):
            raise ValueError("Invalid local path")
    return path


def canonical_base(base: str) -> tuple[str, str]:
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Invalid SSO catalog address")
    return base.rstrip("/"), parsed.hostname.lower()


def enabled_settings(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.SSO_ENABLED:
        raise HTTPException(404, detail="Not Found", headers=_HEADERS)
    return settings


def get_ticket_store(request: Request, settings: Settings = Depends(enabled_settings)) -> MemoryTicketStore:
    # Create once in create_app (not once per request or event loop).
    return request.app.state.sso_store


def browser_error(code: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<title>Вход в кабинет</title><p>Ссылка для входа недействительна '
        'или вход временно недоступен. Вернитесь на портал и попробуйте снова.</p></html>',
        status_code=status, headers={**_HEADERS, "X-SSO-Error": code},
    )


@router.post("/sso-ticket")
async def issue_ticket(payload: TicketRequest, request: Request,
                       api_token: str | None = Header(None, alias="X-Api-Token"),
                       settings: Settings = Depends(enabled_settings),
                       store: MemoryTicketStore = Depends(get_ticket_store),
                       db: AsyncSession = Depends(get_db)):
    if not api_token or not hmac.compare_digest(api_token.encode(), settings.SSO_API_TOKEN.encode()):
        return JSONResponse({"code": "SSO_TOKEN_INVALID", "message": "Invalid SSO API token"}, 401, headers=_HEADERS)
    try:
        path = validate_local_path(payload.path)
        if not payload.uid.strip() or any(ord(c) < 32 or ord(c) == 127 for c in payload.uid):
            raise ValueError("Invalid login")
    except ValueError:
        return JSONResponse({"code": "SSO_PATH_INVALID", "message": "Invalid SSO path or login"}, 400, headers=_HEADERS)
    if payload.cookies is not None and not valid_prepared_cookies(payload.cookies):
        return JSONResponse({"code": "SSO_COOKIES_INVALID", "message": "Invalid prepared session"}, 400, headers=_HEADERS)
    try:
        auth = AuthService(db, settings.model_copy(update={"AUTH_HARD_TIMEOUT": settings.SSO_LOGIN_TIMEOUT}))
        base, _ = canonical_base(await auth._get_base_url(payload.reg))
        prepared = None
        if payload.cookies is not None:
            prepared = await auth.verify_sso_session(reg=payload.reg, name=payload.uid, cookies=payload.cookies)
            if not prepared or not prepared.get("Auth", "").strip():
                return JSONResponse({"code": "SSO_SESSION_INVALID", "message": "Prepared session is invalid"}, 401, headers=_HEADERS)
        token = store.issue(reg=payload.reg, uid=payload.uid, path=path, base_url=base, cookies=prepared)
    except TicketStoreFull:
        return JSONResponse({"code": "SSO_CAPACITY", "message": "Try again later"}, 503, headers=_HEADERS)
    except ValueError:
        return JSONResponse({"code": "SSO_CATALOG_INVALID", "message": "Catalog address is invalid"}, 503, headers=_HEADERS)
    except ServiceError as error:
        return JSONResponse({"code": error.code, "message": "Catalog is unavailable"}, error.http_status, headers=_HEADERS)
    sso_tickets_issued_total.inc()
    return JSONResponse({"ticket": token, "url": base + "/sso?" + urlencode({"ticket": token}),
                         "expires_in": store.ttl}, headers=_HEADERS)


@router.get("/sso")
async def consume_ticket(request: Request, settings: Settings = Depends(enabled_settings),
                         store: MemoryTicketStore = Depends(get_ticket_store),
                         db: AsyncSession = Depends(get_db)):
    started = time.perf_counter()
    result = "error"
    try:
        query = request.query_params
        values = query.getlist("ticket")
        if set(query) != {"ticket"} or len(values) != 1 or not 1 <= len(values[0]) <= 256:
            result = "invalid_ticket"
            return browser_error("SSO_TICKET_INVALID", 401)
        ticket = store.pop(values[0])
        if ticket is None:
            result = "invalid_ticket"
            return browser_error("SSO_TICKET_INVALID", 401)
        base, host = canonical_base(ticket.base_url)
        fresh_base, fresh_host = canonical_base(await AuthService(db, settings)._get_base_url(ticket.reg))
        if request.url.hostname != host or fresh_host != host or fresh_base != base:
            result = "host_mismatch"
            return browser_error("SSO_HOST_MISMATCH", 400)
        cookies = ticket.cookies
        if cookies is None:
            auth = AuthService(db, settings.model_copy(update={"AUTH_HARD_TIMEOUT": settings.SSO_LOGIN_TIMEOUT}))
            cookies = await auth.login_for_sso(reg=ticket.reg, name=ticket.uid)
        if not cookies:
            result = "auth_failed"
            return browser_error("SSO_AUTH_FAILED", 401)
        response = RedirectResponse(ticket.path, status_code=302, headers=_HEADERS)
        for name, value in cookies.items():
            response.set_cookie(name, value, path="/", secure=True, httponly=settings.SSO_COOKIE_HTTPONLY,
                                samesite="lax")
        result = "ok"
        return response
    except ServiceError as error:
        result = "auth_failed"
        return browser_error("SSO_AUTH_FAILED", 401 if error.http_status < 500 else 503)
    except Exception:
        return browser_error("SSO_UNAVAILABLE", 503)
    finally:
        sso_requests_total.labels(result=result).inc()
        sso_latency_ms.observe((time.perf_counter() - started) * 1000)
