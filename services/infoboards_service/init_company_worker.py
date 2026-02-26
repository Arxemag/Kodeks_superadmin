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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.structs import ConsumerRecord, OffsetAndMetadata
from pydantic import ValidationError
from prometheus_client import start_http_server

from common.config import get_settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.kafka import create_consumer, create_producer, unwrap_payload
from common.logger import get_logger, set_trace_id
from services.infoboards_service.dto import InitCompanyDTO
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


@dataclass
class PartitionState:
    """Состояние партиции для последовательного коммита."""
    committed: int = -1
    completed: set[int] = field(default_factory=set)


class OffsetTracker:
    """Учёт offset для коммита при неупорядоченном завершении."""

    def __init__(self) -> None:
        self._states: dict[TopicPartition, PartitionState] = {}
        self._lock = asyncio.Lock()

    async def register(self, partition: TopicPartition, offset: int) -> None:
        async with self._lock:
            if partition not in self._states:
                self._states[partition] = PartitionState(committed=offset - 1)

    async def complete_and_build_commit(
        self, partition: TopicPartition, offset: int
    ) -> dict[TopicPartition, OffsetAndMetadata]:
        async with self._lock:
            state = self._states.setdefault(partition, PartitionState(committed=offset - 1))
            state.completed.add(offset)
            next_offset = state.committed + 1
            while next_offset in state.completed:
                state.completed.remove(next_offset)
                state.committed = next_offset
                next_offset += 1
            return {partition: OffsetAndMetadata(state.committed + 1, "")}


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
    active_tasks: set[asyncio.Task[None]] = set()

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
                max_lag = 0.0

                for partition, records in batch.items():
                    for record in records:
                        await semaphore.acquire()
                        lag = max(0.0, (now_ms - record.timestamp) / 1000.0)
                        if lag > max_lag:
                            max_lag = lag
                        await tracker.register(partition, record.offset)
                        task = asyncio.create_task(
                            _process_record(
                                record=record,
                                resolver=resolver,
                                catalog_client=catalog_client,
                                http_client=http_client,
                                producer=producer,
                                consumer=consumer,
                                tracker=tracker,
                                semaphore=semaphore,
                                settings=settings,
                            )
                        )
                        active_tasks.add(task)
                        task.add_done_callback(active_tasks.discard)

                init_company_lag_seconds.set(max_lag)

            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
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
    try:
        await _handle_with_retries(
            record, resolver, catalog_client, http_client, producer, settings
        )
        commit_map = await tracker.complete_and_build_commit(tp, record.offset)
        await consumer.commit(commit_map)
        init_company_total.inc()
    except Exception as e:
        init_company_failed_total.labels(reason="unhandled").inc()
        logger.exception(f"init_company unexpected error topic={record.topic} offset={record.offset} err={e!r}")
    finally:
        init_company_active_jobs.dec()
        init_company_processing_histogram.observe(time.perf_counter() - started)
        semaphore.release()


async def _handle_with_retries(
    record: ConsumerRecord,
    resolver: RegResolver,
    catalog_client: CatalogClient,
    http_client: httpx.AsyncClient,
    producer: AIOKafkaProducer,
    settings: Any,
) -> None:
    """Валидация, вызов InitCompanyService, retry/DLQ при ошибках."""
    payload = unwrap_payload(record.value)
    if not isinstance(payload, dict):
        await _send_dlq(
            producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
            payload, "INVALID_PAYLOAD", "Payload is not an object",
        )
        init_company_failed_total.labels(reason="invalid_payload").inc()
        return

    # Поддержка обёртки payload.data (если reg/id/companyName не на верхнем уровне)
    if "data" in payload and isinstance(payload.get("data"), dict) and payload.get("reg") is None:
        payload = payload["data"]

    # Подстановка reg из типичных альтернативных ключей, если пришло под другим именем
    if payload.get("reg") is None:
        for key in ("registration", "reg_number", "regNumber", "companyReg"):
            if payload.get(key) is not None:
                payload = {**payload, "reg": str(payload[key])}
                break

    set_trace_id(f"init_company-{record.topic}-{record.partition}-{record.offset}")
    logger.info(f"processing init_company offset={record.offset} reg={payload.get('reg')!r}")
    logger.debug("init_company payload keys: %s", list(payload.keys()))

    attempts = settings.USERS_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            dto = InitCompanyDTO.model_validate(payload)
        except ValidationError as e:
            await _send_dlq(
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
                await _send_dlq(
                    producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
                    payload, "VALIDATION_ERROR", str(e),
                )
                init_company_failed_total.labels(reason="validation_or_parse").inc()
                return
            except AuthError as e:
                if e.code == "REG_NOT_FOUND":
                    await _send_dlq(
                        producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC,
                        payload, e.code, e.message,
                    )
                    init_company_failed_total.labels(reason="reg_not_found").inc()
                    return
                if attempt == attempts:
                    await _send_dlq(producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC, payload, e.code, e.message)
                    init_company_failed_total.labels(reason="retry_exhausted").inc()
                    return
                init_company_retry_total.inc()
                await asyncio.sleep(_backoff(attempt, settings))
                continue
            except NetworkError as e:
                if attempt == attempts:
                    await _send_dlq(producer, settings.KAFKA_INIT_COMPANY_DLQ_TOPIC, payload, e.code, e.message)
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
) -> None:
    """Отправка в DLQ."""
    msg = {
        "original_message": payload,
        "error_code": error_code,
        "error_message": error_message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await producer.send_and_wait(topic, msg)
    init_company_dlq_total.inc()
