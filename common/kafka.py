"""
Единая точка создания Kafka consumer и producer.

Все воркеры (users, init_company, reg_company) создают клиентов через create_consumer()
и create_producer(). Параметры подключения (bootstrap_servers) и общие настройки
берутся из get_settings().
"""
from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRebalanceListener
from aiokafka.structs import OffsetAndMetadata

from common.config import Settings, get_settings


@dataclass(frozen=True)
class MalformedJSON:
    """A failed decode remains a record which can be durably sent to DLQ."""
    raw_base64: str
    error: str


def _json_deserializer(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return MalformedJSON(base64.b64encode(raw).decode("ascii"), str(error))


def dlq_payload(value: Any) -> Any:
    if isinstance(value, MalformedJSON):
        return {"raw_base64": value.raw_base64, "decode_error": value.error}
    return value


async def send_record_dlq(producer, topic: str, record, code: str, message: str) -> None:
    """Commit is allowed only after this durable send has returned successfully."""
    await producer.send_and_wait(topic, {
        "original_message": dlq_payload(record.value),
        "error_code": code,
        "error_message": message,
        **record_coordinates(record),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def record_coordinates(record) -> dict[str, Any]:
    if record is None:
        return {}
    return {
        "source_topic": getattr(record, "source_topic", record.topic),
        "source_partition": record.partition,
        "source_offset": record.offset,
        "array_index": getattr(record, "array_index", None),
    }


@dataclass
class _PartitionState:
    committed: int
    pending: deque[int] = field(default_factory=deque)
    completed: set[int] = field(default_factory=set)


class OffsetTracker:
    """Track delivered records, not numeric gaps which Kafka may never return.

    Registrations must be in delivery order, before concurrent completion. The
    worker must terminate on a commit failure; these are proposed frontiers, not
    proof that Kafka acknowledged them. A new assignment needs a fresh tracker.
    """
    def __init__(self) -> None:
        self._states = {}
        self._lock = asyncio.Lock()

    async def register(self, partition, offset: int) -> None:
        async with self._lock:
            state = self._states.setdefault(partition, _PartitionState(offset - 1))
            if offset in state.pending:
                return
            if offset <= state.committed or (state.pending and offset <= state.pending[-1]):
                raise RuntimeError("Offsets must be registered in delivery order")
            state.pending.append(offset)

    async def complete_and_build_commit(self, partition, offset: int):
        async with self._lock:
            state = self._states.get(partition)
            if state is None or offset not in state.pending:
                raise RuntimeError("Cannot complete an unregistered Kafka record")
            state.completed.add(offset)
            while state.pending and state.pending[0] in state.completed:
                state.committed = state.pending.popleft()
                state.completed.remove(state.committed)
            return {partition: OffsetAndMetadata(state.committed + 1, "")}


class _AssignmentGuard(ConsumerRebalanceListener):
    def __init__(self):
        self.epoch = 0

    async def on_partitions_revoked(self, revoked):
        self.epoch += 1

    async def on_partitions_assigned(self, assigned):
        self.epoch += 1


def get_assignment_epoch(consumer) -> int:
    guard = getattr(consumer, "_integration_assignment_guard", None)
    return guard.epoch if isinstance(guard, _AssignmentGuard) else 0


def ensure_partition_assigned(consumer, partition, expected_epoch: int | None = None) -> None:
    """Do not write/commit a partition already revoked from this consumer.

    Kafka commit still supplies the final group-generation check. This is not
    fencing of an external HTTP call which was already in progress at revoke.
    """
    if (expected_epoch is not None and expected_epoch != get_assignment_epoch(consumer)) or partition not in consumer.assignment():
        raise RuntimeError(f"Kafka partition was revoked: {partition}")


async def process_partition_batch(batch, process_record) -> None:
    """In-order effects per partition; fail fast and await cancellation on error."""
    async def process_partition(partition, records):
        for record in records:
            await process_record(partition, record)

    async with asyncio.TaskGroup() as group:
        for partition, records in batch.items():
            group.create_task(process_partition(partition, records))


def _json_serializer(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def unwrap_payload(value: Any) -> Any:
    """
    Если сообщение в формате { event_id, event_type, payload }, вернуть payload (декодированный из JSON, если строка);
    иначе вернуть value как есть (обратная совместимость с «плоским» телом).
    """
    if isinstance(value, dict) and "payload" in value:
        raw_payload = value["payload"]
        if isinstance(raw_payload, str):
            return _json_deserializer(raw_payload.encode("utf-8"))
        return raw_payload
    return value


def event_type_from_message(value: Any) -> str | None:
    """Извлекает event_type из сообщения Kafka: верхний уровень или внутри payload."""
    if not isinstance(value, dict):
        return None
    if "event_type" in value:
        return str(value["event_type"])
    if "payload" not in value:
        return None
    payload = value["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if isinstance(payload, dict) and "event_type" in payload:
        return str(payload["event_type"])
    return None


def create_consumer(
    *topics: str,
    group_id: str,
    settings: Settings | None = None,
    max_poll_records: int | None = None,
    request_timeout_ms: int = 40_000,
    session_timeout_ms: int = 15_000,
    **kwargs: Any,
) -> AIOKafkaConsumer:
    """
    Создаёт AIOKafkaConsumer с общими настройками (bootstrap из конфига, json десериализация).
    Дополнительные kwargs передаются в AIOKafkaConsumer.
    """
    s = settings or get_settings()
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=s.KAFKA_BOOTSTRAP_SERVERS,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=_json_deserializer,
        max_poll_records=max_poll_records if max_poll_records is not None else s.KAFKA_MAX_BATCH,
        request_timeout_ms=request_timeout_ms,
        session_timeout_ms=session_timeout_ms,
        **kwargs,
    )
    if topics:
        guard = _AssignmentGuard()
        consumer._integration_assignment_guard = guard
        consumer.subscribe(topics=list(topics), listener=guard)
    return consumer


def create_producer(
    settings: Settings | None = None,
    request_timeout_ms: int = 30_000,
    **kwargs: Any,
) -> AIOKafkaProducer:
    """
    Создаёт AIOKafkaProducer с общими настройками (bootstrap из конфига, json сериализация).
    Дополнительные kwargs передаются в AIOKafkaProducer.
    """
    s = settings or get_settings()
    return AIOKafkaProducer(
        bootstrap_servers=s.KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=_json_serializer,
        request_timeout_ms=request_timeout_ms,
        **kwargs,
    )
