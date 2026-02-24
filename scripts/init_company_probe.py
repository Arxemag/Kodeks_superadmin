"""
Пробный прогон init_company без Kafka.

Запуск:
  python scripts/init_company_probe.py --payload-file payload.json
  python scripts/init_company_probe.py --payload-json '{"id":67,"reg":"123456","companyName":"ООО Ромашка","departments":[{"id":"a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11","title":"Отдел продаж","modules":["Кабинет СМК"]}]}'

Требует: БД с reg_services и department_service_mapping, доступ к каталогу.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from common.config import get_settings
from common.http import _default_headers
from common.logger import get_logger
from services.infoboards_service.dto import InitCompanyDTO
from services.infoboards_service.init_company_service import InitCompanyService
from services.users_service.catalog_client import CatalogClient
from services.users_service.reg_resolver import RegResolver


logger = get_logger("init_company.probe")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Прогон init_company без Kafka.")
    parser.add_argument(
        "--payload-json",
        default="",
        help="Inline JSON payload.",
    )
    parser.add_argument(
        "--payload-file",
        default="",
        help="Путь к JSON-файлу с payload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только синхронизация БД (без HTTP к каталогу).",
    )
    return parser.parse_args()


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.payload_json) == bool(args.payload_file):
        raise ValueError("Укажите ровно один из --payload-json или --payload-file")

    if args.payload_json:
        payload = json.loads(args.payload_json)
    else:
        raw = Path(args.payload_file).read_text(encoding="utf-8")
        payload = json.loads(raw)

    if not isinstance(payload, dict):
        raise ValueError("Payload должен быть JSON-объектом")
    return payload


async def _run() -> None:
    args = _parse_args()
    payload_raw = _load_payload(args)
    logger.info(f"probe start reg={payload_raw.get('reg')!r} company={payload_raw.get('companyName')!r}")

    settings = get_settings()
    resolver = RegResolver(
        db_url=settings.DB_URL,
        pool_size=settings.POOL_SIZE,
        pool_timeout=settings.POOL_TIMEOUT,
    )
    await resolver.startup()

    try:
        async with httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
            headers=_default_headers(),
        ) as http_client:
            catalog_client = CatalogClient(http_client=http_client)

            async with resolver.with_session() as session:
                dto = InitCompanyDTO.model_validate(payload_raw)
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
