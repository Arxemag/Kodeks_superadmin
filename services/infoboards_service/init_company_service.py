"""
Сервис init_company: инициализация компании, синхронизация маппинга, групп и ACL.

Flow: получить base_url по reg -> admin login -> sync mapping table ->
sync groups -> get boards -> build ACL -> GET /admin/dir?n=X (users) -> POST /admin/dir.
Коммит offset только после полного успеха (идемпотентность).
"""
from __future__ import annotations

import asyncio
import os
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
    apply_acl_via_browser,
    fetch_acl_form_via_http,
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
        await self._apply_acl(base_url, cookies, acl_matrix, boards_dict)

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
        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        client_id = str(dto.id)
        company_name = dto.companyName
        incoming_ids = {str(d.id) for d in dto.departments}
        logger.debug("_sync_mapping_table(reg=%r, client_id=%r, company_name=%r, incoming_ids=%s)", reg, client_id, company_name, incoming_ids)

        # 3.1 Существующие записи
        res = await self.db.execute(
            text(f"""
                SELECT id, department_id::text, service_group_name, reg, client_id, client_name, mapping_status
                FROM {tbl} WHERE reg = :reg
            """),
            {"reg": reg},
        )
        rows = list(res)
        existing = {r[1]: MappingRow(*r) for r in rows}
        logger.debug("_sync_mapping_table existing count=%s dept_ids=%s", len(existing), list(existing.keys()))

        # 3.2 Обработка входящих departments
        for dept in dto.departments:
            dept_id = str(dept.id)
            title = dept.title
            if dept_id in existing:
                await self.db.execute(
                    text(f"""
                        UPDATE {tbl}
                        SET service_group_name = :title, client_name = :company_name, mapping_status = 'active'
                        WHERE reg = :reg AND department_id = :dept_id
                    """),
                    {"title": title, "company_name": company_name, "reg": reg, "dept_id": dept_id},
                )
            else:
                await self.db.execute(
                    text(f"""
                        INSERT INTO {tbl}
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
            stmt = text(f"""
                UPDATE {tbl}
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
        logger.debug("_sync_groups(base_url=%r, reg=%r)", base_url, reg)
        html = await self.catalog_client.get_groups_page(base_url, cookies)
        groups: dict[str, int] = parse_groups_catalog(html)
        logger.debug("_sync_groups GET groups parsed count=%s", len(groups))

        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        res = await self.db.execute(
            text(f"""
                SELECT service_group_name FROM {tbl}
                WHERE reg = :reg AND mapping_status = 'active'
            """),
            {"reg": reg},
        )
        active_names = {row[0] for row in res}
        logger.debug("_sync_groups active_names from DB=%s", active_names)

        for name in active_names:
            if name not in groups:
                await self.catalog_client.create_group(base_url, name, cookies)
                # После создания — перезагружаем страницу и обновляем словарь
                html = await self.catalog_client.get_groups_page(base_url, cookies)
                groups = parse_groups_catalog(html)

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
        tbl = self.settings.DB_TABLE_DEPARTMENT_MAPPING
        res = await self.db.execute(
            text(f"""
                SELECT department_id::text, service_group_name
                FROM {tbl}
                WHERE reg = :reg AND mapping_status = 'active'
            """),
            {"reg": reg},
        )
        acl: dict[str, list[int]] = {}
        for row in res:
            dept_id, service_group_name = row[0], row[1]
            group_id = groups_dict.get(service_group_name)
            if group_id is None:
                logger.debug("_build_acl_async skip dept_id=%s service_group_name=%r (group not in catalog)", dept_id, service_group_name)
                continue
            modules = dep_id_to_modules.get(dept_id, [])
            for mod_title in modules:
                board_id = boards_dict.get(mod_title)
                if board_id:
                    acl.setdefault(board_id, []).append(group_id)
                else:
                    logger.debug("_build_acl_async skip mod_title=%r (board not in boards_dict)", mod_title)
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
        """6: Та же логика, что add_cabinet_group — форма через fetch_acl_form_via_http, затем POST /admin/dirs."""
        from urllib.parse import unquote, urlencode

        logger.debug("_apply_acl calling fetch_acl_form_via_http(base_url=%r)", base_url)
        setup_html, post_url, acl_form_base, _docs_n = await fetch_acl_form_via_http(
            base_url, self.http_client, cookies
        )
        logger.debug("_apply_acl fetch_acl_form_via_http -> setup_html_len=%s post_url=%r acl_form_base=%r docs_n=%s", len(setup_html), post_url, acl_form_base, _docs_n)
        # Если форма рендерится через JS, в HTML нет acl_infoboard_* — применяем ACL в той же браузерной сессии и выходим.
        acl_from_html = parse_acl_from_html(setup_html)
        if not acl_from_html and acl_matrix:
            try:
                await apply_acl_via_browser(base_url, cookies, _docs_n, acl_matrix)
                return
            except NetworkError as e:
                if getattr(e, "code", None) == "PLAYWRIGHT_NOT_INSTALLED":
                    logger.warning("Playwright не установлен, форма без acl_infoboard_*: %s", e.message)
                else:
                    raise
        full_form = parse_full_form_from_html(setup_html)
        logger.debug("_apply_acl parse_full_form_from_html -> full_form_keys_count=%s keys=%s", len(full_form), sorted(full_form.keys()))
        # Не добавляем base/path/action: в примере параметров POST /admin/dirs их нет, каталог может 500 на лишних полях.
        if not full_form:
            acl_by_key = parse_acl_from_html(setup_html)
            full_form = {f"acl_infoboard_{k}": v for k, v in acl_by_key.items()}
            logger.debug("_apply_acl full_form was empty, filled from parse_acl_from_html -> keys=%s", sorted(full_form.keys()))
        # Ключи кабинетов: из full_form или из parse_acl_from_html (если форма парсится иначе).
        form_board_keys = set()
        for k in full_form:
            if k.startswith("acl_infoboard_"):
                base = k.rsplit("_view", 1)[0] if k.endswith("_view") else k
                base = base.replace("acl_infoboard_", "").lower()
                if base and not base.endswith("_view"):
                    form_board_keys.add(base)
        if not form_board_keys:
            acl_by_key = parse_acl_from_html(setup_html)
            form_board_keys = set(acl_by_key.keys())
            logger.debug("_apply_acl form_board_keys was empty, fallback parse_acl_from_html -> form_board_keys=%s", sorted(form_board_keys))
            for bk, acl_val in acl_by_key.items():
                full_form.setdefault(f"acl_infoboard_{bk}", acl_val)
                full_form.setdefault(f"acl_infoboard_{bk}_view", acl_val)
        else:
            logger.debug("_apply_acl form_board_keys from full_form=%s", sorted(form_board_keys))

        acl_overrides_safe = {
            k: v for k, v in acl_matrix.items()
            if k.lower() in form_board_keys
        }
        # Если каталог вернул форму без acl_infoboard_* (форма рендерится через JS), собираем минимальную форму только по нашим кабинетам.
        if acl_matrix and not form_board_keys:
            logger.info(
                "Форма каталога без полей acl_infoboard_* (вероятно JS). Используем минимальную форму только для кабинетов acl_matrix=%s",
                list(acl_matrix.keys()),
            )
            for bk in acl_matrix:
                bkl = bk.lower()
                form_board_keys.add(bkl)
                full_form.setdefault(f"acl_infoboard_{bkl}", "")
                full_form.setdefault(f"acl_infoboard_{bkl}_view", "")
            acl_overrides_safe = {k: v for k, v in acl_matrix.items()}
        elif acl_matrix and not acl_overrides_safe:
            logger.warning(
                "acl_matrix кабинеты %s не найдены в форме каталога (доступны: %s), ACL не применён",
                list(acl_matrix.keys()), sorted(form_board_keys)[:20],
            )

        logger.debug("_apply_acl acl_overrides_safe=%s (acl_matrix keys=%s form_board_keys=%s)", acl_overrides_safe, list(acl_matrix.keys()), sorted(form_board_keys))
        form_data = build_admin_dirs_form(
            full_form,
            acl_overrides=acl_overrides_safe,
            extra_board_ids=None,
        )
        # Не фильтровать пустые поля: отсутствие поля сервер может трактовать как сброс.
        encoded = urlencode(form_data)
        logger.debug(
            "POST %s fields_count=%s form_keys=%s",
            post_url, len(form_data), [k for k, _ in form_data],
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if base_url:
            headers["Referer"] = f"{base_url}/admin/dir"
            headers["Origin"] = base_url.rstrip("/")
        attempts = getattr(self.settings, "USERS_RETRY_ATTEMPTS", 3)
        base_delay = getattr(self.settings, "USERS_RETRY_BASE_DELAY", 0.2)
        max_delay = getattr(self.settings, "USERS_RETRY_MAX_DELAY", 3.0)
        for attempt in range(1, attempts + 1):
            try:
                resp = await self.http_client.post(
                    post_url,
                    content=encoded,
                    cookies=cookies,
                    headers=headers,
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
                if attempt < attempts:
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    logger.warning(
                        "admin/dirs 5xx status=%s attempt=%s/%s retry in %.2fs",
                        resp.status_code, attempt, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                body_snippet = (resp.text or "")[:500]
                req_len = len(encoded)
                logger.error(
                    "admin/dirs 5xx status=%s request_body_len=%s body_snippet=%s",
                    resp.status_code,
                    req_len,
                    body_snippet.replace("\n", " ").strip(),
                )
                dump_path = os.environ.get("DUMP_ADMIN_DIRS_ON_5XX")
                if dump_path:
                    try:
                        path = os.path.join(dump_path, "admin_dirs_5xx_request.txt")
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(encoded)
                        logger.error("admin/dirs request body saved to %s", path)
                    except OSError as e:
                        logger.warning("could not dump admin_dirs request: %s", e)
                raise NetworkError(
                    code="ADMIN_DIRS_5XX",
                    message=f"admin/dirs 5xx status={resp.status_code} (request_body_len={req_len})",
                )
            if resp.status_code >= 400:
                raise NetworkError(
                    code="ADMIN_DIRS_4XX",
                    message=f"admin/dirs status={resp.status_code}",
                    http_status=502,
                )
            break
