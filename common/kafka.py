"""
Единая точка создания Kafka consumer и producer.

Все воркеры (users, init_company, reg_company) создают клиентов через create_consumer()
и create_producer(). Параметры подключения (bootstrap_servers) и общие настройки
берутся из get_settings().
"""
from __future__ import annotations

import json
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from common.config import Settings, get_settings


def _json_deserializer(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def _json_serializer(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def unwrap_payload(value: Any) -> Any:
    """
    Если сообщение в формате { event_id, event_type, payload }, вернуть payload;
    иначе вернуть value как есть (обратная совместимость с «плоским» телом).
    """
    if isinstance(value, dict) and "payload" in value:
        return value["payload"]
    return value


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
    return AIOKafkaConsumer(
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
