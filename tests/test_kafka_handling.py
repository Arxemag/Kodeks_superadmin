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

from common.kafka import create_consumer, create_producer, event_type_from_message, unwrap_payload
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


@pytest.mark.parametrize("raw", [b"not json {", b"\xff\xfe"])
def test_kafka_deserializer_retains_invalid_bytes_for_dlq(raw) -> None:
    import base64
    from common.kafka import MalformedJSON, _json_deserializer
    malformed = _json_deserializer(raw)
    assert isinstance(malformed, MalformedJSON)
    assert base64.b64decode(malformed.raw_base64) == raw
    assert malformed.error


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


def test_kafka_event_type_from_message_top_level() -> None:
    """event_type извлекается с верхнего уровня сообщения."""
    msg = {"event_id": "e1", "event_type": "create-user", "payload": {"reg": "350832", "uid": "u1"}}
    assert event_type_from_message(msg) == "create-user"


def test_kafka_event_type_from_message_inside_payload() -> None:
    """event_type извлекается из вложенного payload (dict)."""
    msg = {"event_id": "e2", "payload": {"event_type": "init_company", "reg": "123", "id": 67}}
    assert event_type_from_message(msg) == "init_company"


def test_kafka_event_type_from_message_no_event_type_returns_none() -> None:
    """Без event_type возвращается None."""
    assert event_type_from_message({"payload": {"reg": "123"}}) is None
    assert event_type_from_message({"reg": "123"}) is None


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


@pytest.mark.parametrize('topic,payload,expected', [
    ('enable_reg_company', {'reg': '123', 'oldReg': '456', 'companyName': 'OOO'}, 'NOT_IMPLEMENTED'),
    ('disable_reg_company', {'reg': '123', 'companyName': 'OOO'}, 'NOT_IMPLEMENTED'),
    ('enable_reg_company', None, 'INVALID_PAYLOAD'),
    ('enable_reg_company', {}, 'VALIDATION_ERROR'),
])
def test_reg_company_never_claims_unsupported_action_succeeded(topic, payload, expected):
    from common.exceptions import ParseError
    settings = SimpleNamespace(KAFKA_ENABLE_REG_COMPANY_TOPIC='enable_reg_company',
                               KAFKA_DISABLE_REG_COMPANY_TOPIC='disable_reg_company')
    record = _fake_record(topic, payload)
    with pytest.raises(ParseError) as error:
        reg_company_process_one(record, settings)
    assert error.value.code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('handler_name', ['users', 'init_company', 'correct_cabinet'])
@pytest.mark.parametrize('raw', [b'not-json', b'\xff', b'{"payload":"not-json"}'])
async def test_malformed_json_reaches_handler_dlq_as_serializable_bytes(handler_name, raw):
    import base64
    from common.kafka import _json_deserializer, _json_serializer
    from services.infoboards_service.correct_cabinet_worker import _handle_with_retries as correct_handle
    settings = MagicMock()
    settings.KAFKA_DLQ_TOPIC = 'users-dlq'
    settings.KAFKA_INIT_COMPANY_DLQ_TOPIC = 'init-dlq'
    settings.KAFKA_CORRECT_CABINET_DLQ_TOPIC = 'correct-dlq'
    producer = AsyncMock()
    record = _fake_record('test-topic', _json_deserializer(raw))
    if handler_name == 'users':
        await users_handle(record, MagicMock(), producer, settings)
    elif handler_name == 'init_company':
        await init_company_handle(record, MagicMock(), MagicMock(), MagicMock(), producer, settings)
    else:
        await correct_handle(record, MagicMock(), MagicMock(), producer, settings)
    message = producer.send_and_wait.await_args.args[1]
    assert message['error_code'] == 'INVALID_JSON'
    _json_serializer(message)  # Kafka's real serializer must accept the DLQ body.
    expected = b'not-json' if raw.startswith(b'{') else raw
    assert base64.b64decode(message['original_message']['raw_base64']) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_name', ['users', 'init_company', 'correct_cabinet'])
