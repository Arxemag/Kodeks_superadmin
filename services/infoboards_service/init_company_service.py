"""
Сервис init_company: инициализация компании, синхронизация маппинга, групп и ACL.

Flow: получить base_url по reg -> admin login -> sync mapping table ->
sync groups -> get boards -> build ACL -> GET /admin/dir?n=X (users) -> POST /admin/dir.
Коммит offset только после полного успеха (идемпотентность).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.catalog_lock import canonical_catalog_url, catalog_operation
from common.acl_ownership import AclOwnershipRepository, plan_acl_assignments
from common.config import Settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.logger import get_logger
from services.auth_service.service import AuthService
from services.infoboards_service.admin_dirs import (
    build_admin_dirs_form,
    parse_acl_from_html,
    parse_form_pairs,
    acl_control_names,
    acl_checkbox_values,
    verify_acl_form,
    acl_values,
    acl_write_needed,
)
from services.infoboards_service.cabinet_acl import (
    apply_acl_via_browser,
    fetch_acl_form_via_http,
    get_boards,
)
from services.infoboards_service.dto import InitCompanyDTO
from services.users_service.catalog_client import CatalogClient
from services.users_service.html_parser import group_index, normalize_title, parse_groups_catalog


logger = get_logger("infoboards.init_company")


@dataclass
class MappingRow:
    """Запись из department_service_mapping."""
    id: int
    department_id: str
    service_group_name: str
    reg: str
    client_id: str
    client_name: str
    mapping_status: str
    catalog_group_id: int | None = None
    catalog_group_name: str | None = None


@dataclass
class InitCompanyService:
    """Сервис init_company: БД, настройки и HTTP-клиент."""
    db: AsyncSession
    settings: Settings
    http_client: httpx.AsyncClient
    catalog_client: CatalogClient
    acl_ownership: AclOwnershipRepository = field(default_factory=AclOwnershipRepository)

    async def handle(self, dto: InitCompanyDTO, *, dry_run: bool = False) -> None:
        """
        Полный flow init_company: БД sync, группы, ACL.
        При dry_run — только синхронизация таблицы маппинга (без HTTP к каталогу).
        При ошибке — исключение (worker отправит в DLQ или retry).
        """
        reg = dto.reg
        logger.debug(
            "handle(reg=%r, id=%r, companyName=%r, departments_count=%s, dry_run=%s)",
            reg, dto.id, dto.companyName, len(dto.departments), dry_run,
        )
        if dto.departments:
            logger.debug(
                "handle departments: %s",
                [(str(d.id), d.title, d.modules) for d in dto.departments],
            )
        base_url = await self._get_base_url(reg)
        logger.debug("_get_base_url(reg=%r) -> base_url=%r", reg, base_url)

        async with catalog_operation(base_url):
            # Шаг 3: синхронизация таблицы маппинга
            await self._sync_mapping_table(reg, dto)

            if dry_run:
                logger.info("dry_run: пропуск групп, boards, ACL")
                return

            auth_service = AuthService(db=self.db, settings=self.settings)
            cookies = await auth_service.login(reg=reg, name=None)

            # Шаг 4: синхронизация групп в сервисе
            groups_dict = await self._sync_groups(base_url, cookies, reg)
            logger.debug("_sync_groups -> groups_count=%s keys_sample=%s", len(groups_dict), list(groups_dict.keys())[:15])

            # Шаг 5.1: получение кабинетов
            boards_dict = await self._get_boards(base_url, cookies)
            logger.debug("_get_boards -> boards_count=%s mapping=%s", len(boards_dict), list(boards_dict.items())[:20])

            # Шаг 5.2: формирование ACL-матрицы
            dep_id_to_modules = {str(d.id): d.modules for d in dto.departments}
            logger.debug("_build_acl_async input: reg=%r dep_id_to_modules=%s", reg, dep_id_to_modules)
            acl_matrix = await self._build_acl_async(
                reg, groups_dict, boards_dict, dep_id_to_modules
            )
            logger.debug("_build_acl_async -> acl_matrix=%s", acl_matrix)

            # Шаг 6: POST /admin/dirs
            logger.debug("_apply_acl input: base_url=%r acl_matrix=%s boards_dict_keys=%s", base_url, acl_matrix, list(boards_dict.keys()))
            await self._apply_acl(base_url, cookies, acl_matrix, boards_dict, reg=reg)

    async def _get_base_url(self, reg: str) -> str:
        """Base_url по reg; при отсутствии — AuthError REG_NOT_FOUND (DLQ)."""
        tbl = self.settings.DB_TABLE_REG_SERVICES
        res = await self.db.execute(
            text(f"SELECT base_url FROM {tbl} WHERE reg_number = :reg"),
            {"reg": reg},
        )
        value = res.scalar_one_or_none()
        if not value:
            raise AuthError(code="REG_NOT_FOUND", message=f"reg '{reg}' not found", http_status=404)
        return str(value).rstrip("/")

    async def _sync_mapping_table(self, reg: str, dto: InitCompanyDTO) -> None:
        """
        3.1–3.3: select, insert/update, archive.
        Заполнение department_service_mapping:
          id — BIGSERIAL (авто)
          department_id — из departments[].id (UUID)
          service_group_name — из departments[].title (название группы в каталоге)
          reg — из dto.reg
          client_id — из dto.id (ID компании/клиента)
          client_name — из dto.companyName
          mapping_status — 'active' при insert/update, 'archived' для отделов, убранных из payload
        """
        if "departments" not in dto.model_fields_set:
            raise ParseError(code="INCOMPLETE_DEPARTMENT_SNAPSHOT", message="Explicit departments snapshot is required")
        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        client_id = str(dto.id)
        company_name = dto.companyName
        incoming_ids = {str(d.id) for d in dto.departments}
        logger.debug("_sync_mapping_table(reg=%r, client_id=%r, company_name=%r, incoming_ids=%s)", reg, client_id, company_name, incoming_ids)

        # 3.1 Существующие записи
        res = await self.db.execute(
            text(f"""
                SELECT id, department_id::text, service_group_name, reg, client_id, client_name, mapping_status,
                       catalog_group_id, catalog_group_name
                FROM {tbl} WHERE reg = :reg
            """),
            {"reg": reg},
        )
        rows = list(res)
        existing = {r[1]: MappingRow(*r) for r in rows}
        logger.debug("_sync_mapping_table existing count=%s dept_ids=%s", len(existing), list(existing.keys()))
        if not dto.departments and existing and not getattr(dto, "allow_empty_snapshot", False):
            raise ParseError(code="EMPTY_DEPARTMENT_SNAPSHOT", message="Clearing existing departments requires allow_empty_snapshot=true")
        if any(row.client_id != client_id for row in existing.values()):
            raise ParseError(code="COMPANY_REG_CONFLICT", message="Registration is already mapped to a different platform company")
        has_company_name = "companyName" in dto.model_fields_set
        if not has_company_name:
            company_name = next((row.client_name for row in existing.values() if row.client_name), "")

        # 3.2 Обработка входящих departments
        for dept in dto.departments:
            dept_id = str(dept.id)
            title = dept.title
            if dept_id in existing:
                await self.db.execute(
                    text(f"""
                        UPDATE {tbl}
                        SET catalog_group_name = COALESCE(catalog_group_name, service_group_name),
                            service_group_name = :title,
                            client_name = CASE WHEN :has_company_name THEN :company_name ELSE client_name END,
                            mapping_status = 'active'
                        WHERE reg = :reg AND department_id = :dept_id
                    """),
                    {"title": title, "company_name": company_name, "has_company_name": has_company_name, "reg": reg, "dept_id": dept_id},
                )
            else:
                await self.db.execute(
                    text(f"""
                        INSERT INTO {tbl}
                        (department_id, service_group_name, reg, client_id, client_name, mapping_status, catalog_group_name)
                        VALUES (:dept_id, :title, :reg, :client_id, :company_name, 'active', :title)
                    """),
                    {
                        "dept_id": dept_id,
                        "title": title,
                        "reg": reg,
                        "client_id": client_id,
                        "company_name": company_name,
                    },
                )

        # 3.3 Архивация удалённых отделов (department_id отсутствует в новом payload)
        to_archive = existing.keys() - incoming_ids
        if to_archive:
            from sqlalchemy import bindparam
            stmt = text(f"""
                UPDATE {tbl}
                SET mapping_status = 'archived'
                WHERE reg = :reg AND mapping_status = 'active'
                AND department_id::text IN :ids
            """).bindparams(bindparam("ids", expanding=True))
            await self.db.execute(stmt, {"reg": reg, "ids": list(to_archive)})
        await self.db.commit()

    async def _save_group_binding(self, reg: str, department_id: str, group_id: int, confirmed_name: str, origin: str) -> None:
        """Persist a verified identity before subsequent writes, including rename."""
        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        try:
            await self.db.execute(
                text(f"""
                    UPDATE {tbl} SET catalog_group_id = :group_id, catalog_group_name = :confirmed_name,
                      catalog_group_origin = COALESCE(catalog_group_origin, :origin)
                    WHERE reg = :reg AND department_id::text = :department_id
                """),
                {"group_id": group_id, "confirmed_name": confirmed_name, "origin": origin,
                 "reg": reg, "department_id": department_id},
            )
            await self.db.commit()
        except IntegrityError as error:
            await self.db.rollback()
            raise ParseError(code="GROUP_BINDING_CONFLICT", message="Catalog group is already bound to another department") from error

    async def _ensure_group_not_shared(self, base_url: str, reg: str, group_id: int) -> None:
        """Renaming a group also changes it for any other registration using it."""
        mapping = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        registrations = self.settings.DB_TABLE_REG_SERVICES
        result = await self.db.execute(text(f"""
            SELECT m.reg, s.base_url
            FROM {mapping} AS m
            LEFT JOIN {registrations} AS s ON s.reg_number = m.reg
            WHERE m.catalog_group_id = :group_id AND m.reg <> :reg
              AND m.mapping_status = 'active'
        """), {"group_id": group_id, "reg": reg})
        current_catalog = canonical_catalog_url(base_url)
        for other_reg, other_url in result:
            if not other_url:
                raise ParseError(code="GROUP_SHARING_UNKNOWN", message=f"Cannot identify catalog for registration {other_reg!r}")
            try:
                other_catalog = canonical_catalog_url(other_url)
            except ValueError as error:
                raise ParseError(code="GROUP_SHARING_UNKNOWN", message=f"Invalid catalog address for registration {other_reg!r}") from error
            if other_catalog == current_catalog:
                raise ParseError(code="GROUP_SHARED_ACROSS_REGISTRATIONS", message=f"Group ID {group_id} is also bound to registration {other_reg!r}")

    async def _sync_groups(
        self, base_url: str, cookies: dict[str, str], reg: str
    ) -> dict[str, int]:
        """Bind departments to stable catalog IDs; renames preserve identity."""
        async def read_groups():
            groups = parse_groups_catalog(await self.catalog_client.get_groups_page(base_url, cookies))
            index = group_index(groups)
            by_id = {}
            for title, group_id in groups.items():
                if group_id in by_id and normalize_title(by_id[group_id]) != normalize_title(title):
                    raise ParseError(code="AMBIGUOUS_GROUP", message=f"Catalog group ID {group_id} has conflicting names")
                by_id[group_id] = title
            return groups, index, by_id

        groups, index, by_id = await read_groups()
        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        result = await self.db.execute(
            text(f"""
                SELECT department_id::text, service_group_name, catalog_group_id, catalog_group_name,
                       catalog_group_origin, catalog_group_rename_allowed
                FROM {tbl} WHERE reg = :reg AND mapping_status = 'active'
                ORDER BY department_id
            """),
            {"reg": reg},
        )
        rows = list(result)
        desired_names = {}
        owners = {}
        for department_id, title, saved_id, _, _, _ in rows:
            normalized = normalize_title(title)
            if not normalized or (normalized in desired_names and desired_names[normalized] != department_id):
                raise ParseError(code="GROUP_BINDING_CONFLICT", message=f"Several departments request the same group name: {title!r}")
            desired_names[normalized] = department_id
            if saved_id is not None:
                if saved_id not in by_id:
                    raise ParseError(code="GROUP_BINDING_BROKEN", message=f"Bound catalog group ID {saved_id} no longer exists")
                if saved_id in owners and owners[saved_id] != department_id:
                    raise ParseError(code="GROUP_BINDING_CONFLICT", message="Several departments share a catalog group ID")
                owners[saved_id] = department_id

        for department_id, title, saved_id, confirmed_name, saved_origin, rename_allowed in rows:
            desired = normalize_title(title)
            group_id = saved_id
            origin = saved_origin or "adopted"
            if group_id is not None:
                if group_id not in by_id:
                    raise ParseError(code="GROUP_BINDING_BROKEN", message=f"Bound catalog group ID {group_id} no longer exists")
            else:
                original = normalize_title(confirmed_name or title)
                group_id = index.get(original)
                if group_id is None:
                    if original != desired:
                        raise ParseError(code="GROUP_BINDING_BROKEN", message=f"Original group for renamed department was not found: {confirmed_name!r}")
                    created = await self.catalog_client.create_group(base_url, title, cookies)
                    origin = "created" if created is True else "adopted"
                    groups, index, by_id = await read_groups()
                    group_id = index.get(desired)
                    if group_id is None:
                        raise ParseError(code="GROUP_CREATE_NOT_CONFIRMED", message=f"Created group was not found in catalog: {title!r}")
                if group_id in owners and owners[group_id] != department_id:
                    raise ParseError(code="GROUP_BINDING_CONFLICT", message="Catalog group is already owned by another department")
                owners[group_id] = department_id
                # Persist adoption/creation before rename so a crash after the
                # external rename can recover by ID rather than create a copy.
                await self._save_group_binding(reg, department_id, group_id, by_id[group_id], origin)

            if normalize_title(by_id[group_id]) != desired:
                target_id = index.get(desired)
                if target_id is not None and target_id != group_id:
                    raise ParseError(code="GROUP_BINDING_CONFLICT", message=f"Rename target belongs to another catalog group: {title!r}")
                if origin != "created" and not rename_allowed:
                    raise ParseError(code="ADOPTED_GROUP_RENAME_REQUIRES_APPROVAL", message=f"Renaming adopted catalog group ID {group_id} requires an explicit migration decision")
                await self._ensure_group_not_shared(base_url, reg, group_id)
                await self.catalog_client.rename_group(base_url, group_id, title, cookies)
                groups, index, by_id = await read_groups()
                if group_id not in by_id or normalize_title(by_id[group_id]) != desired:
                    raise ParseError(code="GROUP_RENAME_NOT_CONFIRMED", message=f"Catalog rename was not confirmed for group ID {group_id}")
            await self._save_group_binding(reg, department_id, group_id, by_id[group_id], origin)
        return groups
    async def _get_boards(self, base_url: str, cookies: dict[str, str]) -> dict[str, str]:
        """5.1: GraphQL board.many -> title -> id (через cabinet_acl.get_boards)."""
        logger.debug("_get_boards(base_url=%r)", base_url)
        try:
            return await get_boards(base_url, self.http_client, cookies)
        except NetworkError as e:
            if e.code == "GRAPHQL_FAILED":
                raise NetworkError(
                    code="INFOBOARDS_4XX" if (e.message or "").find("4") >= 0 else "INFOBOARDS_5XX",
                    message=e.message or "GraphQL boards failed",
                    http_status=502,
                ) from e
            raise
        except ParseError:
            raise
        except Exception as e:
            if "JSON" in str(e):
                raise ParseError(
                    code="INFOBOARDS_PARSE_ERROR",
                    message="GraphQL boards response is not JSON",
                ) from e
            raise NetworkError(code="INFOBOARDS_REQUEST_FAILED", message="GraphQL boards failed") from e

    async def _build_acl_async(
        self,
        reg: str,
        groups_dict: dict[str, int],
        boards_dict: dict[str, str],
        dep_id_to_modules: dict[str, list[str]],
    ) -> dict[str, list[int]]:
        """5.2: board_id -> [group_ids]."""
        logger.debug(
            "_build_acl_async(reg=%r, groups_dict_len=%s, boards_dict_len=%s, dep_id_to_modules=%s)",
            reg, len(groups_dict), len(boards_dict), dep_id_to_modules,
        )
        group_index(groups_dict)  # Reject ambiguous catalog reads before planning.
        catalog_group_ids = set(groups_dict.values())
        normalized_boards = {}
        for title, board_id in boards_dict.items():
            normalized = normalize_title(title)
            if not normalized or (normalized in normalized_boards and normalized_boards[normalized] != board_id):
                raise ParseError(code="AMBIGUOUS_CABINET", message=f"Several cabinets match title {title!r}")
            normalized_boards[normalized] = board_id
        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        res = await self.db.execute(
            text(f"""
                SELECT department_id::text, catalog_group_id
                FROM {tbl}
                WHERE reg = :reg AND mapping_status = 'active'
            """),
            {"reg": reg},
        )
        acl: dict[str, list[int]] = {}
        for row in res:
            dept_id, group_id = row[0], row[1]
            if group_id is None or group_id not in catalog_group_ids:
                raise ParseError(
                    code="GROUP_BINDING_BROKEN",
                    message=f"Department has no confirmed existing catalog group ID: {dept_id!r}",
                )
            modules = dep_id_to_modules.get(dept_id, [])
            for mod_title in modules:
                board_id = normalized_boards.get(normalize_title(mod_title))
                if board_id:
                    acl.setdefault(board_id, []).append(group_id)
                else:
                    raise ParseError(code="CABINET_NOT_FOUND", message=f"Required cabinet was not found: {mod_title!r}")
        for key in acl:
            acl[key] = list(dict.fromkeys(acl[key]))  # уникальные id с сохранением порядка
        return acl

    async def _apply_acl(
        self,
        base_url: str,
        cookies: dict[str, str],
        acl_matrix: dict[str, list[int]],
        boards_dict: dict[str, str],
        *,
        reg: str,
    ) -> None:
        """Submit a complete known form, then confirm ACLs by a fresh read."""
        from urllib.parse import urlencode

        owned, owned_elsewhere = await self.acl_ownership.get(base_url, reg)
        if not acl_matrix and not owned:
            return
        next_owned = None

        async def prepare_fields(html: str) -> dict[str, list[int]]:
            nonlocal next_owned
            pairs = parse_form_pairs(html, acl=True, include_save=True)
            current = {name: {int(group) for group in value["kw"]} for name, value in acl_values(pairs).items()}
            desired = {}
            for board, group_ids in acl_matrix.items():
                desired[f"acl_infoboard_{board.lower()}"] = set(group_ids)
                desired[f"acl_infoboard_{board.lower()}_view"] = set(group_ids)
            overrides, next_owned = plan_acl_assignments(current, owned, desired, owned_elsewhere)
            return overrides

        setup_html, post_url, _base, docs_n = await fetch_acl_form_via_http(
            base_url, self.http_client, cookies,
        )
        if not parse_acl_from_html(setup_html):
            await apply_acl_via_browser(
                base_url, cookies, docs_n, {}, prepare_field_overrides=prepare_fields,
            )
            if next_owned is None:
                raise ParseError(code="ACL_PLAN_INVALID", message="Browser ACL ownership plan was not prepared")
            try:
                await self.acl_ownership.replace(base_url, reg, next_owned)
            except NetworkError as error:
                raise ParseError(code="ACL_OWNERSHIP_PERSIST_UNCERTAIN", message="Browser ACL state was verified but ownership could not be recorded; reconcile before retrying") from error
            return
        full_form = parse_form_pairs(setup_html, acl=True, include_save=True)
        overrides = await prepare_fields(setup_html)
        form_data = build_admin_dirs_form(
            full_form, {}, available_controls=acl_control_names(setup_html),
            checkbox_values=acl_checkbox_values(setup_html),
            field_overrides=overrides,
        )
        if not acl_write_needed(form_data, setup_html):
            await self.acl_ownership.replace(base_url, reg, next_owned)
            return
        try:
            response = await self.http_client.post(
                post_url, content=urlencode(form_data), cookies=cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{base_url}/admin/dir", "Origin": base_url.rstrip("/")},
            )
        except httpx.RequestError as error:
            raise ParseError(code="ACL_WRITE_UNCERTAIN", message="ACL POST outcome is unknown; inspect the catalog before retrying") from error
        if response.status_code in (401, 403):
            raise AuthError(code="CATALOG_AUTH_FAILED", message="Admin cookies rejected for ACL write", http_status=401)
        if response.status_code >= 500:
            raise ParseError(code="ACL_WRITE_UNCERTAIN", message=f"ACL POST returned HTTP {response.status_code}; inspect the catalog before retrying")
        if response.status_code >= 400:
            raise NetworkError(code="ADMIN_DIRS_FAILED", message=f"ACL POST status={response.status_code}")
        try:
            actual_html, _, _, _ = await fetch_acl_form_via_http(
                base_url, self.http_client, cookies, docs_n=docs_n,
            )
        except Exception as error:
            raise ParseError(code="ACL_WRITE_UNCERTAIN", message="ACL POST returned, but its result cannot be read; inspect the catalog before retrying") from error
        verify_acl_form(form_data, actual_html)
        try:
            await self.acl_ownership.replace(base_url, reg, next_owned)
        except NetworkError as error:
            raise ParseError(code="ACL_OWNERSHIP_PERSIST_UNCERTAIN", message="ACL change was verified but ownership could not be recorded; reconcile before retrying") from error
