"""
Единый Kafka worker: одна consumer group, подписка на все топики, маршрутизация по record.topic.

Топики: create-user, update-user, update-user-departments, init_company, enable_reg_company, disable_reg_company.
Один consumer с KAFKA_GROUP_ID, один producer, один OffsetTracker. Обработчики — те же, что в отдельных воркерах.
"""
from __future__ import annotations

import asyncio
import signal
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.structs import ConsumerRecord
from prometheus_client import start_http_server

from common.config import get_settings
from common.http import _default_headers
from common.kafka import create_consumer, create_producer
from common.logger import get_logger
from services.infoboards_service.init_company_worker import _handle_with_retries as init_company_handle
from services.reg_company_service.worker import _process_one as reg_company_process_one
from services.users_service.auth_client import AuthClient
from services.users_service.catalog_client import CatalogClient
from services.users_service.reg_resolver import RegResolver
from services.users_service.service import UserService
from services.users_service.worker import OffsetTracker, _handle_with_retries as users_handle


logger = get_logger("unified.worker")


def _all_topics(settings: Any) -> list[str]:
    """Список всех топиков для единого consumer."""
    return [
        settings.KAFKA_CREATE_TOPIC,
        settings.KAFKA_UPDATE_TOPIC,
        settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC,
        settings.KAFKA_INIT_COMPANY_TOPIC,
        settings.KAFKA_ENABLE_REG_COMPANY_TOPIC,
        settings.KAFKA_DISABLE_REG_COMPANY_TOPIC,
    ]


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
    """Маршрутизация по топику: вызывается нужный обработчик."""
    topic = record.topic
    if topic in (
        settings.KAFKA_CREATE_TOPIC,
        settings.KAFKA_UPDATE_TOPIC,
        settings.KAFKA_UPDATE_DEPARTMENTS_TOPIC,
    ):
        await users_handle(record, user_service, producer, settings)
        return
    if topic == settings.KAFKA_INIT_COMPANY_TOPIC:
        await init_company_handle(
            record, resolver, catalog_client, http_client, producer, settings
        )
        return
    if topic in (settings.KAFKA_ENABLE_REG_COMPANY_TOPIC, settings.KAFKA_DISABLE_REG_COMPANY_TOPIC):
        reg_company_process_one(record, settings)
        return
    logger.warning("unified worker: неизвестный топик topic=%s offset=%s", topic, record.offset)


async def _process_one_record(
    record: ConsumerRecord,
    *,
    user_service: UserService,
    resolver: RegResolver,
    catalog_client: Any,
    http_client: Any,
    producer: AIOKafkaProducer,
    consumer: AIOKafkaConsumer,
    tracker: OffsetTracker,
    semaphore: asyncio.Semaphore,
    settings: Any,
) -> None:
    """Одна запись: dispatch, при успешном возврате — commit."""
    tp = TopicPartition(record.topic, record.partition)
    try:
        await _dispatch_record(
            record,
            user_service=user_service,
            resolver=resolver,
            catalog_client=catalog_client,
            http_client=http_client,
            producer=producer,
            settings=settings,
        )
        commit_map = await tracker.complete_and_build_commit(tp, record.offset)
        await consumer.commit(commit_map)
    except Exception as e:
        logger.exception(
            "unified worker unhandled error topic=%s partition=%s offset=%s err=%r",
            record.topic, record.partition, record.offset, e,
        )
    finally:
        semaphore.release()


async def run_unified_worker() -> None:
    """Точка входа: один consumer (одна группа, все топики), один producer, цикл с маршрутизацией по топику."""
    settings = get_settings()
    start_http_server(settings.UNIFIED_WORKER_METRICS_PORT)
    topics = _all_topics(settings)
    logger.info(
        "unified worker startup group=%r topics=%s",
        settings.KAFKA_GROUP_ID,
        topics,
    )

    stop_event = asyncio.Event()

    def _stop(*_: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    consumer = create_consumer(
        *topics,
        group_id=settings.KAFKA_GROUP_ID,
        settings=settings,
        max_poll_records=settings.KAFKA_MAX_BATCH,
        request_timeout_ms=60_000,
        session_timeout_ms=30_000,
    )
    producer = create_producer(settings=settings)

    resolver = RegResolver()
    await resolver.startup()

    tracker = OffsetTracker()
    semaphore = asyncio.Semaphore(settings.KAFKA_MAX_CONCURRENCY)
    active_tasks: set[asyncio.Task[None]] = set()

    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT,
        follow_redirects=True,
        headers=_default_headers(),
    ) as http_client:
        auth_client = AuthClient(http_client=http_client, auth_service_url=settings.AUTH_SERVICE_URL)
        catalog_client = CatalogClient(http_client=http_client)
        user_service = UserService(
            auth_client=auth_client,
            catalog_client=catalog_client,
            reg_resolver=resolver,
        )

        await producer.start()
        await consumer.start()
        try:
            while not stop_event.is_set():
                batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
                if not batch:
                    continue

                for partition, records in batch.items():
                    for record in records:
                        await semaphore.acquire()
                        await tracker.register(partition, record.offset)
                        task = asyncio.create_task(
                            _process_one_record(
                                record,
                                user_service=user_service,
                                resolver=resolver,
                                catalog_client=catalog_client,
                                http_client=http_client,
                                producer=producer,
                                consumer=consumer,
                                tracker=tracker,
                                semaphore=semaphore,
                                settings=settings,
                            ),
                        )
                        active_tasks.add(task)
                        task.add_done_callback(active_tasks.discard)

                if active_tasks:
                    await asyncio.gather(*active_tasks, return_exceptions=True)
        finally:
            await consumer.stop()
            await producer.stop()
            await resolver.shutdown()
