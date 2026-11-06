"""Guard writes on the native GraphQL endpoint before proxying to the catalog."""
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from common.catalog_lock import catalog_operation
from common.config import Settings, get_settings
from common.db import get_db
from common.exceptions import ServiceError
from services.auth_service.editor_graphql_policy import authorize_graphql_write, is_service_admin
from services.auth_service.graphql_request import MAX_GRAPHQL_BODY, parse_graphql_request
from services.auth_service.service import AuthService


router = APIRouter()
_REQUEST_HEADERS = {"accept", "content-type", "cookie", "origin", "referer", "user-agent"}
_RESPONSE_HEADERS = {"content-type", "location", "set-cookie", "www-authenticate"}


def is_internal_auth_request(request: Request, settings: Settings) -> bool:
    networks = settings.AUTH_INTERNAL_NETWORKS
    if not networks:
        return True
    try:
        address = ip_address(request.client.host if request.client else "")
        address = getattr(address, "ipv4_mapped", None) or address
        return any(address in ip_network(network.strip()) for network in networks.split(","))
    except ValueError:
        return False


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"errors": [{"message": code, "extensions": {"code": code}}]}, status_code=status)


def _native_response(reply: httpx.Response, upstream: str) -> Response:
    response = Response(reply.content, status_code=reply.status_code)
    for name, value in reply.headers.multi_items():
        if name not in _RESPONSE_HEADERS:
            continue
        if name == "location":
            target = urlsplit(value)
            if target.netloc == urlsplit(upstream).netloc:
                value = urlunsplit(("", "", target.path, target.query, target.fragment))
        response.headers.append(name, value)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.api_route("/api/expert/catalog-graphql/{reg}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def catalog_graphql(
    reg: str, request: Request, db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
) -> Response:
    upstream = settings.EDITOR_GRAPHQL_UPSTREAM.rstrip("/")
    if not upstream or reg != settings.EDITOR_GRAPHQL_REG:
        return _error(404, "GRAPHQL_GATEWAY_UNAVAILABLE")
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                                  "Access-Control-Allow-Headers": "Content-Type"})
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_GRAPHQL_BODY:
            return _error(413, "GRAPHQL_BODY_TOO_LARGE")
        raw.extend(chunk)
    try:
        graphql = parse_graphql_request(request.method, bytes(raw), request.url.query)
        base_url = await AuthService(db=db, settings=settings)._get_base_url(reg)
        headers = {name: value for name, value in request.headers.items() if name in _REQUEST_HEADERS}
        headers["Host"] = urlsplit(base_url).netloc
        if request.method == "POST":
            headers["Content-Type"] = "application/json"
        auth_cookie = request.cookies.get("Auth")

        async with httpx.AsyncClient(timeout=settings.ACL_WRITE_TIMEOUT, follow_redirects=False) as http:
            async def forward():
                reply = await http.request(request.method, upstream + "/infoboard/graphql",
                                           params=graphql.params, content=graphql.body, headers=headers)
                return _native_response(reply, upstream)

            if graphql.action in {"query", "sendRedirectAction"} or is_service_admin(auth_cookie, settings):
                return await forward()
            async with catalog_operation(base_url):
                await authorize_graphql_write(reg, base_url, graphql.action, graphql.board_id, graphql.board_uuid,
                                              auth_cookie, settings, http, db)
                return await forward()
    except ServiceError as error:
        return _error(error.http_status, error.code)
    except httpx.RequestError:
        return _error(502, "GRAPHQL_UPSTREAM_FAILED")
