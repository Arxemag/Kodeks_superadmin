"""
Единый Kafka worker: одна consumer group, подписка на все топики, маршрутизация по record.topic.

Топики: create-user, update-user, update-user-departments, init_company, enable_reg_company, disable_reg_company.
Один consumer сохраняет входы в PostgreSQL. Исполнение и outbox работают независимо от Kafka polling.
Standalone service workers are legacy; production uses only this durable path.
"""
from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.structs import ConsumerRecord, OffsetAndMetadata
from prometheus_client import Gauge, start_http_server

from common.config import get_settings
from common.db import healthcheck_schema, shutdown_db
from common.event_store import EventStore, IncomingEvent
from common.http import _default_headers
from common.kafka import (
    MalformedJSON, create_consumer, create_producer, event_type_from_message,
    ensure_partition_assigned, get_assignment_epoch, send_record_dlq, unwrap_payload,
)
from common.logger import get_logger
from services.infoboards_service.correct_cabinet_worker import _handle_with_retries as correct_cabinet_handle
from services.infoboards_service.init_company_worker import _handle_with_retries as init_company_handle
from services.reg_company_service.worker import _handle_record as reg_company_handle
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.reg_resolver import RegResolver
from services.users_service.service import UserService
from services.users_service.worker import _handle_with_retries as users_handle


logger = get_logger("unified.worker")
unified_worker_ready = Gauge("unified_worker_ready", "Durable unified pipeline is running")


def _all_topics(settings: Any) -> list[str]:
    """Список всех топиков для единого consumer."""
    return [
        settings.KAFKA_CREATE_TOPIC,
        settings.KAFKA_UPDATE_TOPIC,
        settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC,
        settings.KAFKA_INIT_COMPANY_TOPIC,
        settings.KAFKA_SYNC_DEPARTMENTS_TOPIC,
        settings.KAFKA_ENABLE_REG_COMPANY_TOPIC,
        settings.KAFKA_DISABLE_REG_COMPANY_TOPIC,
        settings.KAFKA_CORRECT_CABINET_TOPIC,
    ]


def _effective_topic(record: ConsumerRecord) -> str:
    """Тип события: из event_type (common.kafka), иначе Kafka record.topic."""
    topic = event_type_from_message(record.value)
    return topic if topic else record.topic


def _records_from_value(record: ConsumerRecord) -> list[ConsumerRecord | SimpleNamespace]:
    """
    Один Kafka record: если value — массив событий, возвращаем по одному «виртуальному» record на элемент;
    иначе — один элемент [record]. Маршрутизация по event_type у каждого.
    """
    value = record.value
    if not isinstance(value, list) or len(value) == 0:
        return [record]
    out: list[ConsumerRecord | SimpleNamespace] = []
    for i, item in enumerate(value):
        topic = event_type_from_message(item) or record.topic
        out.append(
            SimpleNamespace(
                value=item,
                topic=topic,
                partition=record.partition,
                offset=record.offset,
                source_topic=record.topic,
                array_index=i,
            )
        )
    return out if out else [record]


def _record_with_effective_topic(record: ConsumerRecord, effective_topic: str) -> SimpleNamespace:
    """Виртуальный record с topic=effective_topic, чтобы users/reg_company роутили по event_type при едином топике Kafka."""
    return SimpleNamespace(
        value=record.value,
        topic=effective_topic,
        partition=record.partition,
        offset=record.offset,
        source_topic=getattr(record, "source_topic", record.topic),
        array_index=getattr(record, "array_index", None),
    )


