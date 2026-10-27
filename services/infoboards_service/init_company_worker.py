"""
Kafka worker для топика init_company.

Обработка: получить base_url, sync mapping, sync groups, get boards, build ACL, POST /admin/dirs.
Commit offset только после полного успеха. REG_NOT_FOUND и ошибки валидации — DLQ.
NetworkError/AuthError (кроме REG_NOT_FOUND) — retry с backoff, затем DLQ.
"""
from __future__ import annotations

import asyncio
import json
import random
import signal
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.structs import ConsumerRecord, OffsetAndMetadata
from pydantic import ValidationError
from prometheus_client import start_http_server

from common.config import get_settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.kafka import (
    MalformedJSON, OffsetTracker, create_consumer, create_producer, unwrap_payload,
    dlq_payload, ensure_partition_assigned, get_assignment_epoch, process_partition_batch, record_coordinates,
)
from common.logger import get_logger, set_trace_id
from services.infoboards_service.dto import InitCompanyDTO, is_missing_department_id_error
from services.infoboards_service.init_company_service import InitCompanyService
from services.infoboards_service.metrics import (
    init_company_active_jobs,
    init_company_dlq_total,
    init_company_failed_total,
    init_company_lag_seconds,
    init_company_processing_histogram,
    init_company_retry_total,
    init_company_total,
)
from services.users_service.catalog_client import CatalogClient
from services.users_service.reg_resolver import RegResolver


logger = get_logger("init_company.worker")


async def run_worker() -> None:
    """Точка входа: метрики, consumer, producer, DB pool, цикл обработки."""
    settings = get_settings()
    start_http_server(settings.INIT_COMPANY_METRICS_PORT)
    logger.debug(
        f"init_company worker startup "
        f"topic={settings.KAFKA_INIT_COMPANY_TOPIC!r} "
        f"dlq={settings.KAFKA_INIT_COMPANY_DLQ_TOPIC!r} "
        f"group={settings.KAFKA_INIT_COMPANY_GROUP_ID!r}"
    )

    stop_event = asyncio.Event()

    def _stop(*_: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    consumer = create_consumer(
        settings.KAFKA_INIT_COMPANY_TOPIC,
        settings.KAFKA_SYNC_DEPARTMENTS_TOPIC,
        group_id=settings.KAFKA_INIT_COMPANY_GROUP_ID,
        settings=settings,
        max_poll_records=min(50, settings.KAFKA_MAX_BATCH),
        request_timeout_ms=60_000,
        session_timeout_ms=30_000,
    )
    producer = create_producer(settings=settings)

    resolver = RegResolver()
    await resolver.startup()

    tracker = OffsetTracker()
    semaphore = asyncio.Semaphore(min(5, settings.KAFKA_MAX_CONCURRENCY))

    from common.http import _default_headers
    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT,
        follow_redirects=True,
        headers=_default_headers(),
    ) as http_client:
        catalog_client = CatalogClient(http_client=http_client)
        await producer.start()
        await consumer.start()
        try:
            while not stop_event.is_set():
                batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
                if not batch:
                    continue

                now_ms = int(time.time() * 1000)
                init_company_lag_seconds.set(max(
                    (max(0.0, (now_ms - record.timestamp) / 1000.0)
                     for records in batch.values() for record in records), default=0.0,
                ))

                batch_epoch = get_assignment_epoch(consumer)

                async def process(partition, record):
                    ensure_partition_assigned(consumer, partition, batch_epoch)
                    await tracker.register(partition, record.offset)
                    await semaphore.acquire()
                    if get_assignment_epoch(consumer) != batch_epoch:
                        semaphore.release()
                        raise RuntimeError("Kafka assignment changed while waiting for capacity")
                    await _process_record(
                        record=record, resolver=resolver, catalog_client=catalog_client,
                        http_client=http_client, producer=producer, consumer=consumer,
                        tracker=tracker, semaphore=semaphore, settings=settings,
                    )

                await process_partition_batch(batch, process)
        finally:
            await consumer.stop()
            await producer.stop()
            await resolver.shutdown()


async def _process_record(
    *,
    record: ConsumerRecord,
    resolver: RegResolver,
    catalog_client: CatalogClient,
    http_client: httpx.AsyncClient,
    producer: AIOKafkaProducer,
    consumer: AIOKafkaConsumer,
    tracker: OffsetTracker,
    semaphore: asyncio.Semaphore,
    settings: Any,
) -> None:
    """Обработка одной записи init_company."""
    init_company_active_jobs.inc()
    started = time.perf_counter()
    tp = TopicPartition(record.topic, record.partition)
    epoch = get_assignment_epoch(consumer)
    try:
        ensure_partition_assigned(consumer, tp, epoch)
        await _handle_with_retries(
            record, resolver, catalog_client, http_client, producer, settings
        )
        commit_map = await tracker.complete_and_build_commit(tp, record.offset)
        ensure_partition_assigned(consumer, tp, epoch)
        await consumer.commit(commit_map)
        init_company_total.inc()
    except Exception as e:
        init_company_failed_total.labels(reason="unhandled").inc()
        logger.exception(f"init_company unexpected error topic={record.topic} offset={record.offset} err={e!r}")
        raise
    finally:
        init_company_active_jobs.dec()
        init_company_processing_histogram.observe(time.perf_counter() - started)
        semaphore.release()


def _normalize_department_item(item: dict[str, Any]) -> dict[str, Any]:
    """Приводит элемент companyDepartments к виду { id, title, modules } для InitCompanyDTO."""
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


