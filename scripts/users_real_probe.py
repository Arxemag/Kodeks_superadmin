"""
Прогон обработки users (create-user, update-user, update-user-departments) без Kafka.

Вход — в том же формате, что и сообщения из Kafka: { event_id, event_type, payload }.
Либо «плоский» payload + --topic (legacy).

Пример (сообщение как из Kafka):
  python scripts/users_real_probe.py --message-json '{"event_id":"e1","event_type":"create-user","payload":{"reg":"350832","uid":"user1","psw":"secret"}}'
  python scripts/users_real_probe.py --message-file message.json

Legacy (плоский payload + явный топик):
  python scripts/users_real_probe.py --topic create-user --payload-json '{"reg":"350832","uid":"user1","psw":"secret"}'
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
from common.kafka import event_type_from_message, unwrap_payload
from common.logger import get_logger
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.dto import (
    CreateUserDTO,
    UpdateUserDTO,
    UpdateUserDepartmentsDTO,
    redact_payload,
)
from services.users_service.reg_resolver import RegResolver
from services.users_service.service import UserService


logger = get_logger("users.real_probe")

USER_TOPICS = ("create-user", "update-user", "update-user-departments")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Прогон users (create/update/update_departments) без Kafka. Вход — как сообщение из Kafka или flat payload."
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
        "--topic",
        choices=list(USER_TOPICS),
        help="Топик (только для режима --payload-*).",
    )
    parser.add_argument(
        "--payload-json",
        default="",
        help="Flat payload (legacy). Требуется --topic.",
    )
    parser.add_argument(
        "--payload-file",
        default="",
        help="Файл с flat payload (legacy). Требуется --topic.",
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


def _get_message_and_topic(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Возвращает (payload для сервиса, event_type/topic)."""
    use_message = bool(args.message_json or args.message_file)
    use_payload = bool(args.payload_json or args.payload_file)

    if use_message and use_payload:
        raise ValueError("Укажите либо (--message-json|--message-file), либо (--topic + --payload-json|--payload-file)")
    if not use_message and not use_payload:
        raise ValueError("Укажите --message-json или --message-file, либо --topic и --payload-json или --payload-file")

    if use_message:
        if bool(args.message_json) == bool(args.message_file):
            raise ValueError("Ровно один из --message-json или --message-file")
        try:
            msg = _load_json_from_inline_or_file(args.message_json, args.message_file)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"Ошибка парсинга JSON: {e}. В PowerShell используйте --message-file путь.json или pipe: Get-Content msg.json | python scripts/users_real_probe.py --message-json=-"
            ) from e
        if not isinstance(msg, dict):
            raise ValueError("Сообщение должно быть JSON-объектом")
        event_type = event_type_from_message(msg)
        if event_type not in USER_TOPICS:
            raise ValueError(f"event_type должен быть один из {USER_TOPICS}, получено {event_type!r}")
        payload = unwrap_payload(msg)
        if not isinstance(payload, dict):
            raise ValueError("После unwrap payload должен быть объектом")
        return payload, event_type

    if not args.topic:
        raise ValueError("Для --payload-* укажите --topic")
    if bool(args.payload_json) == bool(args.payload_file):
        raise ValueError("Ровно один из --payload-json или --payload-file")
    payload = _load_json_from_inline_or_file(args.payload_json, args.payload_file)
    if not isinstance(payload, dict):
        raise ValueError("Payload должен быть JSON-объектом")
    return payload, args.topic


async def _run() -> None:
    args = _parse_args()
    payload_raw, topic = _get_message_and_topic(args)
    logger.info("probe start topic=%s payload=%s", topic, redact_payload(payload_raw))

    settings = get_settings()
    resolver = RegResolver()
    await resolver.startup()

    try:
        async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True) as http_client:
            service = UserService(
                auth_client=AuthClient(http_client=http_client, auth_service_url=settings.AUTH_SERVICE_URL),
                catalog_client=CatalogClient(http_client=http_client),
                reg_resolver=resolver,
            )

            if topic == "create-user":
                dto = CreateUserDTO.model_validate(payload_raw)
                await service.handle_create(dto)
            elif topic == "update-user":
                dto = UpdateUserDTO.model_validate(payload_raw)
                await service.handle_update(dto)
            else:
                dto = UpdateUserDepartmentsDTO.model_validate(payload_raw)
                await service.handle_update_departments(dto)

    finally:
        await resolver.shutdown()

    logger.info("probe done successfully")
    print("OK")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
