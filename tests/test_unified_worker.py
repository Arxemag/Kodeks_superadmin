"""
Тесты единого Kafka worker: одна группа, все топики, маршрутизация по record.topic.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.kafka import event_type_from_message
from services.unified_worker import _all_topics, _dispatch_record, _effective_topic, _records_from_value
from services.users_service.dto import CreateUserDTO
from services.users_service.service import UserService
from tests.conftest import StubAuthClient, StubCatalogClient, StubRegResolver


def _fake_record(topic: str, value: object, partition: int = 0, offset: int = 0) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, partition=partition, offset=offset, value=value)


@pytest.fixture
def unified_settings() -> MagicMock:
    """Настройки с топиками для единого воркера."""
    s = MagicMock()
    s.KAFKA_CREATE_TOPIC = "create-user"
    s.KAFKA_UPDATE_TOPIC = "update-user"
    s.KAFKA_UPDATE_DEPARTMENTS_TOPIC = "update-user-departments"
    s.KAFKA_INIT_COMPANY_TOPIC = "init_company"
    s.KAFKA_SYNC_DEPARTMENTS_TOPIC = "sync_departments"
    s.KAFKA_ENABLE_REG_COMPANY_TOPIC = "enable_reg_company"
    s.KAFKA_DISABLE_REG_COMPANY_TOPIC = "disable_reg_company"
    s.KAFKA_CORRECT_CABINET_TOPIC = "correct-cabinet"
    s.KAFKA_DLQ_TOPIC = "users-dlq"
    s.KAFKA_REG_COMPANY_DLQ_TOPIC = "reg-company-dlq"
    s.KAFKA_INIT_COMPANY_DLQ_TOPIC = "init_company-dlq"
    s.USERS_RETRY_ATTEMPTS = 2
    s.USERS_RETRY_BASE_DELAY = 0.01
    s.USERS_RETRY_MAX_DELAY = 0.1
    return s


def test_all_topics_returns_all_topics(unified_settings: MagicMock) -> None:
    """_all_topics возвращает 8 топиков в нужном порядке (включая sync_departments и correct_cabinet)."""
    topics = _all_topics(unified_settings)
    assert len(topics) == 8
    assert topics[0] == "create-user"
    assert topics[1] == "update-user"
    assert topics[2] == "update-user-departments"
    assert topics[3] == "init_company"
    assert topics[4] == "sync_departments"
    assert topics[5] == "enable_reg_company"
    assert topics[6] == "disable_reg_company"
    assert topics[7] == "correct-cabinet"


def test_effective_topic_from_record_topic() -> None:
    """Без event_type в теле используется record.topic."""
    record = _fake_record("init_company", {"reg": "123"})
    assert _effective_topic(record) == "init_company"


def test_effective_topic_from_event_type() -> None:
    """При наличии event_type в теле используется он для маршрутизации."""
    record = _fake_record("any_topic", {"event_id": "e1", "event_type": "init_company", "payload": {"reg": "123"}})
    assert _effective_topic(record) == "init_company"


def test_effective_topic_from_event_type_inside_payload() -> None:
    """event_type только внутри payload — используется для маршрутизации."""
    record = _fake_record(
        "other_topic",
        {"event_id": "e1", "payload": {"event_type": "init_company", "reg": "123", "companyName": "OOO"}},
    )
    assert _effective_topic(record) == "init_company"
    assert event_type_from_message(record.value) == "init_company"


def test_records_from_value_single() -> None:
    """Обычное сообщение (не массив) — один record."""
    record = _fake_record("create-user", {"reg": "1", "uid": "u", "psw": "p"})
    out = _records_from_value(record)
    assert len(out) == 1
    assert out[0] is record


def test_records_from_value_array() -> None:
    """Массив событий — по одному виртуальному record на элемент, topic из event_type."""
    record = _fake_record(
        "single_topic",
        [
            {"event_type": "init_company", "payload": {"reg": "123"}},
            {"event_type": "disable_reg_company", "payload": {"reg": "456", "companyName": "OOO"}},
        ],
    )
    out = _records_from_value(record)
    assert len(out) == 2
    assert out[0].topic == "init_company"
    assert out[0].value == {"event_type": "init_company", "payload": {"reg": "123"}}
    assert out[1].topic == "disable_reg_company"
    assert out[1].value == {"event_type": "disable_reg_company", "payload": {"reg": "456", "companyName": "OOO"}}


@pytest.fixture
def stub_user_service() -> UserService:
    return UserService(
        auth_client=StubAuthClient(),
        catalog_client=StubCatalogClient(),
        reg_resolver=StubRegResolver(),
    )


@pytest.mark.asyncio
async def test_dispatch_record_create_user_calls_users_handle(
    stub_user_service: UserService,
    unified_settings: MagicMock,
) -> None:
    """Топик create-user — вызывается users_handle, пользователь создаётся в стабе."""
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    record = _fake_record("create-user", {"reg": "350832", "uid": "u1", "psw": "p"})
    resolver = StubRegResolver()
    catalog = StubCatalogClient()
    http_client = MagicMock()

    await _dispatch_record(
        record,
        user_service=stub_user_service,
        resolver=resolver,
        catalog_client=catalog,
        http_client=http_client,
        producer=producer,
        settings=unified_settings,
    )
    assert stub_user_service.catalog_client.users.get("u1") is not None
    producer.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_record_update_user_calls_users_handle(
    stub_user_service: UserService,
    unified_settings: MagicMock,
) -> None:
    """Топик update-user — вызывается users_handle (handle_update)."""
    await stub_user_service.handle_create(
        CreateUserDTO.model_validate({"reg": "350832", "uid": "u2", "psw": "p"})
    )
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    record = _fake_record("update-user", {"reg": "350832", "uid": "u2", "psw": "p", "mail": "x@y.z"})
    resolver = StubRegResolver()
    catalog = stub_user_service.catalog_client

    await _dispatch_record(
        record,
        user_service=stub_user_service,
        resolver=resolver,
        catalog_client=catalog,
        http_client=MagicMock(),
        producer=producer,
        settings=unified_settings,
    )
    assert catalog.users["u2"].get("mail") == "x@y.z"


@pytest.mark.asyncio
async def test_dispatch_record_init_company_by_event_type(unified_settings: MagicMock) -> None:
    """Сообщение с event_type=init_company в теле маршрутизируется в init_company даже с другим record.topic."""
    record = _fake_record(
        "disable_reg_company",
        {"event_id": "e1", "event_type": "init_company", "payload": {"reg": "123", "companyName": "OOO", "departments": []}},
    )
    with patch("services.unified_worker.init_company_handle", new_callable=AsyncMock) as mock_init:
        await _dispatch_record(
            record,
            user_service=MagicMock(),
            resolver=MagicMock(),
            catalog_client=MagicMock(),
            http_client=MagicMock(),
            producer=AsyncMock(),
            settings=unified_settings,
        )
        mock_init.assert_called_once()
        # В handler передаётся тот же record (value с envelope, unwrap внутри)
        assert mock_init.call_args[0][0].value["event_type"] == "init_company"


@pytest.mark.asyncio
async def test_dispatch_record_init_company_calls_init_company_handle(unified_settings: MagicMock) -> None:
    """Топик init_company — вызывается init_company_handle (мокаем, проверяем вызов)."""
    record = _fake_record("init_company", {"id": 1, "reg": "123", "companyName": "OOO", "departments": []})
    user_service = UserService(
        auth_client=StubAuthClient(),
        catalog_client=StubCatalogClient(),
        reg_resolver=StubRegResolver(),
    )
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    resolver = StubRegResolver()
    catalog_client = MagicMock()

    with patch(
        "services.unified_worker.init_company_handle",
        new_callable=AsyncMock,
    ) as mock_init:
        await _dispatch_record(
            record,
            user_service=user_service,
            resolver=resolver,
            catalog_client=catalog_client,
            http_client=MagicMock(),
            producer=producer,
            settings=unified_settings,
        )
        mock_init.assert_called_once()
        # В unified_worker вызов позиционный: (record, resolver, catalog_client, http_client, producer, settings)
        args = mock_init.call_args[0]
        assert len(args) == 6
        assert args[0] is record
        assert args[1] is resolver
        assert args[4] is producer
        assert args[5] is unified_settings


@pytest.mark.asyncio
async def test_dispatch_record_sync_departments_calls_init_company_handle(unified_settings: MagicMock) -> None:
    """Топик sync_departments — тот же обработчик, что init_company."""
    record = _fake_record("sync_departments", {"id": 1, "reg": "123", "departments": []})
    with patch("services.unified_worker.init_company_handle", new_callable=AsyncMock) as mock_init:
        await _dispatch_record(
            record,
            user_service=UserService(
                auth_client=StubAuthClient(),
                catalog_client=StubCatalogClient(),
                reg_resolver=StubRegResolver(),
            ),
            resolver=StubRegResolver(),
            catalog_client=MagicMock(),
            http_client=MagicMock(),
            producer=AsyncMock(),
            settings=unified_settings,
        )
        mock_init.assert_called_once()
        assert mock_init.call_args[0][0].topic == "sync_departments"


@pytest.mark.asyncio
async def test_dispatch_record_enable_reg_company_retains_not_implemented(unified_settings: MagicMock) -> None:
    """Топик enable_reg_company — вызывается reg_company_process_one, без исключения."""
    record = _fake_record(
        "enable_reg_company",
        {"reg": "123", "oldReg": "456", "companyName": "OOO"},
    )
    user_service = UserService(
        auth_client=StubAuthClient(),
        catalog_client=StubCatalogClient(),
        reg_resolver=StubRegResolver(),
    )
    producer = AsyncMock()

    await _dispatch_record(
        record,
        user_service=user_service,
        resolver=StubRegResolver(),
        catalog_client=MagicMock(),
        http_client=MagicMock(),
        producer=producer,
        settings=unified_settings,
    )
    producer.send_and_wait.assert_awaited_once()
    assert producer.send_and_wait.await_args.args[0] == 'reg-company-dlq'
    assert producer.send_and_wait.await_args.args[1]['error_code'] == 'NOT_IMPLEMENTED'


@pytest.mark.asyncio
async def test_dispatch_record_disable_reg_company_retains_not_implemented(unified_settings: MagicMock) -> None:
    """Топик disable_reg_company — вызывается reg_company_process_one."""
    record = _fake_record("disable_reg_company", {"reg": "123", "companyName": "OOO"})
    user_service = UserService(
        auth_client=StubAuthClient(),
        catalog_client=StubCatalogClient(),
        reg_resolver=StubRegResolver(),
    )
    producer = AsyncMock()

    await _dispatch_record(
        record,
        user_service=user_service,
        resolver=StubRegResolver(),
        catalog_client=MagicMock(),
        http_client=MagicMock(),
        producer=producer,
        settings=unified_settings,
    )

    producer.send_and_wait.assert_awaited_once()
    assert producer.send_and_wait.await_args.args[0] == 'reg-company-dlq'
    assert producer.send_and_wait.await_args.args[1]['error_code'] == 'NOT_IMPLEMENTED'


@pytest.mark.asyncio
async def test_dispatch_record_unknown_topic_must_reach_dlq(unified_settings: MagicMock) -> None:
    """An unknown event must be retained in DLQ before its offset is committed."""
    record = _fake_record("unknown-topic", {"a": 1})
    user_service = UserService(
        auth_client=StubAuthClient(),
        catalog_client=StubCatalogClient(),
        reg_resolver=StubRegResolver(),
    )
    producer = AsyncMock()

    await _dispatch_record(
        record,
        user_service=user_service,
        resolver=StubRegResolver(),
        catalog_client=MagicMock(),
        http_client=MagicMock(),
        producer=producer,
        settings=unified_settings,
    )
    producer.send_and_wait.assert_awaited_once()
    assert producer.send_and_wait.call_args.args[1]["error_code"] == "UNKNOWN_TOPIC"


@pytest.mark.asyncio
async def test_mixed_array_keeps_every_element_and_original_index(unified_settings):
    record = _fake_record('transport-topic', [
        {'event_type': 'unsupported', 'payload': {}}, 'bad', None, 17,
    ], offset=55)
    producer = AsyncMock()
    records = _records_from_value(record)
    assert len(records) == 4
    for item in records:
        await _dispatch_record(item, user_service=MagicMock(), resolver=MagicMock(),
                               catalog_client=MagicMock(), http_client=MagicMock(),
                               producer=producer, settings=unified_settings)
    messages = [call.args[1] for call in producer.send_and_wait.await_args_list]
    assert [message['array_index'] for message in messages] == [0, 1, 2, 3]
    assert all(message['source_topic'] == 'transport-topic' for message in messages)
    assert all(message['source_offset'] == 55 for message in messages)
    assert [message['error_code'] for message in messages] == ['UNKNOWN_TOPIC', 'INVALID_PAYLOAD', 'INVALID_PAYLOAD', 'INVALID_PAYLOAD']


@pytest.mark.asyncio
async def test_inbox_failure_propagates_without_kafka_commit(unified_settings):
    from aiokafka import TopicPartition
    from services.unified_worker import _persist_record
    unified_settings.INTEGRATION_SOURCE_NAMESPACE = 'source'
    record = _fake_record('transport-topic', {'event_type': 'unknown', 'payload': {}}, offset=7)
    consumer = MagicMock()
    consumer.assignment.return_value = {TopicPartition(record.topic, record.partition)}
    consumer.commit = AsyncMock()
    store = SimpleNamespace(ingest=AsyncMock(side_effect=RuntimeError('inbox unavailable')))
    with pytest.raises(RuntimeError, match='inbox unavailable'):
        await _persist_record(record, consumer=consumer, store=store, settings=unified_settings)
    consumer.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_intake_refuses_commit_after_revocation_even_if_inbox_is_saved(unified_settings):
    from aiokafka import TopicPartition
    from common.kafka import _AssignmentGuard
    from services.unified_worker import _persist_record
    unified_settings.INTEGRATION_SOURCE_NAMESPACE = 'source'
    record = _fake_record('unknown-topic', {}, offset=7)
    tp = TopicPartition(record.topic, record.partition)
    guard = _AssignmentGuard()
    consumer = MagicMock()
    consumer._integration_assignment_guard = guard
    consumer.assignment.return_value = {tp}
    consumer.commit = AsyncMock()
    async def persist(events):
        await guard.on_partitions_revoked({tp})
        await guard.on_partitions_assigned({tp})
    store = SimpleNamespace(ingest=AsyncMock(side_effect=persist))
    with pytest.raises(RuntimeError, match='revoked'):
        await _persist_record(record, consumer=consumer, store=store, settings=unified_settings)
    store.ingest.assert_awaited_once()
    consumer.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_dto_in_array_retains_transport_coordinates(unified_settings):
    record = _fake_record('transport-topic', [{'event_type': 'create-user', 'payload': {'reg': '123'}}], offset=91)
    producer = AsyncMock()
    for item in _records_from_value(record):
        await _dispatch_record(item, user_service=MagicMock(), resolver=MagicMock(),
                               catalog_client=MagicMock(), http_client=MagicMock(),
                               producer=producer, settings=unified_settings)
    message = producer.send_and_wait.await_args.args[1]
    assert message['error_code'] == 'VALIDATION_ERROR'
    assert message['source_topic'] == 'transport-topic'
    assert message['source_offset'] == 91
    assert message['array_index'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('value,code', [
    ({'event_type': 'enable_reg_company', 'payload': {}}, 'VALIDATION_ERROR'),
    ({'event_type': 'disable_reg_company', 'payload': 'not json'}, 'INVALID_JSON'),
    ({'event_type': 'enable_reg_company', 'payload': []}, 'INVALID_PAYLOAD'),
])
async def test_invalid_registration_event_uses_registration_dlq(unified_settings, value, code):
    producer = AsyncMock()
    await _dispatch_record(_fake_record('transport-topic', value), user_service=MagicMock(), resolver=MagicMock(),
                           catalog_client=MagicMock(), http_client=MagicMock(), producer=producer,
                           settings=unified_settings)
    assert producer.send_and_wait.await_args.args[0] == 'reg-company-dlq'
    assert producer.send_and_wait.await_args.args[1]['error_code'] == code


@pytest.mark.asyncio
async def test_inbox_commit_precedes_kafka_commit_for_every_array_element(unified_settings):
    from aiokafka import TopicPartition
    from services.unified_worker import _persist_record
    unified_settings.INTEGRATION_SOURCE_NAMESPACE = 'source'
    record = _fake_record('transport-topic', [
        {'event_id': 'one', 'event_type': 'create-user', 'payload': {'reg': '1'}},
        'bad',
    ], offset=77)
    trace = []
    async def ingest(events):
        assert len(events) == 2
        assert [event.array_index for event in events] == [0, 1]
        assert events[1].disposition == 'INVALID_PAYLOAD'
        trace.append('inbox committed')
    consumer = MagicMock()
    consumer.assignment.return_value = {TopicPartition(record.topic, record.partition)}
    async def commit(offsets):
        assert trace == ['inbox committed']
        trace.append('kafka committed')
    consumer.commit = AsyncMock(side_effect=commit)
    await _persist_record(record, consumer=consumer, store=SimpleNamespace(ingest=AsyncMock(side_effect=ingest)), settings=unified_settings)
    assert trace == ['inbox committed', 'kafka committed']


@pytest.mark.asyncio
async def test_intake_does_not_wait_for_external_execution():
    import asyncio
    from services.unified_worker import _run_pipeline
    settings = SimpleNamespace(
        INTEGRATION_EXECUTOR_CONCURRENCY=1, POOL_SIZE=10, INTEGRATION_SOURCE_NAMESPACE='source',
        INTEGRATION_MAX_PENDING=100, INTEGRATION_POLL_SECONDS=.001, INTEGRATION_SHUTDOWN_SECONDS=.02,
        KAFKA_POLL_TIMEOUT_MS=1, KAFKA_INIT_COMPANY_TOPIC='init_company',
        KAFKA_SYNC_DEPARTMENTS_TOPIC='sync_departments', KAFKA_CREATE_TOPIC='create-user',
        KAFKA_UPDATE_DEPARTMENTS_TOPIC='update-user-departments',
    )
    stop = asyncio.Event()
    executor_started = asyncio.Event()
    intake_polled = asyncio.Event()
    executor_cancelled = asyncio.Event()
    async def execution(*args, **kwargs):
        executor_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            executor_cancelled.set()
    async def poll(**kwargs):
        await executor_started.wait()
        intake_polled.set()
        stop.set()
        return {}
    consumer = MagicMock()
    consumer.assignment.return_value = set()
    consumer.paused.return_value = set()
    consumer.getmany = AsyncMock(side_effect=poll)
    store = SimpleNamespace(pending_count=AsyncMock(return_value=0), execute_one=AsyncMock(side_effect=execution), publish_one=AsyncMock(return_value=False))
    await asyncio.wait_for(_run_pipeline(consumer, AsyncMock(), store, AsyncMock(), settings, stop), 1)
    assert intake_polled.is_set()
    assert executor_cancelled.is_set()


@pytest.mark.asyncio
async def test_paused_intake_persists_records_from_new_assignment(monkeypatch):
    import asyncio
    import services.unified_worker as worker
    from aiokafka import TopicPartition

    stop = asyncio.Event()
    old = TopicPartition('old', 0)
    new = TopicPartition('new', 0)
    record = _fake_record('new', {'reg': '1'}, offset=1)
    consumer = MagicMock()
    consumer.assignment.return_value = {old}
    consumer.getmany = AsyncMock(return_value={new: [record]})
    async def persist(received, **kwargs):
        assert received is record
        stop.set()
    save = AsyncMock(side_effect=persist)
    monkeypatch.setattr(worker, '_persist_record', save)
    store = SimpleNamespace(pending_count=AsyncMock(return_value=100))
    settings = SimpleNamespace(INTEGRATION_MAX_PENDING=100, KAFKA_POLL_TIMEOUT_MS=1,
                               INTEGRATION_POLL_SECONDS=.001)
    await worker._intake_loop(consumer, store, settings, stop)
    consumer.pause.assert_called_once_with(old)
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_fatal_pipeline_failure_cancels_peers_and_clears_ready():
    import asyncio
    from services.unified_worker import _run_pipeline, unified_worker_ready
    settings = SimpleNamespace(INTEGRATION_EXECUTOR_CONCURRENCY=1, POOL_SIZE=10,
                               INTEGRATION_SOURCE_NAMESPACE='source', INTEGRATION_MAX_PENDING=100,
                               INTEGRATION_POLL_SECONDS=.001, INTEGRATION_SHUTDOWN_SECONDS=.1)
    store = SimpleNamespace(pending_count=AsyncMock(side_effect=RuntimeError('DB unavailable')),
                            execute_one=AsyncMock(return_value=False), publish_one=AsyncMock(return_value=False))
    with pytest.raises(ExceptionGroup):
        await _run_pipeline(MagicMock(), AsyncMock(), store, AsyncMock(), settings, asyncio.Event())
    assert unified_worker_ready._value.get() == 0
