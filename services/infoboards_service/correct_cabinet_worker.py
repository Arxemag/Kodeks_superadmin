"""
Kafka worker для топика correct_cabinet (уточнение кабинета по опроснику).

Поток: payload (userId, reg, link, questions[]) -> base_url по reg (reg_services) + админ-cookies
(AuthService) -> correct_cabinet(): в под-кабинетах родителя link удалить виджеты невыбранных
вариантов (по справочнику) -> ответ в топик cabinet-corrected (userId, reg, oldLink, link).

Ошибки валидации / REG_NOT_FOUND -> DLQ без retry. NetworkError/прочие AuthError -> retry с backoff, затем DLQ.
Используется единым воркером (unified_worker) или как отдельный процесс (run_worker).
"""
from __future__ import annotations

import asyncio
import random
import signal
from datetime import datetime, timezone
from typing import Any

import httpx
from aiokafka import AIOKafkaProducer
from aiokafka.structs import ConsumerRecord, OffsetAndMetadata
from pydantic import ValidationError

from common.config import get_settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.http import _default_headers
from common.kafka import create_consumer, create_producer, unwrap_payload
from common.logger import get_logger, set_trace_id
from services.auth_service.service import AuthService
from services.infoboards_service.cabinet_edit import correct_cabinet, load_questionnaire_map
from services.infoboards_service.dto import CorrectCabinetDTO
from services.users_service.reg_resolver import RegResolver


logger = get_logger("correct_cabinet.worker")


def _backoff(attempt: int, settings: Any) -> float:
    exp = min(settings.USERS_RETRY_MAX_DELAY, settings.USERS_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


async def _send_dlq(producer: AIOKafkaProducer, topic: str, payload: Any, code: str, message: str) -> None:
    await producer.send_and_wait(topic, {
        "original_message": payload,
        "error_code": code,
        "error_message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    logger.warning("correct_cabinet -> DLQ code=%s msg=%s", code, message)


async def _handle_with_retries(
    record: ConsumerRecord,
    resolver: RegResolver,
    http_client: httpx.AsyncClient,
    producer: AIOKafkaProducer,
    settings: Any,
) -> None:
    """Валидация, обработка correct_cabinet, ответ в cabinet-corrected; retry/DLQ при ошибках."""
    dlq = settings.KAFKA_CORRECT_CABINET_DLQ_TOPIC
    payload = unwrap_payload(record.value)
    if not isinstance(payload, dict):
        await _send_dlq(producer, dlq, payload, "INVALID_PAYLOAD", "Payload is not an object")
        return

    set_trace_id(f"correct_cabinet-{record.topic}-{record.partition}-{record.offset}")
    try:
        dto = CorrectCabinetDTO.model_validate(payload)
    except ValidationError as e:
        await _send_dlq(producer, dlq, payload, "VALIDATION_ERROR", str(e))
        return

    logger.info(
        "correct_cabinet reg=%r userId=%r link=%r questions=%s",
        dto.reg, dto.userId, dto.link, len(dto.questions),
    )

    attempts = settings.USERS_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            base_url = await resolver.resolve_base_url(dto.reg)
            async with resolver.with_session() as session:
                cookies = await AuthService(db=session, settings=settings).login(reg=dto.reg, name=None)
            dry_run = bool(getattr(settings, "CORRECT_CABINET_DRY_RUN", False))
            plans = await correct_cabinet(
                base_url, http_client, cookies, dto.link,
                [q.model_dump() for q in dto.questions],
                dry_run=dry_run,
            )
            if dry_run:
                logger.warning("CORRECT_CABINET_DRY_RUN=true — виджеты НЕ удалялись (только план)")
            await producer.send_and_wait(settings.KAFKA_CABINET_CORRECTED_TOPIC, {
                "userId": dto.userId,
                "reg": dto.reg,
                "oldLink": dto.link,
                "link": dto.link,
            })
            logger.info(
                "correct_cabinet done reg=%r plans=%s",
                dto.reg, [(p.action, p.sub_id, len(p.delete_headers)) for p in plans],
            )
            return
        except (ParseError, ValueError) as e:
            await _send_dlq(producer, dlq, payload, "VALIDATION_ERROR", str(e))
            return
        except AuthError as e:
            if e.code == "REG_NOT_FOUND" or attempt == attempts:
                await _send_dlq(producer, dlq, payload, e.code, e.message)
                return
            await asyncio.sleep(_backoff(attempt, settings))
        except NetworkError as e:
            if attempt == attempts:
                await _send_dlq(producer, dlq, payload, e.code, e.message)
                return
            await asyncio.sleep(_backoff(attempt, settings))


async def run_worker() -> None:
    """Отдельный процесс: consumer correct_cabinet, producer, RegResolver, цикл обработки."""
    from prometheus_client import start_http_server

    settings = get_settings()
    start_http_server(settings.CORRECT_CABINET_METRICS_PORT)
    load_questionnaire_map()  # fail-fast: справочник должен читаться
    logger.info(
        "correct_cabinet worker startup topic=%r dlq=%r group=%r",
        settings.KAFKA_CORRECT_CABINET_TOPIC, settings.KAFKA_CORRECT_CABINET_DLQ_TOPIC,
        settings.KAFKA_CORRECT_CABINET_GROUP_ID,
    )

    stop_event = asyncio.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    consumer = create_consumer(
        settings.KAFKA_CORRECT_CABINET_TOPIC,
        group_id=settings.KAFKA_CORRECT_CABINET_GROUP_ID,
        settings=settings,
        max_poll_records=min(20, settings.KAFKA_MAX_BATCH),
        request_timeout_ms=60_000,
        session_timeout_ms=30_000,
    )
    producer = create_producer(settings=settings)
    resolver = RegResolver()
    await resolver.startup()

    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT, follow_redirects=True, headers=_default_headers(),
    ) as http_client:
        await producer.start()
        await consumer.start()
        try:
            while not stop_event.is_set():
                batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
                if not batch:
                    continue
                for partition, records in batch.items():
                    for record in records:
                        try:
                            await _handle_with_retries(record, resolver, http_client, producer, settings)
                        except Exception as e:
                            logger.exception("correct_cabinet unexpected error offset=%s err=%r", record.offset, e)
                        await consumer.commit({partition: OffsetAndMetadata(record.offset + 1, "")})
        finally:
            await consumer.stop()
            await producer.stop()
            await resolver.shutdown()
