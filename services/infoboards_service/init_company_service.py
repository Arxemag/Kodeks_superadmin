"""
Сервис init_company: инициализация компании, синхронизация маппинга, групп и ACL.

Flow: получить base_url по reg -> admin login -> sync mapping table ->
sync groups -> get boards -> build ACL -> GET /admin/dir?n=X (users) -> POST /admin/dir.
Коммит offset только после полного успеха (идемпотентность).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import Settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.logger import get_logger
from services.auth_service.service import AuthService
from services.infoboards_service.admin_dirs import (
    build_admin_dirs_form,
    parse_acl_from_html,
    parse_full_form_from_html,
)
from services.infoboards_service.cabinet_acl import (
    get_boards,
    get_docs_n_from_dirs,
)
from services.infoboards_service.dto import InitCompanyDTO
from services.users_service.catalog_client import CatalogClient
from services.users_service.html_parser import parse_groups_catalog


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


@dataclass
class InitCompanyService:
    """Сервис init_company: БД, настройки и HTTP-клиент."""
    db: AsyncSession
    settings: Settings
    http_client: httpx.AsyncClient
    catalog_client: CatalogClient

    async def handle(self, dto: InitCompanyDTO, *, dry_run: bool = False) -> None:
        """
        Полный flow init_company: БД sync, группы, ACL.
        При dry_run — только синхронизация таблицы маппинга (без HTTP к каталогу).
        При ошибке — исключение (worker отправит в DLQ или retry).
        """
        reg = dto.reg
        base_url = await self._get_base_url(reg)

        # Шаг 3: синхронизация таблицы маппинга
        await self._sync_mapping_table(reg, dto)

        if dry_run:
            logger.info("dry_run: пропуск групп, boards, ACL")
            return

        auth_service = AuthService(db=self.db, settings=self.settings)
        cookies = await auth_service.login(reg=reg, name=None)

        # Шаг 4: синхронизация групп в сервисе
        groups_dict = await self._sync_groups(base_url, cookies, reg)

        # Шаг 5.1: получение кабинетов
        boards_dict = await self._get_boards(base_url, cookies)

        # Шаг 5.2: формирование ACL-матрицы
        dep_id_to_modules = {str(d.id): d.modules for d in dto.departments}
        acl_matrix = await self._build_acl_async(
            reg, groups_dict, boards_dict, dep_id_to_modules
        )

        # Шаг 6: POST /admin/dirs
        await self._apply_acl(base_url, cookies, acl_matrix, boards_dict)

    async def _get_base_url(self, reg: str) -> str:
        """Base_url по reg; при отсутствии — AuthError REG_NOT_FOUND (DLQ)."""
        res = await self.db.execute(
            text("SELECT base_url FROM reg_services WHERE reg_number = :reg"),
            {"reg": reg},
        )
        value = res.scalar_one_or_none()
        if not value:
            raise AuthError(code="REG_NOT_FOUND", message=f"reg '{reg}' not found", http_status=404)
        return str(value).rstrip("/")

    async def _sync_mapping_table(self, reg: str, dto: InitCompanyDTO) -> None:
        """3.1–3.3: select, insert/update, archive."""
        client_id = str(dto.id)
        company_name = dto.companyName
        incoming_ids = {str(d.id) for d in dto.departments}

        # 3.1 Существующие записи
        res = await self.db.execute(
            text("""
                SELECT id, department_id::text, service_group_name, reg, client_id, client_name, mapping_status
                FROM department_service_mapping WHERE reg = :reg
            """),
            {"reg": reg},
        )
        rows = list(res)
        existing = {r[1]: MappingRow(*r) for r in rows}

        # 3.2 Обработка входящих departments
        for dept in dto.departments:
            dept_id = str(dept.id)
            title = dept.title
            if dept_id in existing:
                await self.db.execute(
                    text("""
                        UPDATE department_service_mapping
                        SET service_group_name = :title, client_name = :company_name, mapping_status = 'active'
                        WHERE reg = :reg AND department_id = :dept_id
                    """),
                    {"title": title, "company_name": company_name, "reg": reg, "dept_id": dept_id},
                )
            else:
                await self.db.execute(
                    text("""
                        INSERT INTO department_service_mapping
                        (department_id, service_group_name, reg, client_id, client_name, mapping_status)
                        VALUES (:dept_id, :title, :reg, :client_id, :company_name, 'active')
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
            stmt = text("""
                UPDATE department_service_mapping
                SET mapping_status = 'archived'
                WHERE reg = :reg AND mapping_status = 'active'
                AND department_id::text IN :ids
            """).bindparams(bindparam("ids", expanding=True))
            await self.db.execute(stmt, {"reg": reg, "ids": list(to_archive)})
        await self.db.commit()

    async def _sync_groups(
        self, base_url: str, cookies: dict[str, str], reg: str
    ) -> dict[str, int]:
        """4.1–4.2: GET groups, парсинг, создание недостающих."""
        html = await self.catalog_client.get_groups_page(base_url, cookies)
        groups: dict[str, int] = parse_groups_catalog(html)

        res = await self.db.execute(
            text("""
                SELECT service_group_name FROM department_service_mapping
                WHERE reg = :reg AND mapping_status = 'active'
            """),
            {"reg": reg},
        )
        active_names = {row[0] for row in res}

        for name in active_names:
            if name not in groups:
                await self.catalog_client.create_group(base_url, name, cookies)
                # После создания — перезагружаем страницу и обновляем словарь
                html = await self.catalog_client.get_groups_page(base_url, cookies)
                groups = parse_groups_catalog(html)

        return groups

    async def _get_boards(self, base_url: str, cookies: dict[str, str]) -> dict[str, str]:
        """5.1: GraphQL board.many -> title -> id (через cabinet_acl.get_boards)."""
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
        res = await self.db.execute(
            text("""
                SELECT department_id::text, service_group_name
                FROM department_service_mapping
                WHERE reg = :reg AND mapping_status = 'active'
            """),
            {"reg": reg},
        )
        acl: dict[str, list[int]] = {}
        for row in res:
            dept_id, service_group_name = row[0], row[1]
            group_id = groups_dict.get(service_group_name)
            if group_id is None:
                continue
            modules = dep_id_to_modules.get(dept_id, [])
            for mod_title in modules:
                board_id = boards_dict.get(mod_title)
                if board_id:
                    acl.setdefault(board_id, []).append(group_id)
        for key in acl:
            acl[key] = list(dict.fromkeys(acl[key]))  # уникальные id с сохранением порядка
        return acl

    async def _apply_acl(
        self,
        base_url: str,
        cookies: dict[str, str],
        acl_matrix: dict[str, list[int]],
        boards_dict: dict[str, str],
    ) -> None:
        """6: GET /admin/dirs → найти n для /users/ → GET /admin/dir?n=X → merge → POST."""
        dirs_url = f"{base_url}/admin/dirs"
        try:
            resp = await self.http_client.get(dirs_url, cookies=cookies)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise NetworkError(code="ADMIN_DIRS_FAILED", message="GET /admin/dirs failed") from e
        if resp.status_code in (401, 403):
            raise AuthError(
                code="CATALOG_AUTH_FAILED",
                message="Admin cookies not accepted for /admin/dirs",
                http_status=401,
            )
        if resp.status_code >= 400:
            raise NetworkError(
                code="ADMIN_DIRS_4XX",
                message=f"GET /admin/dirs status={resp.status_code}",
                http_status=502,
            )
        dirs_html = resp.text or ""
        docs_n = get_docs_n_from_dirs(dirs_html)
        dir_url = f"{base_url}/admin/dir?n={docs_n}"
        try:
            resp = await self.http_client.get(dir_url, cookies=cookies)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise NetworkError(code="ADMIN_DIRS_FAILED", message="GET /admin/dir failed") from e
        if resp.status_code in (401, 403):
            raise AuthError(
                code="CATALOG_AUTH_FAILED",
                message="Admin cookies not accepted for /admin/dir",
                http_status=401,
            )
        if resp.status_code >= 400:
            raise NetworkError(
                code="ADMIN_DIRS_4XX",
                message=f"GET {dir_url} status={resp.status_code}",
                http_status=502,
            )
        page_html = resp.text or ""
        setup_form = parse_full_form_from_html(page_html)
        if not setup_form:
            setup_form = {"n": docs_n, "set": ""}
        setup_form["setup2"] = "Настройки сервисов"
        setup_form.setdefault("n", docs_n)
        from urllib.parse import urlencode as _urlencode
        encoded_setup = _urlencode([(k, str(v)) for k, v in setup_form.items()])
        try:
            resp = await self.http_client.post(
                f"{base_url}/admin/dir",
                content=encoded_setup,
                cookies=cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise NetworkError(code="ADMIN_DIRS_FAILED", message="POST setup2 failed") from e
        if resp.status_code >= 400:
            raise NetworkError(
                code="ADMIN_DIRS_4XX",
                message=f"POST setup2 status={resp.status_code}",
                http_status=502,
            )
        setup_html = resp.text or ""
        has_acl = "acl_infoboard_" in setup_html or bool(parse_acl_from_html(setup_html))
        post_url = f"{base_url}/admin/dirs"
        acl_form_base = ""
        if not has_acl:
            for try_url in [
                f"{base_url}/admin/dirs?base=%2Fdocs",
                f"{base_url}/admin/dirs?base=%2Fusers",
                f"{base_url}/admin/dirs",
            ]:
                try:
                    r = await self.http_client.get(try_url, cookies=cookies)
                except (httpx.TimeoutException, httpx.NetworkError):
                    continue
                if r.status_code == 200:
                    ch = r.text or ""
                    if "acl_infoboard_" in ch or parse_acl_from_html(ch):
                        setup_html = ch
                        has_acl = True
                        post_url = f"{base_url}/admin/dirs"
                        m = re.search(r"base=([^&'\"]+)", try_url, re.I)
                        if m:
                            acl_form_base = m.group(1)
                        break
        else:
            post_url = f"{base_url}/admin/dirs"
            acl_form_base = "/docs"
        full_form = parse_full_form_from_html(setup_html)
        if acl_form_base and "base" not in full_form:
            full_form["base"] = acl_form_base
        if not full_form:
            acl_by_key = parse_acl_from_html(setup_html)
            full_form = {f"acl_infoboard_{k}": v for k, v in acl_by_key.items()}
        form_data = build_admin_dirs_form(
            full_form,
            acl_overrides=acl_matrix,
            extra_board_ids={bid for bid in boards_dict.values()},
        )
        from urllib.parse import urlencode
        encoded = urlencode(form_data)
        logger.debug(f"POST {post_url} fields_count={len(form_data)}")
        try:
            resp = await self.http_client.post(
                post_url,
                content=encoded,
                cookies=cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise NetworkError(code="ADMIN_DIRS_FAILED", message="POST /admin/dirs failed") from e

        if resp.status_code in (401, 403):
            raise AuthError(
                code="CATALOG_AUTH_FAILED",
                message="Admin cookies not accepted for /admin/dirs",
                http_status=401,
            )
        if resp.status_code >= 500:
            raise NetworkError(code="ADMIN_DIRS_5XX", message="admin/dirs 5xx")
        if resp.status_code >= 400:
            raise NetworkError(
                code="ADMIN_DIRS_4XX",
                message=f"admin/dirs status={resp.status_code}",
                http_status=502,
            )
