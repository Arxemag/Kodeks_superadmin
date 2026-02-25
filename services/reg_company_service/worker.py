"""
Kafka worker для топиков enable_reg_company и disable_reg_company.

ПОКА: только приём сообщения, валидация payload и запись в лог «принял, всё ок».
Реализацию (запрос к другому API) добавим позже.
"""
from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from aiokafka import TopicPartition
from aiokafka.structs import OffsetAndMetadata
from pydantic import ValidationError
from prometheus_client import start_http_server

from common.config import get_settings
from common.kafka import create_consumer, unwrap_payload
from common.logger import get_logger, set_trace_id
from services.reg_company_service.dto import DisableRegCompanyDTO, EnableRegCompanyDTO


logger = get_logger("reg_company.worker")


async def run_worker() -> None:
    """Точка входа: метрики, consumer, цикл — приём и лог."""
    settings = get_settings()
    start_http_server(settings.REG_COMPANY_METRICS_PORT)
    logger.info(
        "reg_company worker startup topics=%s group=%s",
        [settings.KAFKA_ENABLE_REG_COMPANY_TOPIC, settings.KAFKA_DISABLE_REG_COMPANY_TOPIC],
        settings.KAFKA_REG_COMPANY_GROUP_ID,
    )

    stop_event = asyncio.Event()

    def _stop(*_: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    consumer = create_consumer(
        settings.KAFKA_ENABLE_REG_COMPANY_TOPIC,
        settings.KAFKA_DISABLE_REG_COMPANY_TOPIC,
        group_id=settings.KAFKA_REG_COMPANY_GROUP_ID,
        settings=settings,
        max_poll_records=min(50, settings.KAFKA_MAX_BATCH),
        request_timeout_ms=60_000,
        session_timeout_ms=30_000,
    )

    await consumer.start()
    try:
        while not stop_event.is_set():
            batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
            if not batch:
                continue
            for _partition, records in batch.items():
                for record in records:
                    _process_one(record, settings)
                    tp = TopicPartition(record.topic, record.partition)
                    await consumer.commit({tp: OffsetAndMetadata(record.offset + 1, "")})
    finally:
        await consumer.stop()


def _process_one(record: Any, settings: Any) -> None:
    """Валидация payload и лог «принял, всё ок». Реализацию добавим позже."""
    topic = getattr(record, "topic", "")
    offset = getattr(record, "offset", -1)
    raw = record.value if hasattr(record, "value") else None
    payload = unwrap_payload(raw) if raw is not None else None

    set_trace_id(f"reg_company-{topic}-{offset}")

    if not isinstance(payload, dict):
        logger.warning("topic=%s offset=%s payload не объект, пропуск", topic, offset)
        return

    try:
        if topic == settings.KAFKA_ENABLE_REG_COMPANY_TOPIC:
            dto = EnableRegCompanyDTO.model_validate(payload)
            logger.info(
                "Принял enable_reg_company: reg=%r oldReg=%r companyName=%r — всё ок (реализация позже)",
                dto.reg, dto.oldReg, dto.companyName,
            )
        elif topic == settings.KAFKA_DISABLE_REG_COMPANY_TOPIC:
            dto = DisableRegCompanyDTO.model_validate(payload)
            logger.info(
                "Принял disable_reg_company: reg=%r companyName=%r — всё ок (реализация позже)",
                dto.reg, dto.companyName,
            )
        else:
            logger.warning("topic=%s offset=%s неизвестный топик", topic, offset)
    except ValidationError as e:
        logger.warning("topic=%s offset=%s невалидный payload: %s", topic, offset, e)