async def _dispatch_record(
    record: ConsumerRecord,
    *,
    user_service: UserService,
    resolver: RegResolver,
    catalog_client: Any,
    http_client: Any,
    producer: AIOKafkaProducer,
    settings: Any,
) -> None:
    """Маршрутизация по топику: event_type из сообщения (если есть) или record.topic."""
    topic = _effective_topic(record)
    logger.debug("dispatch record kafka_topic=%s effective_topic=%s offset=%s", record.topic, topic, record.offset)
    if topic in (settings.KAFKA_ENABLE_REG_COMPANY_TOPIC, settings.KAFKA_DISABLE_REG_COMPANY_TOPIC):
        await reg_company_handle(_record_with_effective_topic(record, topic), producer, settings)
        return
    payload = unwrap_payload(record.value)
    if isinstance(payload, MalformedJSON) or not isinstance(payload, dict):
        code = "INVALID_JSON" if isinstance(payload, MalformedJSON) else "INVALID_PAYLOAD"
        await send_record_dlq(producer, settings.KAFKA_DLQ_TOPIC, record, code, "Event payload must be a valid JSON object")
        return
    if topic in (
        settings.KAFKA_CREATE_TOPIC,
        settings.KAFKA_UPDATE_TOPIC,
        settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC,
    ):
        # users_handle роутит по record.topic — передаём виртуальный record с effective topic
        await users_handle(_record_with_effective_topic(record, topic), user_service, producer, settings)
        return
    if topic in (settings.KAFKA_INIT_COMPANY_TOPIC, settings.KAFKA_SYNC_DEPARTMENTS_TOPIC):
        await init_company_handle(
            record, resolver, catalog_client, http_client, producer, settings
        )
        return
    if topic == settings.KAFKA_CORRECT_CABINET_TOPIC:
        await correct_cabinet_handle(record, resolver, http_client, producer, settings)
        return
    await send_record_dlq(
        producer, settings.KAFKA_DLQ_TOPIC, record,
        "UNKNOWN_TOPIC", f"Unsupported event type: {topic}",
    )


def _dlq_topic_for(event_type, settings):
    if event_type in (settings.KAFKA_ENABLE_REG_COMPANY_TOPIC, settings.KAFKA_DISABLE_REG_COMPANY_TOPIC):
        return settings.KAFKA_REG_COMPANY_DLQ_TOPIC
    if event_type in (settings.KAFKA_INIT_COMPANY_TOPIC, settings.KAFKA_SYNC_DEPARTMENTS_TOPIC):
        return settings.KAFKA_INIT_COMPANY_DLQ_TOPIC
    if event_type == settings.KAFKA_CORRECT_CABINET_TOPIC:
        return settings.KAFKA_CORRECT_CABINET_DLQ_TOPIC
    return settings.KAFKA_DLQ_TOPIC


def _incoming_events(record, settings):
    snapshots = (settings.KAFKA_INIT_COMPANY_TOPIC, settings.KAFKA_SYNC_DEPARTMENTS_TOPIC)
    return [IncomingEvent.from_record(
        item, namespace=settings.INTEGRATION_SOURCE_NAMESPACE,
        event_type=_effective_topic(item),
        dlq_topic=_dlq_topic_for(_effective_topic(item), settings),
        snapshot_types=snapshots,
        user_revision_types=(settings.KAFKA_UPDATE_TOPIC, settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC),
    ) for item in _records_from_value(record)]


async def _persist_record(record, *, consumer, store, settings, expected_epoch=None):
    """Kafka commit acknowledges durable intake of every array element, not execution."""
    partition = TopicPartition(record.topic, record.partition)
    if expected_epoch is None:
        expected_epoch = get_assignment_epoch(consumer)
    ensure_partition_assigned(consumer, partition, expected_epoch)
    await store.ingest(_incoming_events(record, settings))
    ensure_partition_assigned(consumer, partition, expected_epoch)
    await consumer.commit({partition: OffsetAndMetadata(record.offset + 1, '')})


async def _idle(stop_event, seconds):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def _intake_loop(consumer, store, settings, stop_event):
    async def persist_fetched(batch):
        epoch = get_assignment_epoch(consumer)
        for records in batch.values():
            for record in records:
                if stop_event.is_set():
                    return
                await _persist_record(record, consumer=consumer, store=store,
                                      settings=settings, expected_epoch=epoch)

    while not stop_event.is_set():
        assigned = consumer.assignment()
        if await store.pending_count() >= settings.INTEGRATION_MAX_PENDING:
            if assigned:
                consumer.pause(*assigned)
            # Keep polling while paused so a long downstream outage does not
            # itself violate max_poll_interval_ms and evict this consumer.
            # A rebalance may assign a new, unpaused partition while waiting.
            # Its records still advance the consumer position and must never
            # be discarded even when the previous assignment was paused.
            await persist_fetched(await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS))
            await _idle(stop_event, settings.INTEGRATION_POLL_SECONDS)
            continue
        paused = consumer.paused() & assigned
        if paused:
            consumer.resume(*paused)
        batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
        await persist_fetched(batch)


