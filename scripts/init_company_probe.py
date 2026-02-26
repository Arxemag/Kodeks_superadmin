"""
Пробный прогон init_company без Kafka.

Вход — в том же формате, что и сообщения из Kafka: { event_id, event_type, payload }.
Либо «плоский» payload (legacy).

Пример (сообщение как из Kafka):
  python scripts/init_company_probe.py --message-json '{"event_id":"e1","event_type":"init_company","payload":{"id":67,"reg":"123456","companyName":"ООО Ромашка","departments":[]}}'
  python scripts/init_company_probe.py --message-file message.json

Legacy (плоский payload):
  python scripts/init_company_probe.py --payload-file payload.json
  python scripts/init_company_probe.py --payload-json '{"id":67,"reg":"123456","companyName":"ООО Ромашка","departments":[]}'

Требует: БД с reg_services и department_service_mapping, доступ к каталогу.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from common.config import get_settings
from common.http import _default_headers
from common.kafka import unwrap_payload
from common.logger import get_logger
from pydantic import ValidationError

from services.infoboards_service.dto import InitCompanyDTO, is_missing_department_id_error
from services.infoboards_service.init_company_service import InitCompanyService
from services.users_service.catalog_client import CatalogClient
from services.users_service.reg_resolver import RegResolver


logger = get_logger("init_company.probe")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Прогон init_company без Kafka. Вход — как сообщение из Kafka или flat payload."
    )
    parser.add_argument(
        "--message-json",
        default="",
        help='Сообщение из Kafka в JSON. В PowerShell лучше --message-file или --message-json=- (чтение из stdin).',
    )
    parser.add_argument(
        "--message-file",
        default="",
        help="Файл с сообщением из Kafka (тот же формат).",
    )
    parser.add_argument(
        "--payload-json",
        default="",
        help="Inline JSON payload (legacy).",
    )
    parser.add_argument(
        "--payload-file",
        default="",
        help="Путь к JSON-файлу с payload (legacy).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только синхронизация БД (без HTTP к каталогу).",
    )
    return parser.parse_args()


def _normalize_json_string(s: str) -> str:
    """Заменяет фигурные кавычки на обычные (для надёжного парсинга в PowerShell/терминале)."""
    for a, b in (
        ("\u201c", '"'),  # "
        ("\u201d", '"'),  # "
        ("\u201e", '"'),  # „
        ("\u201f", '"'),  # ‟
    ):
        s = s.replace(a, b)
    return s


def _load_json_from_inline_or_file(inline: str, file_path: str) -> dict[str, Any]:
    if inline:
        raw = sys.stdin.read() if inline == "-" else inline
        raw = _normalize_json_string(raw)
        return json.loads(raw)
    raw = Path(file_path).read_text(encoding="utf-8")
    return json.loads(_normalize_json_string(raw))


def _normalize_department_item(item: dict[str, Any]) -> dict[str, Any]:
    """Приводит элемент companyDepartments к виду { id, title, modules } (как в воркере)."""
    raw_id = item.get("id") or item.get("departmentId")
    raw_title = item.get("title") or item.get("name") or ""
    raw_modules = item.get("modules")
    if not isinstance(raw_modules, list):
        raw_modules = []
    return {
        "id": str(raw_id) if raw_id is not None else None,
        "title": str(raw_title),
        "modules": [str(m) for m in raw_modules],
    }


def _normalize_init_company_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Единая нормализация payload (reg/id fallback, companyDepartments → departments)."""
    if payload.get("reg") is None:
        for key in (
            "registration", "reg_number", "regNumber", "companyReg",
            "registrationNumber", "company_reg", "companyRegNumber", "reg_id",
        ):
            if payload.get(key) is not None:
                payload = {**payload, "reg": str(payload[key])}
                break
    if payload.get("id") is None:
        for key in ("companyId", "company_id"):
            if payload.get(key) is not None:
                payload = {**payload, "id": int(payload[key])}
                break
    if payload.get("departments") is None and payload.get("companyDepartments") is not None:
        raw = payload.get("companyDepartments")
        if isinstance(raw, list):
            normalized = [_normalize_department_item(d) for d in raw if isinstance(d, dict)]
            payload = {**payload, "departments": [x for x in normalized if x.get("id") is not None]}
    return payload


def _payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Возвращает payload для InitCompanyDTO: из сообщения Kafka (unwrap + data + reg fallback) или flat."""
    use_message = bool(args.message_json or args.message_file)
    use_payload = bool(args.payload_json or args.payload_file)

    if use_message and use_payload:
        raise ValueError("Укажите либо (--message-json|--message-file), либо (--payload-json|--payload-file)")
    if not use_message and not use_payload:
        raise ValueError("Укажите --message-json или --message-file, либо --payload-json или --payload-file")

    if use_message:
        if bool(args.message_json) == bool(args.message_file):
            raise ValueError("Ровно один из --message-json или --message-file")
        try:
            raw = _load_json_from_inline_or_file(args.message_json, args.message_file)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"Ошибка парсинга JSON: {e}. В PowerShell используйте --message-file путь.json или pipe: Get-Content msg.json | python scripts/init_company_probe.py --message-json=-"
            ) from e
        if not isinstance(raw, dict):
            raise ValueError("Сообщение должно быть JSON-объектом")
        payload = unwrap_payload(raw)
        if not isinstance(payload, dict):
            raise ValueError("После unwrap payload должен быть объектом")
        # Та же логика, что в воркере: обёртка payload.data
        if "data" in payload and isinstance(payload.get("data"), dict) and payload.get("reg") is None:
            payload = payload["data"]
        return _normalize_init_company_payload(payload)

    if bool(args.payload_json) == bool(args.payload_file):
        raise ValueError("Ровно один из --payload-json или --payload-file")
    try:
        payload = _load_json_from_inline_or_file(args.payload_json, args.payload_file)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"Ошибка парсинга JSON: {e}. В PowerShell используйте --payload-file путь.json или pipe с --payload-json=-"
        ) from e
    if not isinstance(payload, dict):
        raise ValueError("Payload должен быть JSON-объектом")
    return _normalize_init_company_payload(payload)


async def _run() -> None:
    args = _parse_args()
    payload_raw = _payload_from_args(args)
    logger.info("probe start reg=%r company=%r", payload_raw.get("reg"), payload_raw.get("companyName"))

    settings = get_settings()
    resolver = RegResolver()
    await resolver.startup()

    try:
        async with httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
            headers=_default_headers(),
        ) as http_client:
            catalog_client = CatalogClient(http_client=http_client)

            async with resolver.with_session() as session:
                try:
                    dto = InitCompanyDTO.model_validate(payload_raw)
                except ValidationError as e:
                    if is_missing_department_id_error(e):
                        logger.error("В отделе отсутствует id. Для каждого элемента departments укажите поле id (UUID).")
                    raise SystemExit(1) from e
                service = InitCompanyService(
                    db=session,
                    settings=settings,
                    http_client=http_client,
                    catalog_client=catalog_client,
                )
                await service.handle(dto, dry_run=args.dry_run)

    finally:
        await resolver.shutdown()

    logger.info("probe done successfully")
    print("OK")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