async def test_standalone_dlq_failure_never_commits_or_swallows(worker_name):
    import asyncio
    from aiokafka import TopicPartition
    from common.kafka import OffsetTracker
    from services.users_service.worker import _process_record as process_users
    from services.infoboards_service.init_company_worker import _process_record as process_init
    from services.infoboards_service.correct_cabinet_worker import _process_record as process_correct
    record = _fake_record('topic', 'invalid-payload', offset=5)
    tp = TopicPartition(record.topic, record.partition)
    settings = MagicMock()
    settings.KAFKA_DLQ_TOPIC = 'users-dlq'
    settings.KAFKA_INIT_COMPANY_DLQ_TOPIC = 'init-dlq'
    settings.KAFKA_CORRECT_CABINET_DLQ_TOPIC = 'correct-dlq'
    producer = AsyncMock()
    producer.send_and_wait.side_effect = RuntimeError('DLQ is unavailable')
    consumer = MagicMock()
    consumer.assignment.return_value = {tp}
    consumer.commit = AsyncMock()
    tracker = OffsetTracker()
    await tracker.register(tp, record.offset)
    common = dict(producer=producer, consumer=consumer, settings=settings)
    with pytest.raises(RuntimeError, match='DLQ is unavailable'):
        if worker_name == 'users':
            await process_users(record=record, service=MagicMock(), tracker=tracker,
                                semaphore=asyncio.Semaphore(0), **common)
        elif worker_name == 'init_company':
            await process_init(record=record, resolver=MagicMock(), catalog_client=MagicMock(),
                               http_client=MagicMock(), tracker=tracker,
                               semaphore=asyncio.Semaphore(0), **common)
        else:
            await process_correct(record, tp, resolver=MagicMock(), http_client=MagicMock(), **common)
    consumer.commit.assert_not_awaited()
    producer.send_and_wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_correct_cabinet_dry_run_has_no_success_event_and_uses_catalog_lock():
    from contextlib import asynccontextmanager
    from services.infoboards_service.correct_cabinet_worker import _handle_with_retries as correct_handle
    held = False
    @asynccontextmanager
    async def operation(base_url):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False
    @asynccontextmanager
    async def session():
        yield MagicMock()
    resolver = MagicMock()
    resolver.resolve_base_url = AsyncMock(return_value='https://catalog.test')
    resolver.with_session = session
    settings = SimpleNamespace(KAFKA_CORRECT_CABINET_DLQ_TOPIC='correct-dlq',
                               KAFKA_CABINET_CORRECTED_TOPIC='done', USERS_RETRY_ATTEMPTS=1,
                               CORRECT_CABINET_DRY_RUN=True)
    record = _fake_record('correct-cabinet', {'reg': '123', 'link': '?infoboard=one', 'questions': []})
    async def plan(*args, **kwargs):
        assert held
        assert kwargs['dry_run'] is True
        return []
    producer = AsyncMock()
    with patch('services.infoboards_service.correct_cabinet_worker.catalog_operation', operation), \
         patch('services.infoboards_service.correct_cabinet_worker.AuthService') as auth, \
         patch('services.infoboards_service.correct_cabinet_worker.correct_cabinet', side_effect=plan):
        auth.return_value.login = AsyncMock(return_value={})
        await correct_handle(record, resolver, MagicMock(), producer, settings)
    assert not held
    producer.send_and_wait.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,expected_code', [
    ({'reg': '123', 'oldReg': '456', 'companyName': 'OOO'}, 'NOT_IMPLEMENTED'),
    ({}, 'VALIDATION_ERROR'),
    (None, 'INVALID_PAYLOAD'),
])
async def test_registration_failure_reaches_dlq_before_commit(payload, expected_code):
    from aiokafka import TopicPartition
    from services.reg_company_service.worker import _process_record
    trace = []
    settings = SimpleNamespace(KAFKA_ENABLE_REG_COMPANY_TOPIC='enable_reg_company',
                               KAFKA_DISABLE_REG_COMPANY_TOPIC='disable_reg_company',
                               KAFKA_REG_COMPANY_DLQ_TOPIC='reg-company-dlq')
    record = _fake_record('enable_reg_company', payload, offset=33)
    partition = TopicPartition(record.topic, record.partition)
    async def publish(topic, message):
        assert topic == 'reg-company-dlq'
        assert message['error_code'] == expected_code
        assert message['source_offset'] == 33
        trace.append('dlq')
    async def commit(offsets):
        assert offsets[partition].offset == 34
        trace.append('commit')
    consumer = MagicMock()
    consumer.assignment.return_value = {partition}
    consumer.commit = AsyncMock(side_effect=commit)
    producer = AsyncMock()
    producer.send_and_wait.side_effect = publish
    await _process_record(record, partition, producer=producer, consumer=consumer, settings=settings)
    assert trace == ['dlq', 'commit']


@pytest.mark.asyncio
async def test_registration_dlq_failure_is_fatal_without_commit():
    from aiokafka import TopicPartition
    from services.reg_company_service.worker import _process_record
    settings = SimpleNamespace(KAFKA_ENABLE_REG_COMPANY_TOPIC='enable_reg_company',
                               KAFKA_DISABLE_REG_COMPANY_TOPIC='disable_reg_company',
                               KAFKA_REG_COMPANY_DLQ_TOPIC='reg-company-dlq')
    record = _fake_record('enable_reg_company', {'reg': '123', 'oldReg': '456', 'companyName': 'OOO'}, offset=33)
    partition = TopicPartition(record.topic, record.partition)
    consumer = MagicMock()
    consumer.assignment.return_value = {partition}
    consumer.commit = AsyncMock()
    producer = AsyncMock()
    producer.send_and_wait.side_effect = RuntimeError('DLQ unavailable')
    with pytest.raises(RuntimeError, match='DLQ unavailable'):
        await _process_record(record, partition, producer=producer, consumer=consumer, settings=settings)
    consumer.commit.assert_not_awaited()
