"""
Тесты обработки сообщений Kafka: common.kafka, users worker, init_company, reg_company.

- common.kafka: сериализация/десериализация, create_consumer/create_producer передают settings.
- users worker: валидный payload → сервис; невалидный/ошибка валидации → DLQ.
- init_company: невалидный payload → DLQ.
- reg_company: валидный/невалидный payload — без падения.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.kafka import create_consumer, create_producer, unwrap_payload
from services.infoboards_service.init_company_worker import _handle_with_retries as init_company_handle
from services.reg_company_service.worker import _process_one as reg_company_process_one
from services.users_service.dto import CreateUserDTO
from services.users_service.service import UserService
from services.users_service.worker import _handle_with_retries as users_handle
from tests.conftest import StubAuthClient, StubCatalogClient, StubRegResolver


# ----- common.kafka: десериализация/сериализация -----


def test_kafka_json_roundtrip() -> None:
    """Сериализатор и десериализатор в common.kafka дают обратимый результат."""
    from common.kafka import _json_deserializer, _json_serializer
    payload = {"reg": "123", "uid": "u1", "psw": "secret", "list": [1, 2]}
    raw = _json_serializer(payload)
    assert isinstance(raw, bytes)
    back = _json_deserializer(raw)
    assert back == payload


def test_kafka_deserializer_invalid_json_raises() -> None:
    """Невалидный JSON в сообщении — десериализатор бросает исключение (воркер должен обработать)."""
    from common.kafka import _json_deserializer
    with pytest.raises((json.JSONDecodeError, ValueError)):
        _json_deserializer(b"not json {")


def test_kafka_unwrap_payload_envelope() -> None:
    """unwrap_payload извлекает payload из обёртки { event_id, event_type, payload }."""
    envelope = {"event_id": "e1", "event_type": "disable_reg_company", "payload": {"reg": "123", "companyName": "OOO"}}
    assert unwrap_payload(envelope) == {"reg": "123", "companyName": "OOO"}


def test_kafka_unwrap_payload_json_string() -> None:
    """unwrap_payload декодирует payload из JSON-строки."""
    envelope = {"event_id": "e2", "event_type": "create-user", "payload": "{\"reg\": \"456\", \"uid\": \"u1\", \"psw\": \"p\"}"}
    assert unwrap_payload(envelope) == {"reg": "456", "uid": "u1", "psw": "p"}


def test_kafka_unwrap_payload_flat_passthrough() -> None:
    """unwrap_payload возвращает value как есть, если нет ключа payload (обратная совместимость)."""
    flat = {"reg": "123", "companyName": "OOO"}
    assert unwrap_payload(flat) is flat


def test_kafka_create_consumer_uses_settings() -> None:
    """create_consumer передаёт в AIOKafkaConsumer bootstrap_servers и max_poll_records из settings."""
    from unittest.mock import patch
    settings = MagicMock()
    settings.KAFKA_BOOTSTRAP_SERVERS = "broker:9092"
    settings.KAFKA_MAX_BATCH = 100
    with patch("common.kafka.AIOKafkaConsumer") as mock_consumer:
        create_consumer("topic-a", group_id="g1", settings=settings)
        mock_consumer.assert_called_once()
        call_kw = mock_consumer.call_args[1]
        assert call_kw["bootstrap_servers"] == "broker:9092"
        assert call_kw["group_id"] == "g1"
        assert call_kw["max_poll_records"] == 100
        assert call_kw["value_deserializer"] is not None
    with patch("common.kafka.AIOKafkaConsumer") as mock_consumer:
        create_consumer("topic-b", group_id="g2", settings=settings, max_poll_records=50)
        assert mock_consumer.call_args[1]["max_poll_records"] == 50


def test_kafka_create_producer_uses_settings() -> None:
    """create_producer передаёт в AIOKafkaProducer bootstrap_servers из переданного settings."""
    from unittest.mock import patch
    settings = MagicMock()
    settings.KAFKA_BOOTSTRAP_SERVERS = "broker:9092"
    with patch("common.kafka.AIOKafkaProducer") as mock_producer:
        create_producer(settings=settings)
        mock_producer.assert_called_once()
        call_kw = mock_producer.call_args[1]
        assert call_kw["bootstrap_servers"] == "broker:9092"
        assert call_kw["value_serializer"] is not None


# ----- users worker: _handle_with_retries -----


def _fake_record(topic: str, value: object, partition: int = 0, offset: int = 0) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, partition=partition, offset=offset, value=value)


@pytest.fixture
def users_worker_settings() -> MagicMock:
    s = MagicMock()
    s.KAFKA_CREATE_TOPIC = "create-user"
    s.KAFKA_UPDATE_TOPIC = "update-user"
    s.KAFKA_UPDATE_DEPARTMENTS_TOPIC = "update-user-departments"
    s.KAFKA_DLQ_TOPIC = "users-dlq"
    s.USERS_RETRY_ATTEMPTS = 2
    s.USERS_RETRY_BASE_DELAY = 0.01
    s.USERS_RETRY_MAX_DELAY = 0.1
    return s


@pytest.fixture
def stub_user_service() -> UserService:
    return UserService(
        auth_client=StubAuthClient(),
        catalog_client=StubCatalogClient(),
        reg_resolver=StubRegResolver(),
    )


@pytest.mark.asyncio
async def test_handle_with_retries_valid_create_user_calls_service(
    stub_user_service: UserService,
    users_worker_settings: MagicMock,
) -> None:
    """Валидный payload create-user — вызывается service.handle_create, в DLQ ничего не уходит."""
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    record = _fake_record(
        users_worker_settings.KAFKA_CREATE_TOPIC,
        {"reg": "350832", "uid": "kafka_user", "psw": "pass"},
    )
    await users_handle(record, stub_user_service, producer, users_worker_settings)
    assert stub_user_service.catalog_client.users.get("kafka_user") is not None
    assert stub_user_service.catalog_client.users["kafka_user"]["psw"] == "pass"
    producer.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_handle_with_retries_invalid_payload_sends_dlq(
    stub_user_service: UserService,
    users_worker_settings: MagicMock,
) -> None:
    """Payload не dict — отправка в DLQ с INVALID_PAYLOAD, сервис не вызывается."""
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    record = _fake_record(users_worker_settings.KAFKA_CREATE_TOPIC, "not a dict")
    await users_handle(record, stub_user_service, producer, users_worker_settings)
    assert producer.send_and_wait.call_count == 1
    call_args = producer.send_and_wait.call_args
    assert call_args[0][0] == users_worker_settings.KAFKA_DLQ_TOPIC
    msg = call_args[0][1]
    assert msg["error_code"] == "INVALID_PAYLOAD"
    assert "Payload is not an object" in msg["error_message"]


@pytest.mark.asyncio
async def test_handle_with_retries_validation_error_sends_dlq(
    stub_user_service: UserService,
    users_worker_settings: MagicMock,
) -> None:
    """Невалидный DTO (нет обязательных полей) — отправка в DLQ с VALIDATION_ERROR."""
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    record = _fake_record(users_worker_settings.KAFKA_CREATE_TOPIC, {"reg": "123"})  # нет uid, psw
    await users_handle(record, stub_user_service, producer, users_worker_settings)
    assert producer.send_and_wait.call_count == 1
    msg = producer.send_and_wait.call_args[0][1]
    assert msg["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_handle_with_retries_valid_update_topic_calls_service(
    stub_user_service: UserService,
    users_worker_settings: MagicMock,
) -> None:
    """Валидный payload update-user — вызывается service.handle_update."""
    # сначала создаём пользователя
    await stub_user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "u2", "psw": "p"})
    )
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    record = _fake_record(
        users_worker_settings.KAFKA_UPDATE_TOPIC,
        {"reg": "350832", "uid": "u2", "psw": "p", "mail": "a@b.c"},
    )
    await users_handle(record, stub_user_service, producer, users_worker_settings)
    assert stub_user_service.catalog_client.users["u2"].get("mail") == "a@b.c"
    producer.send_and_wait.assert_not_called()


# ----- init_company worker: невалидный payload → DLQ -----


@pytest.mark.asyncio
async def test_init_company_handle_invalid_payload_sends_dlq() -> None:
    """init_company: payload не dict — отправка в DLQ, без исключения."""
    settings = MagicMock()
    settings.KAFKA_INIT_COMPANY_TOPIC = "init_company"
    settings.KAFKA_INIT_COMPANY_DLQ_TOPIC = "init_company-dlq"
    settings.USERS_RETRY_ATTEMPTS = 1
    settings.USERS_RETRY_BASE_DELAY = 0.01
    settings.USERS_RETRY_MAX_DELAY = 0.1
    record = SimpleNamespace(topic="init_company", partition=0, offset=0, value="not a dict")
    resolver = StubRegResolver()
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()

    await init_company_handle(
        record,
        resolver=resolver,
        catalog_client=MagicMock(),
        http_client=MagicMock(),
        producer=producer,
        settings=settings,
    )
    assert producer.send_and_wait.call_count == 1
    msg = producer.send_and_wait.call_args[0][1]
    assert msg["error_code"] == "INVALID_PAYLOAD"
    assert producer.send_and_wait.call_args[0][0] == settings.KAFKA_INIT_COMPANY_DLQ_TOPIC


def test_reg_company_process_one_valid_enable_no_raise() -> None:
    """Валидный payload enable_reg_company — без исключения."""
    settings = MagicMock()
    settings.KAFKA_ENABLE_REG_COMPANY_TOPIC = "enable_reg_company"
    settings.KAFKA_DISABLE_REG_COMPANY_TOPIC = "disable_reg_company"
    record = SimpleNamespace(topic="enable_reg_company", offset=0, value={"reg": "123", "oldReg": "456", "companyName": "OOO"})
    reg_company_process_one(record, settings)


def test_reg_company_process_one_valid_disable_no_raise() -> None:
    """Валидный payload disable_reg_company — без исключения."""
    settings = MagicMock()
    settings.KAFKA_ENABLE_REG_COMPANY_TOPIC = "enable_reg_company"
    settings.KAFKA_DISABLE_REG_COMPANY_TOPIC = "disable_reg_company"
    record = SimpleNamespace(topic="disable_reg_company", offset=1, value={"reg": "123", "companyName": "OOO"})
    reg_company_process_one(record, settings)


def test_reg_company_process_one_invalid_payload_not_dict_no_raise() -> None:
    """Payload не dict — логируем warning, не падаем."""
    settings = MagicMock()
    settings.KAFKA_ENABLE_REG_COMPANY_TOPIC = "enable_reg_company"
    settings.KAFKA_DISABLE_REG_COMPANY_TOPIC = "disable_reg_company"
    record = SimpleNamespace(topic="enable_reg_company", offset=0, value=None)
    reg_company_process_one(record, settings)


def test_reg_company_process_one_validation_error_no_raise() -> None:
    """Невалидный payload (нет полей) — логируем warning, не падаем."""
    settings = MagicMock()
    settings.KAFKA_ENABLE_REG_COMPANY_TOPIC = "enable_reg_company"
    settings.KAFKA_DISABLE_REG_COMPANY_TOPIC = "disable_reg_company"
    record = SimpleNamespace(topic="enable_reg_company", offset=0, value={})
    reg_company_process_one(record, settings)