async def _executor_loop(store, dispatch, settings, stop_event):
    snapshots = (settings.KAFKA_INIT_COMPANY_TOPIC, settings.KAFKA_SYNC_DEPARTMENTS_TOPIC)
    while not stop_event.is_set():
        worked = await store.execute_one(
            dispatch, dlq_topic_for=lambda event_type: _dlq_topic_for(event_type, settings),
            snapshot_types=snapshots,
            resolver_types=(settings.KAFKA_CREATE_TOPIC,),
            user_dependency_types=(settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC,),
        )
        if not worked:
            await _idle(stop_event, settings.INTEGRATION_POLL_SECONDS)


async def _outbox_loop(store, producer, settings, stop_event):
    while not stop_event.is_set():
        if not await store.publish_one(producer):
            await _idle(stop_event, settings.INTEGRATION_POLL_SECONDS)


async def _run_pipeline(consumer, producer, store, dispatch, settings, stop_event):
    # Each executor may need its journal connection, catalog-lock connection and
    # a service DB connection. Leave room for intake/publisher in the same pool.
    count = min(settings.INTEGRATION_EXECUTOR_CONCURRENCY, max(1, (settings.POOL_SIZE - 2) // 3))
    logger.info('Durable unified pipeline executors=%s namespace=%s', count, settings.INTEGRATION_SOURCE_NAMESPACE)
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(_intake_loop(consumer, store, settings, stop_event)),
                     group.create_task(_outbox_loop(store, producer, settings, stop_event))]
            tasks.extend(group.create_task(_executor_loop(store, dispatch, settings, stop_event)) for _ in range(count))
            unified_worker_ready.set(1)
            await stop_event.wait()
            try:
                async with asyncio.timeout(settings.INTEGRATION_SHUTDOWN_SECONDS):
                    await asyncio.gather(*tasks)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                # Await rollback/cancellation cleanup before closing DB/Kafka.
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        unified_worker_ready.set(0)


async def run_unified_worker() -> None:
    """Production path: intake -> durable inbox -> executor/outbox -> Kafka.

    Standalone service workers are legacy and must not run alongside this path.
    """
    settings = get_settings()
    start_http_server(settings.UNIFIED_WORKER_METRICS_PORT)
    unified_worker_ready.set(0)
    await healthcheck_schema()
    stop_event = asyncio.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    consumer = create_consumer(*_all_topics(settings), group_id=settings.KAFKA_GROUP_ID,
                               settings=settings, max_poll_records=settings.KAFKA_MAX_BATCH,
                               request_timeout_ms=60_000, session_timeout_ms=30_000)
    producer = create_producer(settings=settings, acks='all', enable_idempotence=True)
    resolver = RegResolver()
    try:
        await resolver.startup()
        async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True,
                                     headers=_default_headers()) as http_client:
            auth_client = AuthClient(http_client=http_client, auth_service_url=settings.AUTH_SERVICE_URL)
            catalog_client = CatalogClient(http_client=http_client)
            user_service = UserService(auth_client=auth_client, catalog_client=catalog_client, reg_resolver=resolver)
            store = EventStore(settings)
            async def dispatch(record, outbox_producer):
                await _dispatch_record(record, user_service=user_service, resolver=resolver,
                                       catalog_client=catalog_client, http_client=http_client,
                                       producer=outbox_producer, settings=settings)
            await producer.start()
            await consumer.start()
            await _run_pipeline(consumer, producer, store, dispatch, settings, stop_event)
    finally:
        unified_worker_ready.set(0)
        try:
            await consumer.stop()
        finally:
            try:
                await producer.stop()
            finally:
                await resolver.shutdown()
                await shutdown_db()
