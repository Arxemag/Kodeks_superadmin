"""Project the Portal's responsible-cabinet rule into native editor groups."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlalchemy import text

from common.acl_ownership import AclOwnershipRepository
from common.catalog_lock import canonical_catalog_url, catalog_operation
from common.db import get_db_session
from common.editor_portal import EditorSnapshot, read_editor_snapshot
from common.exceptions import AuthError, NetworkError, ParseError
from common.logger import get_logger
from services.infoboards_service.editor_scope import resolve_editor_scopes
from services.infoboards_service.init_company_service import InitCompanyService
from services.users_service.html_parser import group_index, normalize_title, parse_groups_catalog, parse_user_state
from services.users_service.service import UserService, overlay_form


logger = get_logger("infoboards.editor_service")
_WEBMASTER = "Администратор ( webmaster )"
_ADMIN_GROUPS = (_WEBMASTER, "Администраторы подсистем")


@dataclass(frozen=True)
class EditorGroup:
    branch_id: str
    root_id: str
    group_id: int | None = None
    boards: tuple[str, ...] = ()
    tracked_uids: tuple[str, ...] = ()


class EditorGroupRepository:
    async def get(self, base_url, reg):
        async with get_db_session() as db:
            rows = await db.execute(text("""
                SELECT branch_id, root_id, group_id, boards, tracked_uids
                FROM catalog_cabinet_editor_groups WHERE catalog_url=:url AND reg=:reg
            """), {"url": canonical_catalog_url(base_url), "reg": reg})
            return {b: EditorGroup(b, r, g, tuple(boards), tuple(users)) for b, r, g, boards, users in rows}

    async def put(self, base_url, reg, group):
        async with get_db_session() as db, db.begin():
            result = await db.execute(text("""
                INSERT INTO catalog_cabinet_editor_groups
                  (catalog_url,branch_id,reg,root_id,group_id,boards,tracked_uids)
                VALUES (:url,:branch,:reg,:root,:gid,CAST(:boards AS text[]),CAST(:uids AS text[]))
                ON CONFLICT (catalog_url,branch_id) DO UPDATE SET
                  group_id=EXCLUDED.group_id, boards=EXCLUDED.boards, tracked_uids=EXCLUDED.tracked_uids
                WHERE catalog_cabinet_editor_groups.reg=EXCLUDED.reg
                  AND catalog_cabinet_editor_groups.root_id=EXCLUDED.root_id
                  AND (catalog_cabinet_editor_groups.group_id IS NULL OR
                       catalog_cabinet_editor_groups.group_id=EXCLUDED.group_id)
                RETURNING branch_id
            """), {"url": canonical_catalog_url(base_url), "reg": reg, "branch": group.branch_id,
                   "root": group.root_id, "gid": group.group_id, "boards": list(group.boards),
                   "uids": list(group.tracked_uids)})
            if result.scalar_one_or_none() is None:
                raise ParseError(code="EDITOR_GROUP_CONFLICT", message="An editor group is already bound to a different scope")


def _root_assignments(snapshot: EditorSnapshot, base_url: str) -> dict[str, set[str]]:
    roots: dict[str, set[str]] = {}
    catalog = canonical_catalog_url(base_url)
    for module in snapshot.modules:
        link = urlsplit(module.link)
        ids = parse_qs(link.query, keep_blank_values=True).get("infoboard", [])
        valid = (link.scheme in {"http", "https"} and link.netloc and not link.username
                 and canonical_catalog_url(urlunsplit((link.scheme, link.netloc, "/", "", ""))) == catalog
                 and link.path.rstrip("/") == "/docs" and len(ids) == 1 and bool(ids[0]))
        if not valid:
            if module.editor_uid is not None:
                raise ParseError(code="EDITOR_ROOT_INVALID", message="Portal editor module does not identify a board in this catalog")
            continue
        users = roots.setdefault(ids[0], set())
        if module.editor_uid is not None:
            users.add(module.editor_uid)
    return roots


def _group_title(reg, branch_id):
    return f"ИТРХаб Редакторы {reg} {branch_id}"


class EditorService:
    def __init__(self, settings, resolver, user_service, *, groups=None):
        self.settings = settings
        self.resolver = resolver
        self.user_service = user_service
        self.catalog = user_service.catalog_client
        self.groups = groups or EditorGroupRepository()

    async def _group_index(self, base, cookies):
        return group_index(parse_groups_catalog(await self.catalog.get_groups_page(base, cookies)))

    async def _member(self, base, cookies, uid, gid, allowed):
        page = await self.catalog.get_user_page(base, uid, cookies)
        if page is None:
            if allowed:
                logger.warning("Editor user is not provisioned yet")
            return
        current = parse_user_state(page)
        UserService._check_identity(current, uid)
        groups = set(current.groups)
        expected = groups | {gid} if allowed else groups - {gid}
        if groups == expected:
            return
        replacement = {"grp": [str(group) for group in sorted(expected)]}
        form = overlay_form(current.form_data, replacement)
        try:
            await self.catalog.post_user(base, form, cookies, form_action=current.action, form_method=current.method)
        except (NetworkError, ParseError):
            # The full user form may already have been saved. Verify by reading.
            await self.user_service._verify_user(base, uid, cookies, replacement)
            return
        await self.user_service._verify_user(base, uid, cookies, replacement)

    async def sync(self, reg: str, snapshot: EditorSnapshot | None = None):
        base = await self.resolver.resolve_base_url(reg)
        async with catalog_operation(base):
            if snapshot is None:
                snapshot = await read_editor_snapshot(self.settings.EDITOR_PORTAL_DB_URL.get_secret_value(), reg)
            assignments = _root_assignments(snapshot, base)
            tracked = await self.groups.get(base, reg)
            active = {root for root, users in assignments.items() if users}
            if not active and not tracked:
                return
            cookies = await self.user_service.auth_client.get_admin_cookies(reg)
            names = await self._group_index(base, cookies)
            admin_ids = {names[normalize_title(title)] for title in _ADMIN_GROUPS if normalize_title(title) in names}
            if normalize_title(_WEBMASTER) not in names:
                raise ParseError(code="EDITOR_ADMIN_GROUP_MISSING", message="The native webmaster group could not be identified")

            for binding in tracked.values():
                if binding.group_id is None:
                    continue
                if names.get(normalize_title(_group_title(reg, binding.branch_id))) != binding.group_id:
                    raise ParseError(code="EDITOR_GROUP_BINDING_BROKEN", message="The reserved native editor group has changed")
                for uid in set(binding.tracked_uids) - assignments.get(binding.root_id, set()):
                    await self._member(base, cookies, uid, binding.group_id, False)

            scopes = await resolve_editor_scopes(base, self.catalog.http_client, cookies, active, set(assignments))
            wanted = {scope.branch_id: root for root, scope in scopes.items()}
            if len(wanted) != len(scopes):
                raise ParseError(code="EDITOR_SCOPE_CONFLICT", message="Multiple portal cabinets refer to the same editor branch")

            old_boards = {board for binding in tracked.values() for board in binding.boards}
            for branch, binding in list(tracked.items()):
                if binding.root_id in active and wanted.get(branch) != binding.root_id and binding.group_id is not None:
                    for uid in set(binding.tracked_uids) & assignments[binding.root_id]:
                        await self._member(base, cookies, uid, binding.group_id, False)

            for branch, root in wanted.items():
                binding = tracked.get(branch)
                title = _group_title(reg, branch)
                key = normalize_title(title)
                if binding is None:
                    if key in names:
                        raise ParseError(code="EDITOR_GROUP_NAME_CONFLICT", message="A reserved editor group name already exists")
                    binding = EditorGroup(branch, root)
                    await self.groups.put(base, reg, binding)  # Creation intent precedes the native write.
                if binding.root_id != root:
                    raise ParseError(code="EDITOR_SCOPE_CONFLICT", message="The editor branch has a different recorded root")
                if binding.group_id is None:
                    if key in names:
                        raise ParseError(code="EDITOR_GROUP_CREATE_UNCERTAIN", message="A previous group creation requires reconciliation")
                    if not await self.catalog.create_group(base, title, cookies):
                        raise ParseError(code="EDITOR_GROUP_CREATE_UNCERTAIN", message="Creation of the reserved editor group was not confirmed")
                    names = await self._group_index(base, cookies)
                    if key not in names:
                        raise ParseError(code="EDITOR_GROUP_NOT_CONFIRMED", message="The editor group is absent after creation")
                    binding = replace(binding, group_id=names[key])
                    await self.groups.put(base, reg, binding)
                tracked[branch] = binding

            edit: dict[str, list[int]] = {}
            view: dict[str, list[int]] = {}
            exclusive = {board: set(admin_ids) for board in old_boards}
            planned = {}
            for branch, binding in tracked.items():
                gid = binding.group_id
                if gid is None:
                    continue
                if wanted.get(branch) == binding.root_id:
                    boards = tuple(sorted(scopes[binding.root_id].editable_ids))
                    for board in boards:
                        edit.setdefault(board, []).append(gid)
                        view.setdefault(board, []).append(gid)
                        exclusive.setdefault(board, set(admin_ids)).add(gid)
                    view.setdefault(binding.root_id, []).append(gid)
                else:
                    # When the grant is revoked the branch stays closed to ordinary users.
                    # Keep the previous scope in the registry so future syncs enforce it.
                    boards = binding.boards
                planned[branch] = replace(binding, boards=boards)

            async with get_db_session() as db:
                service = InitCompanyService(db, self.settings, self.catalog.http_client, self.catalog,
                                             AclOwnershipRepository(source="editors"))
                await service._apply_acl(base, cookies, edit, {}, reg=reg, view_acl_matrix=view,
                                         exclusive_edit=exclusive)

            for branch, binding in planned.items():
                desired = assignments[binding.root_id] if wanted.get(branch) == binding.root_id else set()
                updated = replace(binding, tracked_uids=tuple(sorted(set(binding.tracked_uids) | desired)))
                if updated != tracked[branch]:
                    await self.groups.put(base, reg, updated)  # Record membership intent before native writes.
                for uid in desired:
                    await self._member(base, cookies, uid, binding.group_id, True)
            logger.info("Editor ACL synchronized reg=%s branches=%s edit_boards=%s", reg, len(wanted), len(edit))


async def run_editor_sync(service: EditorService, regs: tuple[str, ...], stop_event):
    while not stop_event.is_set():
        for reg in regs:
            if stop_event.is_set():
                break
            try:
                await service.sync(reg)
            except Exception as error:
                logger.warning("Editor synchronization failed reg=%s code=%s", reg, getattr(error, "code", type(error).__name__))
        try:
            await asyncio.wait_for(stop_event.wait(), service.settings.EDITOR_SYNC_SECONDS)
        except TimeoutError:
            pass