async def _handle_with_retries(
    record: ConsumerRecord,
    resolver: RegResolver,
    catalog_client: CatalogClient,
    http_client: httpx.AsyncClient,
    producer: AIOKafkaProducer,
    settings: Any,
) -> None:
    """Валидация, вызов InitCompanyService, retry/DLQ при ошибках."""
    send_dlq = partial(_send_dlq, source_record=record)
    raw_value = record.value
    # Что реально пришло из Kafka (включите DEBUG, чтобы увидеть, почему нет reg)
    if isinstance(raw_value, dict):
        logger.debug(
            "init_company raw message keys=%s payload_keys=%s",
            list(raw_value.keys()),
            list(raw_value["payload"].keys()) if isinstance(raw_value.get("payload"), dict) else type(raw_value.get("payload")).__name__,
        )
    else:
        logger.debug("init_company raw value type=%s", type(raw_value).__name__)

    payload = unwrap_payload(raw_value)
    if isinstance(payload, MalformedJSON):
        await send_dlq(producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC, payload, "INVALID_JSON", payload.error)
        init_company_failed_total.labels(reason="invalid_json").inc()
        return
    if not isinstance(payload, dict):
        await send_dlq(
            producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
            payload, "INVALID_PAYLOAD", "Payload is not an object",
        )
        init_company_failed_total.labels(reason="invalid_payload").inc()
        return

    # Поддержка обёртки payload.data (если reg/id/companyName не на верхнем уровне)
    if "data" in payload and isinstance(payload.get("data"), dict) and payload.get("reg") is None:
        payload = payload["data"]

    # Подстановка reg из типичных альтернативных ключей (формат продюсера может отличаться)
    if payload.get("reg") is None:
        for key in (
            "registration", "reg_number", "regNumber", "companyReg",
            "registrationNumber", "company_reg", "companyRegNumber", "reg_id",
        ):
            if payload.get(key) is not None:
                payload = {**payload, "reg": str(payload[key])}
                break

    # Подстановка id из альтернативных ключей
    if payload.get("id") is None:
        for key in ("companyId", "company_id"):
            if payload.get(key) is not None:
                payload = {**payload, "id": payload[key]}
                break

    # Нормализация: companyDepartments → departments (продюсер может слать только companyDepartments)
    if payload.get("departments") is None and payload.get("companyDepartments") is not None:
        raw = payload.get("companyDepartments")
        if isinstance(raw, list):
            normalized = [_normalize_department_item(d) for d in raw if isinstance(d, dict)]
            # только элементы с заданным id (без id валидация DTO всё равно упадёт)
            payload = {**payload, "departments": [x for x in normalized if x.get("id") is not None]}

    set_trace_id(f"init_company-{record.topic}-{record.partition}-{record.offset}")
    logger.info(f"processing init_company offset={record.offset} reg={payload.get('reg')!r}")
    logger.debug("init_company payload keys: %s", list(payload.keys()))

    attempts = settings.USERS_RETRY_ATTEMPTS
    if attempts < 1:
        raise RuntimeError("Retry attempts must be at least one")
    for attempt in range(1, attempts + 1):
        try:
            dto = InitCompanyDTO.model_validate(payload)
        except ValidationError as e:
            if is_missing_department_id_error(e):
                logger.warning("В отделе отсутствует id (departments[].id). Укажите UUID для каждого отдела.")
            await send_dlq(
                producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
                payload, "VALIDATION_ERROR", str(e),
            )
            init_company_failed_total.labels(reason="validation").inc()
            return

        logger.info(
            "init_company DTO validated reg=%r id=%r companyName=%r departments=%s",
            dto.reg, dto.id, dto.companyName, len(dto.departments),
        )
        async with resolver.with_session() as session:
            service = InitCompanyService(
                db=session,
                settings=settings,
                http_client=http_client,
                catalog_client=catalog_client,
            )
            try:
                await service.handle(dto)
            except (ValidationError, ParseError, ValueError) as e:
                await send_dlq(
                    producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
                    payload, e.code if isinstance(e, ParseError) else "VALIDATION_ERROR", str(e),
                )
                init_company_failed_total.labels(reason="validation_or_parse").inc()
                return
            except AuthError as e:
                if e.code == "REG_NOT_FOUND":
                    await send_dlq(
                        producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
                        payload, e.code, e.message,
                    )
                    init_company_failed_total.labels(reason="reg_not_found").inc()
                    return
                if attempt == attempts:
                    await send_dlq(producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC, payload, e.code, e.message)
                    init_company_failed_total.labels(reason="retry_exhausted").inc()
                    return
                init_company_retry_total.inc()
                await asyncio.sleep(_backoff(attempt, settings))
                continue
            except NetworkError as e:
                if attempt == attempts:
                    await send_dlq(producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC, payload, e.code, e.message)
                    init_company_failed_total.labels(reason="retry_exhausted").inc()
                    return
                init_company_retry_total.inc()
                await asyncio.sleep(_backoff(attempt, settings))
                continue
        return  # успех


def _backoff(attempt: int, settings: Any) -> float:
    """Exponential backoff с jitter."""
    exp = min(settings.USERS_RETRY_MAX_DELAY, settings.USERS_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


async def _send_dlq(
    producer: AIOKafkaProducer,
    topic: str,
    payload: Any,
    error_code: str,
    error_message: str,
    *, source_record=None,
) -> None:
    """Отправка в DLQ."""
    msg = {
        "original_message": dlq_payload(payload),
        **record_coordinates(source_record),
        "error_code": error_code,
        "error_message": error_message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await producer.send_and_wait(topic, msg)
    init_company_dlq_total.inc()
