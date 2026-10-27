"""Reject unimplemented registration operations explicitly and retain them in DLQ.

No external registration enable/disable operation is implemented here. Successful
Kafka consumption means only that the explicit failure was durably published.
"""
from __future__ import annotations

import asyncio
import signal
from typing import Any

from aiokafka.structs import OffsetAndMetadata
from pydantic import ValidationError
from prometheus_client import start_http_server

from common.config import get_settings
from common.exceptions import ParseError
from common.kafka import (
    MalformedJSON, create_consumer, create_producer, unwrap_payload,
    ensure_partition_assigned, get_assignment_epoch, process_partition_batch, send_record_dlq,
)
from common.logger import get_logger, set_trace_id
from services.reg_company_service.dto import DisableRegCompanyDTO, EnableRegCompanyDTO


logger = get_logger('reg_company.worker')


def _process_one(record: Any, settings: Any) -> None:
    """Validate the input; never claim the missing business action succeeded."""
    topic = record.topic
    payload = unwrap_payload(record.value)
    set_trace_id(f'reg_company-{topic}-{record.offset}')
    if isinstance(payload, MalformedJSON):
        raise ParseError(code='INVALID_JSON', message=payload.error)
    if not isinstance(payload, dict):
        raise ParseError(code='INVALID_PAYLOAD', message='Payload is not an object')
    try:
        if topic == settings.KAFKA_ENABLE_REG_COMPANY_TOPIC:
            EnableRegCompanyDTO.model_validate(payload)
        elif topic == settings.KAFKA_DISABLE_REG_COMPANY_TOPIC:
            DisableRegCompanyDTO.model_validate(payload)
        else:
            raise ParseError(code='UNKNOWN_TOPIC', message=f'Unsupported registration event: {topic}')
    except ValidationError as error:
        raise ParseError(code='VALIDATION_ERROR', message=str(error)) from error
    raise ParseError(
        code='NOT_IMPLEMENTED',
        message=f'Registration operation {topic} has no confirmed implementation',
    )


async def _handle_record(record, producer, settings):
    try:
        _process_one(record, settings)
    except ParseError as error:
        await send_record_dlq(producer, settings.KAFKA_REG_COMPANY_DLQ_TOPIC, record, error.code, error.message)


async def _process_record(record, partition, *, producer, consumer, settings):
    epoch = get_assignment_epoch(consumer)
    ensure_partition_assigned(consumer, partition, epoch)
    await _handle_record(record, producer, settings)
    ensure_partition_assigned(consumer, partition, epoch)
    await consumer.commit({partition: OffsetAndMetadata(record.offset + 1, '')})


async def run_worker() -> None:
    settings = get_settings()
    start_http_server(settings.REG_COMPANY_METRICS_PORT)
    logger.info('reg_company worker rejects unsupported actions into %s', settings.KAFKA_REG_COMPANY_DLQ_TOPIC)
    stop_event = asyncio.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    consumer = create_consumer(
        settings.KAFKA_ENABLE_REG_COMPANY_TOPIC, settings.KAFKA_DISABLE_REG_COMPANY_TOPIC,
        group_id=settings.KAFKA_REG_COMPANY_GROUP_ID, settings=settings,
        max_poll_records=min(50, settings.KAFKA_MAX_BATCH),
        request_timeout_ms=60_000, session_timeout_ms=30_000,
    )
    producer = create_producer(settings=settings)
    try:
        await producer.start()
        await consumer.start()
        while not stop_event.is_set():
            batch = await consumer.getmany(timeout_ms=settings.KAFKA_POLL_TIMEOUT_MS)
            if not batch:
                continue
            batch_epoch = get_assignment_epoch(consumer)
            async def process(partition, record):
                ensure_partition_assigned(consumer, partition, batch_epoch)
                await _process_record(record, partition, producer=producer, consumer=consumer, settings=settings)
            await process_partition_batch(batch, process)
    finally:
        try:
            await consumer.stop()
        finally:
            await producer.stop()
