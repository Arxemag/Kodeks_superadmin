from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from common.config import get_settings
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a real users create/update flow without Kafka.")
    parser.add_argument(
        "--topic",
        required=True,
        choices=["create-user", "update-user", "update-user-departments"],
        help="Operation topic to simulate.",
    )
    parser.add_argument(
        "--payload-json",
        default="",
        help="Inline JSON payload. Example: '{\"reg\":\"350832\",\"uid\":\"Tetpo\",\"psw\":\"123\"}'",
    )
    parser.add_argument(
        "--payload-file",
        default="",
        help="Path to JSON payload file.",
    )
    return parser.parse_args()


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.payload_json) == bool(args.payload_file):
        raise ValueError("Provide exactly one of --payload-json or --payload-file")

    if args.payload_json:
        payload = json.loads(args.payload_json)
    else:
        raw = Path(args.payload_file).read_text(encoding="utf-8")
        payload = json.loads(raw)

    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")
    return payload


async def _run() -> None:
    args = _parse_args()
    payload_raw = _load_payload(args)
    logger.info(f"probe start topic={args.topic} payload={redact_payload(payload_raw)}")

    settings = get_settings()
    resolver = RegResolver(
        db_url=settings.DB_URL,
        pool_size=settings.POOL_SIZE,
        pool_timeout=settings.POOL_TIMEOUT,
    )
    await resolver.startup()

    try:
        async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True) as http_client:
            service = UserService(
                auth_client=AuthClient(http_client=http_client, auth_service_url=settings.AUTH_SERVICE_URL),
                catalog_client=CatalogClient(http_client=http_client),
                reg_resolver=resolver,
            )

            if args.topic == "create-user":
                dto = CreateUserDTO.model_validate(payload_raw)
                await service.handle_create(dto)
            elif args.topic == "update-user":
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
