"""
Бизнес-логика обработки пользователей каталога: создание, обновление, обновление отделов.

Инициализация: UserService создаётся в worker с auth_client, catalog_client и reg_resolver.
Обработка: handle_create — при наличии пользователя переходит в update; handle_update — partial update
по пришедшим полям, grp при наличии перезаписывается; handle_update_departments — группы каталога,
создание отсутствующих, замена групп пользователя и проверка применения.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from common.exceptions import ParseError
from common.logger import get_logger
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.dto import CreateUserDTO, UpdateUserDTO, UpdateUserDepartmentsDTO, redact_payload
from services.users_service.html_parser import (
    UserState,
    parse_groups_catalog,
    parse_user_state,
)
from services.users_service.metrics import users_created_total, users_updated_total
from services.users_service.reg_resolver import RegResolver


logger = get_logger("users.service")

# Поля формы каталога, которые можно обновлять частично (кроме uid, psw, id, grp)
_OPTIONAL_FIELDS = ("name", "org", "pos", "mail", "telephon", "end")


@dataclass
class UserService:
    """Сервис операций с пользователями: получение куков через auth_client, запросы к каталогу через catalog_client, base_url через reg_resolver."""

    auth_client: AuthClient
    catalog_client: CatalogClient
    reg_resolver: RegResolver

    async def handle_create(self, payload: CreateUserDTO) -> None:
        """Создание пользователя: base_url и куки, проверка существования по get_user_page; при наличии — переход в _update_from_state, иначе POST с обязательными и опциональными полями."""
        logger.debug(f"handle_create payload={redact_payload(payload.model_dump(mode='python'))}")
        base_url = await self.reg_resolver.resolve_base_url(payload.reg)
        logger.debug(f"resolved base_url for create reg={payload.reg!r}: {base_url!r}")
        cookies = await self.auth_client.get_admin_cookies(payload.reg)

        page = await self.catalog_client.get_user_page(base_url, payload.uid, cookies)
        if page is not None:
            logger.info(f"user exists, switching create->update uid={payload.uid!r} reg={payload.reg!r}")
            existing = parse_user_state(page)
            await self._update_from_state(
                incoming=payload.model_dump(mode="python"),
                fields_set=payload.model_fields_set,
                existing=existing,
                fallback_id=payload.uid,
                base_url=base_url,
                cookies=cookies,
            )
            return

        form_data: list[tuple[str, str]] = [("uid", payload.uid), ("psw", payload.psw), ("set", "")]
        payload_dict = payload.model_dump(mode="python")
        for key in _OPTIONAL_FIELDS:
            if key in payload.model_fields_set:
                form_data.append((key, _to_form_value(payload_dict.get(key))))
        if "grp" in payload.model_fields_set and payload.grp is not None:
            for grp in payload.grp:
                form_data.append(("grp", str(grp)))

        logger.debug(f"create post prepared uid={payload.uid!r} fields={_safe_form_data(form_data)}")
        await self.catalog_client.post_user(base_url, form_data, cookies)
        logger.debug(f"create completed uid={payload.uid!r}")
        users_created_total.inc()

    async def handle_update(self, payload: UpdateUserDTO) -> None:
        """Обновление пользователя: загрузка текущего состояния, слияние с входящими полями, POST только при наличии изменений."""
        logger.debug(f"handle_update payload={redact_payload(payload.model_dump(mode='python'))}")
        base_url = await self.reg_resolver.resolve_base_url(payload.reg)
        logger.debug(f"resolved base_url for update reg={payload.reg!r}: {base_url!r}")
        cookies = await self.auth_client.get_admin_cookies(payload.reg)

        page = await self.catalog_client.get_user_page(base_url, payload.uid, cookies)
        if page is None:
            raise ParseError(code="USER_NOT_FOUND", message=f"user '{payload.uid}' not found for update")

        existing = parse_user_state(page)
        await self._update_from_state(
            incoming=payload.model_dump(mode="python"),
            fields_set=payload.model_fields_set,
            existing=existing,
            fallback_id=payload.id or payload.uid,
            base_url=base_url,
            cookies=cookies,
        )

    async def handle_update_departments(self, payload: UpdateUserDepartmentsDTO) -> None:
        """Обновление отделов пользователя: разбор групп каталога, создание отсутствующих, сопоставление title -> id, замена групп пользователя и проверка после сохранения."""
        logger.debug(f"handle_update_departments payload={redact_payload(payload.model_dump(mode='python'))}")
        base_url = await self.reg_resolver.resolve_base_url(payload.reg)
        logger.debug(f"resolved base_url for update_departments reg={payload.reg!r}: {base_url!r}")
        cookies = await self.auth_client.get_admin_cookies(payload.reg)

        groups_page = await self.catalog_client.get_groups_page(base_url, cookies)
        groups_map = parse_groups_catalog(groups_page)
        logger.debug(f"parsed catalog groups count={len(groups_map)} titles_sample={list(groups_map.keys())[:8]!r}")

        desired_titles: list[str] = []
        seen_titles: set[str] = set()
        for dep in payload.departments:
            title = dep.title.strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            desired_titles.append(title)

        normalized_groups_map = {_normalize_title(k): v for k, v in groups_map.items()}
        target_group_ids: list[int] = []
        for title in desired_titles:
            grp_id = normalized_groups_map.get(_normalize_title(title))
            if grp_id is None:
                logger.debug(f"group missing, creating title={title!r}")
                await self.catalog_client.create_group(base_url, title, cookies)
                grp_id = await self._find_group_id_with_refresh(
                    base_url=base_url,
                    cookies=cookies,
                    title=title,
                )
                if grp_id is None:
                    raise ParseError(code="GROUP_NOT_FOUND_AFTER_CREATE", message=f"group {title!r} was not found")
            target_group_ids.append(grp_id)

        user_page = await self.catalog_client.get_user_page(base_url, payload.id, cookies)
        if user_page is None:
            raise ParseError(code="USER_NOT_FOUND", message=f"user '{payload.id}' not found for department update")

        existing = parse_user_state(user_page)

        if sorted(set(existing.groups)) == sorted(set(target_group_ids)):
            logger.info(f"skip department update (no changes) uid={payload.id!r}")
            return

        logger.debug(
            f"department update uid={payload.id!r} old_groups={sorted(set(existing.groups))} "
            f"new_groups={sorted(set(target_group_ids))}"
        )
        await self._update_from_state(
            incoming={
                "uid": existing.fields.get("uid", payload.id),
                "psw": existing.fields.get("psw", ""),
                "id": payload.id,
                "grp": target_group_ids,
            },
            fields_set={"uid", "psw", "id", "grp"},
            existing=existing,
            fallback_id=payload.id,
            base_url=base_url,
            cookies=cookies,
        )

        verify_page = await self.catalog_client.get_user_page(base_url, payload.id, cookies)
        if verify_page is None:
            raise ParseError(code="USER_NOT_FOUND", message=f"user '{payload.id}' not found after update")
        verify_state = parse_user_state(verify_page)
        if sorted(set(verify_state.groups)) != sorted(set(target_group_ids)):
            raise ParseError(
                code="GROUP_ASSIGNMENT_NOT_APPLIED",
                message=(
                    f"groups were not applied uid={payload.id!r} "
                    f"expected={sorted(set(target_group_ids))} actual={sorted(set(verify_state.groups))}"
                ),
            )
        users_updated_total.inc()

    async def _find_group_id_with_refresh(
        self,
        *,
        base_url: str,
        cookies: dict[str, str],
        title: str,
    ) -> int | None:
        """После создания группы повторно запрашивает страницу групп до 5 раз с паузой; возвращает id по нормализованному title или None."""
        normalized_target = _normalize_title(title)
        for attempt in range(5):
            page = await self.catalog_client.get_groups_page(base_url, cookies)
            groups_map = parse_groups_catalog(page)
            normalized_map = {_normalize_title(k): v for k, v in groups_map.items()}
            grp_id = normalized_map.get(normalized_target)
            if grp_id is not None:
                return grp_id
            delay = 0.2 * (attempt + 1)
            logger.debug(
                f"group still missing after create title={title!r} attempt={attempt + 1}/5 "
                f"sleep={delay:.1f}s groups_sample={list(groups_map.keys())[:8]!r}"
            )
            await asyncio.sleep(delay)
        return None

    async def _update_from_state(
        self,
        *,
        incoming: dict[str, Any],
        fields_set: set[str],
        existing: UserState,
        fallback_id: str,
        base_url: str,
        cookies: dict[str, str],
    ) -> None:
        """Сливает входящие поля с текущим состоянием, определяет id и grp; при отсутствии изменений выходит без POST; иначе формирует form_data и post_user."""
        merged: dict[str, str] = dict(existing.fields)

        merged["uid"] = _to_form_value(incoming["uid"])
        merged["psw"] = _to_form_value(incoming["psw"])

        for key in _OPTIONAL_FIELDS:
            if key in fields_set:
                merged[key] = _to_form_value(incoming.get(key))

        id_value = _to_form_value(incoming.get("id")) if "id" in fields_set else fallback_id
        if not id_value:
            id_value = fallback_id

        if "grp" in fields_set:
            grp_final = sorted(set(incoming.get("grp") or []))
        else:
            grp_final = sorted(set(existing.groups))

        logger.debug(
            f"update diff start uid={incoming.get('uid')!r} fields_set={sorted(fields_set)} "
            f"existing_groups={sorted(set(existing.groups))} incoming_groups={sorted(set(incoming.get('grp') or []))}"
        )

        changed = False
        if merged.get("uid", "") != existing.fields.get("uid", ""):
            changed = True
        if merged.get("psw", "") != existing.fields.get("psw", ""):
            changed = True
        if id_value != existing.fields.get("id", fallback_id):
            changed = True
        for key in _OPTIONAL_FIELDS:
            if key in fields_set and merged.get(key, "") != existing.fields.get(key, ""):
                changed = True
        if "grp" in fields_set and grp_final != sorted(set(existing.groups)):
            changed = True

        if not changed:
            logger.info(f"skip update (no changes) uid={merged.get('uid', '')!r}")
            return

        form_data: list[tuple[str, str]] = [
            ("uid", merged.get("uid", "")),
            ("psw", merged.get("psw", "")),
            ("id", id_value),
        ]

        # Форма каталога legacy: ожидает все текстовые поля при каждом сохранении.
        # Семантика partial update сохранена: мы сначала мержим входящие поля в текущее состояние.
        for key in _OPTIONAL_FIELDS:
            form_data.append((key, merged.get(key, "")))

        # Текущие группы сохраняем, если grp в payload не передан.
        for grp in grp_final:
            form_data.append(("grp", str(grp)))

        form_data.append(("set", ""))
        logger.debug(
            f"update post prepared uid={merged.get('uid', '')!r} id={id_value!r} fields={_safe_form_data(form_data)}"
        )
        await self.catalog_client.post_user(base_url, form_data, cookies)
        logger.debug(f"update completed uid={merged.get('uid', '')!r}")
        users_updated_total.inc()


def _to_form_value(value: Any) -> str:
    """Преобразует значение поля в строку для формы; None -> пустая строка."""
    if value is None:
        return ""
    return str(value)


def _normalize_title(value: str) -> str:
    """Нормализация названия для сравнения: пробелы схлопнуты, нижний регистр."""
    return " ".join(value.strip().casefold().split())


def _safe_form_data(form_data: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Копия form_data с замаскированными значениями psw и mail для логов."""
    safe: list[tuple[str, str]] = []
    for key, value in form_data:
        if key in {"psw", "mail"}:
            safe.append((key, "***"))
        else:
            safe.append((key, value))
    return safe
