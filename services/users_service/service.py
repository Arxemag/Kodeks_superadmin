"""
Бизнес-логика обработки пользователей каталога: создание, обновление, обновление отделов.

Инициализация: UserService создаётся в worker с auth_client, catalog_client и reg_resolver.
Обработка: handle_create — при наличии пользователя переходит в update; handle_update — partial update
по пришедшим полям, grp при наличии перезаписывается; handle_update_departments — группы каталога,
создание отсутствующих, замена групп пользователя и проверка применения.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from common.exceptions import AuthError, NetworkError, ParseError
from common.logger import get_logger
from common.group_ownership import GroupOwnershipRepository
from common.department_groups import DepartmentGroupRepository
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.dto import CreateUserDTO, UpdateUserDTO, UpdateUserDepartmentsDTO, redact_payload
from services.users_service.html_parser import (
    UserState,
    parse_groups_catalog,
    parse_user_state,
    normalize_title,
    group_index,
)
from services.users_service.metrics import users_created_total, users_updated_total
from services.users_service.reg_resolver import RegResolver


logger = get_logger("users.service")

# Поля формы каталога, которые можно обновлять частично (кроме uid, psw, id, grp)
_OPTIONAL_FIELDS = ("name", "org", "pos", "mail", "telephon", "end")


def _serialized(method):
    @wraps(method)
    async def wrapped(self, payload):
        base_url = await self.reg_resolver.resolve_base_url(payload.reg)
        async with self.catalog_client.operation(base_url):
            return await method(self, payload)
    return wrapped


def overlay_form(form_data, replacements):
    """Replace named controls, preserving all other values and repeated keys."""
    result = [(name, value) for name, value in form_data if name not in replacements]
    for name, values in replacements.items():
        result.extend((name, value) for value in values)
    return result


@dataclass
class UserService:
    """Сервис операций с пользователями: получение куков через auth_client, запросы к каталогу через catalog_client, base_url через reg_resolver."""

    auth_client: AuthClient
    catalog_client: CatalogClient
    reg_resolver: RegResolver
    group_ownership: GroupOwnershipRepository = field(default_factory=GroupOwnershipRepository)
    department_groups: DepartmentGroupRepository = field(default_factory=DepartmentGroupRepository)

    @_serialized
    async def handle_create(self, payload: CreateUserDTO) -> None:
        """Создание пользователя: base_url и куки, проверка существования по get_user_page; при наличии — переход в _update_from_state, иначе POST с обязательными и опциональными полями."""
        logger.debug(f"handle_create payload={redact_payload(payload.model_dump(mode='python'))}")
        base_url = await self.reg_resolver.resolve_base_url(payload.reg)
        logger.debug(f"resolved base_url for create reg={payload.reg!r}: {base_url!r}")
        cookies = await self.auth_client.get_admin_cookies(payload.reg)

        page = await self.catalog_client.get_user_page(base_url, payload.uid, cookies)
        absent = page is None
        if absent:
            page = await self.catalog_client.get_user_create_page(base_url, cookies)
        existing = parse_user_state(page)
        if existing.is_empty_form and not absent:
            raise ParseError(code="USER_PRESENCE_UNCERTAIN", message="A blank response does not confirm user absence")
        if not existing.is_empty_form:
            self._check_identity(existing, payload.uid)
            logger.info(f"user exists, switching create->update uid={payload.uid!r} reg={payload.reg!r}")
            await self._update_from_state(
                reg=payload.reg,
                incoming=payload.model_dump(mode="python"),
                fields_set=payload.model_fields_set,
                existing=existing,
                fallback_id=payload.uid,
                base_url=base_url,
                cookies=cookies,
            )
            return

        replacements = {"uid": [payload.uid], "psw": [payload.psw]}
        payload_dict = payload.model_dump(mode="python")
        for key in _OPTIONAL_FIELDS:
            if key in payload.model_fields_set:
                replacements[key] = [_to_form_value(payload_dict.get(key))]
        if "grp" in payload.model_fields_set and payload.grp is not None:
            replacements["grp"] = [str(grp) for grp in payload.grp]
        form_data = overlay_form(existing.form_data, replacements)

        logger.debug(f"create post prepared uid={payload.uid!r} fields={_safe_form_data(form_data)}")
        await self.catalog_client.post_user(base_url, form_data, cookies, form_action=existing.action, form_method=existing.method)
        await self._verify_user(base_url, payload.uid, cookies, replacements)
        if "grp" in replacements:
            try:
                await self.group_ownership.set(payload.reg, payload.uid, set(payload.grp or []) - set(existing.groups))
            except NetworkError as error:
                raise ParseError(code="GROUP_OWNERSHIP_PERSIST_UNCERTAIN", message="User creation was verified but group ownership could not be recorded; reconcile before retrying") from error
        logger.debug(f"create completed uid={payload.uid!r}")
        users_created_total.inc()

    @_serialized
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
        self._check_identity(existing, payload.uid)
        await self._update_from_state(
            reg=payload.reg,
            incoming=payload.model_dump(mode="python"),
            fields_set=payload.model_fields_set,
            existing=existing,
            fallback_id=payload.id or payload.uid,
            base_url=base_url,
            cookies=cookies,
        )

    @_serialized
    async def handle_update_departments(self, payload: UpdateUserDepartmentsDTO) -> None:
        """Обновление отделов пользователя: разбор групп каталога, создание отсутствующих, сопоставление title -> id, замена групп пользователя и проверка после сохранения."""
        logger.debug(f"handle_update_departments payload={redact_payload(payload.model_dump(mode='python'))}")
        base_url = await self.reg_resolver.resolve_base_url(payload.reg)
        logger.debug(f"resolved base_url for update_departments reg={payload.reg!r}: {base_url!r}")
        cookies = await self.auth_client.get_admin_cookies(payload.reg)

        groups_page = await self.catalog_client.get_groups_page(base_url, cookies)
        groups_map = parse_groups_catalog(groups_page)
        logger.debug(f"parsed catalog groups count={len(groups_map)} titles_sample={list(groups_map.keys())[:8]!r}")

        target_group_ids = sorted(await self.department_groups.resolve(payload.reg, [dep.id for dep in payload.departments]))
        if not set(target_group_ids) <= set(groups_map.values()):
            raise NetworkError(code="DEPARTMENT_GROUP_NOT_READY", message="Company synchronisation must restore missing catalog groups")

        user_page = await self.catalog_client.get_user_page(base_url, payload.id, cookies)
        if user_page is None:
            raise ParseError(code="USER_NOT_FOUND", message=f"user '{payload.id}' not found for department update")

        existing = parse_user_state(user_page)
        self._check_identity(existing, payload.id, allow_internal_id=True)

        logger.debug(
            f"department update uid={payload.id!r} old_groups={sorted(set(existing.groups))} "
            f"new_groups={sorted(set(target_group_ids))}"
        )
        await self._update_from_state(
            reg=payload.reg,
            incoming={
                "uid": existing.fields.get("uid", payload.id),
                "grp": target_group_ids,
            },
            fields_set={"uid", "grp"},
            existing=existing,
            fallback_id=payload.id,
            base_url=base_url,
            cookies=cookies,
        )


    async def _update_from_state(
        self,
        *,
        reg: str,
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
        if "psw" in fields_set:
            merged["psw"] = _to_form_value(incoming["psw"])

        for key in _OPTIONAL_FIELDS:
            if key in fields_set:
                merged[key] = _to_form_value(incoming.get(key))

        # The catalog's internal ID belongs to the parsed form, not the platform.
        id_value = existing.fields.get("id", "")

        if "grp" in fields_set:
            desired = set(incoming.get("grp") or [])
            current = set(existing.groups)
            owned, owned_elsewhere = await self.group_ownership.get_with_shared(
                reg, existing.fields["uid"], base_url, desired=desired, current=current,
            )
            grp_final = sorted((current - (owned - owned_elsewhere)) | desired)
            next_owned = (owned & desired) | (desired - current) | (desired & owned_elsewhere)
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
        for key in _OPTIONAL_FIELDS:
            if key in fields_set and merged.get(key, "") != existing.fields.get(key, ""):
                changed = True
        if "grp" in fields_set and grp_final != sorted(set(existing.groups)):
            changed = True

        if not changed:
            if "grp" in fields_set:
                await self.group_ownership.set(reg, existing.fields["uid"], next_owned)
            logger.info(f"skip update (no changes) uid={merged.get('uid', '')!r}")
            return

        replacements = {"uid": [merged["uid"]]}
        for key in ("psw", *_OPTIONAL_FIELDS):
            if key in fields_set:
                replacements[key] = [merged.get(key, "")]
        if "grp" in fields_set:
            replacements["grp"] = [str(grp) for grp in grp_final]
        form_data = overlay_form(existing.form_data, replacements)
        logger.debug(
            f"update post prepared uid={merged.get('uid', '')!r} id={id_value!r} fields={_safe_form_data(form_data)}"
        )
        await self.catalog_client.post_user(base_url, form_data, cookies, form_action=existing.action, form_method=existing.method)
        await self._verify_user(base_url, merged["uid"], cookies, replacements)
        if "grp" in fields_set:
            try:
                await self.group_ownership.set(reg, merged["uid"], next_owned)
            except NetworkError as error:
                raise ParseError(code="GROUP_OWNERSHIP_PERSIST_UNCERTAIN", message="User write was verified but group ownership could not be recorded; reconcile before retrying") from error
        logger.debug(f"update completed uid={merged.get('uid', '')!r}")
        users_updated_total.inc()

    @staticmethod
    def _check_identity(state: UserState, uid: str, *, allow_internal_id=False) -> None:
        matches = state.fields.get("uid") == uid or (allow_internal_id and state.fields.get("id") == uid)
        if state.is_empty_form or not matches:
            raise ParseError(code="USER_IDENTITY_MISMATCH", message="Catalog returned a different or blank user form")

    async def _verify_user(self, base_url, uid, cookies, expected) -> None:
        try:
            page = await self.catalog_client.get_user_page(base_url, uid, cookies)
        except (AuthError, NetworkError) as error:
            raise ParseError(code="USER_WRITE_UNCERTAIN", message="User write could not be verified") from error
        if page is None:
            raise ParseError(code="USER_WRITE_NOT_CONFIRMED", message="User is absent after saving")
        state = parse_user_state(page)
        self._check_identity(state, uid)
        for key, values in expected.items():
            if key == "grp":
                matches = sorted(state.groups) == sorted(int(value) for value in values)
            else:
                matches = state.fields.get(key) == (values[0] if values else "")
            if not matches:
                raise ParseError(code="USER_WRITE_NOT_CONFIRMED", message=f"Catalog did not apply field {key!r}")


def _to_form_value(value: Any) -> str:
    """Преобразует значение поля в строку для формы; None -> пустая строка."""
    if value is None:
        return ""
    return str(value)


def _normalize_title(value: str) -> str:
    """Нормализация названия для сравнения: пробелы схлопнуты, нижний регистр."""
    return normalize_title(value)


def _safe_form_data(form_data: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Копия form_data с замаскированными значениями psw и mail для логов."""
    safe: list[tuple[str, str]] = []
    for key, value in form_data:
        if key in {"psw", "mail"}:
            safe.append((key, "***"))
        else:
            safe.append((key, value))
    return safe
