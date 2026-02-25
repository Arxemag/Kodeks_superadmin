"""
Kafka worker для топиков create-user, update-user, update-user-departments.

Инициализация: run_worker() поднимает HTTP-сервер метрик, consumer и producer, RegResolver,
UserService с AuthClient и CatalogClient; затем цикл getmany и обработка записей в задачах.
Обработка: каждая запись обрабатывается в _process_record; успех — commit offset через OffsetTracker;
ошибки валидации/REG_NOT_FOUND — сразу в DLQ; NetworkError/AuthError — retry с backoff, исчерпание — DLQ.
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
from common.kafka import create_consumer, create_producer
from common.exceptions import AuthError, NetworkError, ParseError
from common.logger import get_logger, set_trace_id
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.dto import (
    CreateUserDTO,
    UpdateUserDTO,
    UpdateUserDepartmentsDTO,
    redact_payload,
)
from services.users_service.metrics import (
    active_jobs,
    kafka_lag_seconds,
    processing_latency_histogram,
    users_dlq_total,
    users_failed_total,
    users_retry_total,
)
from services.users_service.reg_resolver import RegResolver
from services.users_service.service import UserService


logger = get_logger("users.worker")


@dataclass
class PartitionState:
    """Состояние по партиции: последний закоммиченный offset и множество завершённых offset для последовательного коммита."""
    committed: int = -1
    completed: set[int] = field(default_factory=set)


class OffsetTracker:
    """
    Учёт завершённых offset по партициям: при полном наборе подряд идущих завершённых
    строит commit до следующего offset. Позволяет коммитить при неупорядоченном завершении задач.
    """
    def __init__(self) -> None:
        self._states: dict[TopicPartition, PartitionState] = {}
        self._lock = asyncio.Lock()

    async def register(self, partition: TopicPartition, offset: int) -> None:
        """Регистрирует партицию при первом появлении offset (инициализирует committed = offset - 1)."""
        async with self._lock:
            if partition in self._states:
                return
            self._states[partition] = PartitionState(committed=offset - 1)

    async def complete_and_build_commit(
        self,
        partition: TopicPartition,
        offset: int,
    ) -> dict[TopicPartition, OffsetAndMetadata]:
        """Помечает offset завершённым и возвращает карту для commit до следующего непрерывного offset."""
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
    """Точка входа воркера: метрики, consumer/producer, RegResolver, цикл опроса и обработки записей до SIGINT/SIGTERM."""
    settings = get_settings()
    start_http_server(settings.USERS_METRICS_PORT)
    logger.debug(
        "users worker startup "
        f"group={settings.KAFKA_GROUP_ID!r} "
        f"broker={settings.KAFKA_BOOTSTRAP_SERVERS!r} "
        f"topics={[settings.KAFKA_CREATE_TOPIC, settings.KAFKA_UPDATE_TOPIC, settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC]!r} "
        f"dlq={settings.KAFKA_DLQ_TOPIC!r} "
        f"concurrency={settings.KAFKA_MAX_CONCURRENCY}"
    )

    stop_event = asyncio.Event()

    def _stop_handler(*_: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    consumer = create_consumer(
        settings.KAFKA_CREATE_TOPIC,
        settings.KAFKA_UPDATE_TOPIC,
        settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC,
        group_id=settings.KAFKA_GROUP_ID,
        settings=settings,
        max_poll_records=settings.KAFKA_MAX_BATCH,
        request_timeout_ms=40_000,
        session_timeout_ms=15_000,
    )
    producer = create_producer(settings=settings)

    resolver = RegResolver()
    await resolver.startup()

    async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True) as http_client:
        auth_client = AuthClient(http_client=http_client, auth_service_url=settings.AUTH_SERVICE_URL)
        catalog_client = CatalogClient(http_client=http_client)
        service = UserService(
            auth_client=auth_client,
            catalog_client=catalog_client,
            reg_resolver=resolver,
        )
        tracker = OffsetTracker()
        semaphore = asyncio.Semaphore(settings.KAFKA_MAX_CONCURRENCY)
        active_tasks: set[asyncio.Task[None]] = set()

        await producer.start()
        await consumer.start()
        try:
            while not stop_event.is_set():
                records_batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
                if not records_batch:
                    continue
                logger.debug(f"polled batch partitions={len(records_batch)}")

                now_ms = int(time.time() * 1000)
                max_lag_sec = 0.0

                for partition, records in records_batch.items():
                    for record in records:
                        await semaphore.acquire()
                        lag_sec = max(0.0, (now_ms - record.timestamp) / 1000.0)
                        if lag_sec > max_lag_sec:
                            max_lag_sec = lag_sec
                        await tracker.register(partition, record.offset)

                        task = asyncio.create_task(
                            _process_record(
                                record=record,
                                service=service,
                                producer=producer,
                                consumer=consumer,
                                tracker=tracker,
                                semaphore=semaphore,
                                settings=settings,
                            )
                        )
                        active_tasks.add(task)
                        task.add_done_callback(active_tasks.discard)

                kafka_lag_seconds.set(max_lag_sec)

            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
        finally:
            await consumer.stop()
            await producer.stop()
            await resolver.shutdown()


async def _process_record(
    *,
    record: ConsumerRecord,
    service: UserService,
    producer: AIOKafkaProducer,
    consumer: AIOKafkaConsumer,
    tracker: OffsetTracker,
    semaphore: asyncio.Semaphore,
    settings: Any,
) -> None:
    """Обрабатывает одну запись: _handle_with_retries, при успехе — complete_and_build_commit и consumer.commit; метрики и release semaphore в finally."""
    active_jobs.inc()
    started = time.perf_counter()
    topic_partition = TopicPartition(record.topic, record.partition)
    try:
        logger.debug(
            f"process start topic={record.topic} partition={record.partition} offset={record.offset}"
        )
        await _handle_with_retries(record, service, producer, settings)
        commit_map = await tracker.complete_and_build_commit(topic_partition, record.offset)
        logger.debug(
            f"committing topic={record.topic} partition={record.partition} "
            f"next_offset={commit_map[topic_partition].offset}"
        )
        await consumer.commit(commit_map)
    except Exception as e:  # pragma: no cover
        users_failed_total.labels(reason="unhandled").inc()
        logger.exception(f"unexpected processing error topic={record.topic} offset={record.offset} err={e!r}")
    finally:
        active_jobs.dec()
        processing_latency_histogram.observe(time.perf_counter() - started)
        semaphore.release()


async def _handle_with_retries(
    record: ConsumerRecord,
    service: UserService,
    producer: AIOKafkaProducer,
    settings: Any,
) -> None:
    """Валидация payload, определение топика и вызов handle_create/update/update_departments; при ошибках — DLQ или retry с backoff (AuthError/NetworkError)."""
    payload_raw = record.value
    if not isinstance(payload_raw, dict):
        await _send_dlq(
            producer,
            settings.KAFKA_DLQ_TOPIC,
            payload_raw,
            "INVALID_PAYLOAD",
            "Payload is not an object",
        )
        users_failed_total.labels(reason="invalid_payload").inc()
        return

    set_trace_id(f"kafka-{record.topic}-{record.partition}-{record.offset}")
    redacted = redact_payload(payload_raw)
    logger.info(f"processing topic={record.topic} offset={record.offset} payload={redacted}")

    attempts = settings.USERS_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        logger.debug(
            f"attempt {attempt}/{attempts} topic={record.topic} partition={record.partition} offset={record.offset}"
        )
        try:
            if record.topic == settings.KAFKA_CREATE_TOPIC:
                dto = CreateUserDTO.model_validate(payload_raw)
                await service.handle_create(dto)
                return
            if record.topic == settings.KAFKA_UPDATE_TOPIC:
                dto = UpdateUserDTO.model_validate(payload_raw)
                await service.handle_update(dto)
                return
            if record.topic == settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC:
                dto = UpdateUserDepartmentsDTO.model_validate(payload_raw)
                await service.handle_update_departments(dto)
                return

            await _send_dlq(
                producer,
                settings.KAFKA_DLQ_TOPIC,
                payload_raw,
                "UNKNOWN_TOPIC",
                f"Unsupported topic: {record.topic}",
            )
            users_failed_total.labels(reason="unknown_topic").inc()
            return
        except (ValidationError, ParseError, ValueError) as e:
            logger.debug(
                f"non-retryable error topic={record.topic} partition={record.partition} "
                f"offset={record.offset} err={e!r}"
            )
            await _send_dlq(
                producer,
                settings.KAFKA_DLQ_TOPIC,
                payload_raw,
                "VALIDATION_ERROR",
                str(e),
            )
            users_failed_total.labels(reason="validation_or_parse").inc()
            return
        except AuthError as e:
            if e.code == "REG_NOT_FOUND":
                logger.debug(
                    f"non-retryable auth error topic={record.topic} partition={record.partition} "
                    f"offset={record.offset} code={e.code}"
                )
                await _send_dlq(
                    producer,
                    settings.KAFKA_DLQ_TOPIC,
                    payload_raw,
                    e.code,
                    e.message,
                )
                users_failed_total.labels(reason="reg_not_found").inc()
                return
            if attempt == attempts:
                logger.debug(
                    f"retry exhausted topic={record.topic} partition={record.partition} "
                    f"offset={record.offset} code={e.code} message={e.message!r}"
                )
                await _send_dlq(
                    producer,
                    settings.KAFKA_DLQ_TOPIC,
                    payload_raw,
                    e.code,
                    e.message,
                )
                users_failed_total.labels(reason="retry_exhausted").inc()
                return
            users_retry_total.inc()
            delay = _backoff_with_jitter(
                attempt=attempt,
                base=settings.USERS_RETRY_BASE_DELAY,
                max_delay=settings.USERS_RETRY_MAX_DELAY,
            )
            logger.debug(
                f"retry scheduled topic={record.topic} partition={record.partition} offset={record.offset} "
                f"delay={delay:.3f}s code={e.code}"
            )
            await asyncio.sleep(delay)
            continue
        except NetworkError as e:
            if attempt == attempts:
                logger.debug(
                    f"retry exhausted topic={record.topic} partition={record.partition} "
                    f"offset={record.offset} code={e.code} message={e.message!r}"
                )
                await _send_dlq(
                    producer,
                    settings.KAFKA_DLQ_TOPIC,
                    payload_raw,
                    e.code,
                    e.message,
                )
                users_failed_total.labels(reason="retry_exhausted").inc()
                return
            users_retry_total.inc()
            delay = _backoff_with_jitter(
                attempt=attempt,
                base=settings.USERS_RETRY_BASE_DELAY,
                max_delay=settings.USERS_RETRY_MAX_DELAY,
            )
            logger.debug(
                f"retry scheduled topic={record.topic} partition={record.partition} offset={record.offset} "
                f"delay={delay:.3f}s code={e.code}"
            )
            await asyncio.sleep(delay)
            continue
        except Exception as e:  # pragma: no cover
            logger.debug(
                f"unexpected non-retryable topic={record.topic} partition={record.partition} "
                f"offset={record.offset} err={e!r}"
            )
            await _send_dlq(
                producer,
                settings.KAFKA_DLQ_TOPIC,
                payload_raw,
                "UNHANDLED_ERROR",
                str(e),
            )
            users_failed_total.labels(reason="unhandled").inc()
            return


def _backoff_with_jitter(*, attempt: int, base: float, max_delay: float) -> float:
    """Экспоненциальная задержка с ограничением max_delay и добавлением jitter до 25% от задержки."""
    exp = min(max_delay, base * (2 ** (attempt - 1)))
    jitter = random.uniform(0, exp * 0.25)
    return exp + jitter


async def _send_dlq(
    producer: AIOKafkaProducer,
    dlq_topic: str,
    payload: Any,
    error_code: str,
    error_message: str,
) -> None:
    """Отправляет в DLQ сообщение с original_message, error_code, error_message и timestamp; инкрементирует users_dlq_total."""
    logger.debug(f"send dlq topic={dlq_topic!r} code={error_code!r} message={error_message!r}")
    message = {
        "original_message": payload,
        "error_code": error_code,
        "error_message": error_message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await producer.send_and_wait(dlq_topic, message)
    users_dlq_total.inc()
