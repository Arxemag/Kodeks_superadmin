"""Fail-closed authorization for native cabinet GraphQL writes.

The caller holds catalog_operation for the authorization and native write,
except for a locally authenticated service administrator. The caller must parse
the GraphQL operation, reject unsupported/multiple mutations and pass the actual
target identifiers; this module does not parse GraphQL syntax.
"""
from __future__ import annotations

import base64
import binascii
import hmac

from common.editor_portal import read_editor_snapshot
from common.exceptions import AuthError
from services.auth_service.service import AuthService
from services.infoboards_service.cabinet_edit import fetch_board
from services.infoboards_service.editor_scope import resolve_editor_scopes
from services.infoboards_service.editor_service import (
    EditorGroupRepository, _ADMIN_GROUPS, _group_title, _root_assignments,
)
from services.users_service.catalog_client import CatalogClient
from services.users_service.html_parser import group_index, normalize_title, parse_groups_catalog, parse_user_state


def _denied() -> AuthError:
    return AuthError(code="EDITOR_WRITE_FORBIDDEN", message="Cabinet write is not authorized", http_status=403)


def _credentials(cookie: str | None) -> tuple[str, str] | None:
    if not isinstance(cookie, str) or not cookie or len(cookie) > 8192:
        return None
    try:
        raw = base64.b64decode(cookie.encode('ascii'), validate=True)
        # Require one canonical base64 spelling, as emitted by the catalog.
        if base64.b64encode(raw).decode('ascii') != cookie:
            return None
        value = raw.decode('utf-8')
        uid, separator, password = value.partition(':')
        if (not separator or not uid or uid != uid.strip() or not password
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
            return None
        return uid, password
    except (UnicodeError, ValueError, binascii.Error):
        return None


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def is_service_admin(auth_cookie: str | None, settings) -> bool:
    credentials = _credentials(auth_cookie)
    if credentials is None:
        return False
    uid, password = credentials
    name = settings.ADMIN_LOGIN
    secret = settings.ADMIN_PASSWORD
    if not isinstance(name, str) or not name or not isinstance(secret, str) or not secret:
        return False
    name_matches = _equal(uid, name)
    password_matches = _equal(password, secret)
    return name_matches and password_matches


async def authorize_graphql_write(reg, base_url, action, board_id, board_uuid, auth_cookie,
                                  settings, http_client, db) -> None:
    if is_service_admin(auth_cookie, settings):
        return
    credentials = _credentials(auth_cookie)
    if credentials is None or action not in {'editConfig', 'deleteConfig', 'createConfig'}:
        raise _denied()
    uid, password = credentials
    catalog = CatalogClient(http_client)
    cookies = await AuthService(db, settings).login(reg=reg, name=None)
    page = await catalog.get_user_page(base_url, uid, cookies)
    if page is None:
        raise _denied()
    user = parse_user_state(page)
    stored_password = user.fields.get('psw')
    if (user.is_empty_form or user.fields.get('uid') != uid or not isinstance(stored_password, str)
            or not stored_password or not _equal(password, stored_password)):
        raise _denied()
    native_groups = group_index(parse_groups_catalog(await catalog.get_groups_page(base_url, cookies)))
    admin_ids = {native_groups[normalize_title(title)] for title in _ADMIN_GROUPS
                 if normalize_title(title) in native_groups}
    if set(user.groups) & admin_ids:
        return
    if (action != 'editConfig' or not isinstance(board_id, str) or not board_id
            or board_id != board_id.strip()):
        raise _denied()
    snapshot = await read_editor_snapshot(settings.EDITOR_PORTAL_DB_URL.get_secret_value(), reg)
    assignments = _root_assignments(snapshot, base_url)
    roots = {root for root, users in assignments.items() if uid in users}
    if not roots:
        raise _denied()
    scopes = await resolve_editor_scopes(base_url, http_client, cookies, roots, set(assignments))
    bindings = await EditorGroupRepository().get(base_url, reg)
    permitted = False
    for root, scope in scopes.items():
        binding = bindings.get(scope.branch_id)
        if (root not in roots or board_id not in scope.editable_ids or binding is None
                or binding.root_id != root or board_id not in binding.boards
                or uid not in binding.tracked_uids or type(binding.group_id) is not int
                or binding.group_id not in user.groups
                or native_groups.get(normalize_title(_group_title(reg, scope.branch_id))) != binding.group_id):
            continue
        permitted = True
        break
    if not permitted:
        raise _denied()
    if board_uuid is not None:
        if not isinstance(board_uuid, str) or not board_uuid:
            raise _denied()
        board = await fetch_board(base_url, http_client, cookies, board_id)
        if board.id != board_id or board.settings.get('boardUuid') != board_uuid:
            raise _denied()
