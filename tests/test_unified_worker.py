"""
Тесты единого Kafka worker: одна группа, все топики, маршрутизация по record.topic.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.unified_worker import _all_topics, _dispatch_record, _effective_topic, _event_type_from_value, _records_from_value
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
    s.KAFKA_ENABLE_REG_COMPANY_TOPIC = "enable_reg_company"
    s.KAFKA_DISABLE_REG_COMPANY_TOPIC = "disable_reg_company"
    s.KAFKA_DLQ_TOPIC = "users-dlq"
    s.KAFKA_INIT_COMPANY_DLQ_TOPIC = "init_company-dlq"
    s.USERS_RETRY_ATTEMPTS = 2
    s.USERS_RETRY_BASE_DELAY = 0.01
    s.USERS_RETRY_MAX_DELAY = 0.1
    return s


def test_all_topics_returns_six_topics(unified_settings: MagicMock) -> None:
    """_all_topics возвращает ровно 6 топиков в нужном порядке."""
    topics = _all_topics(unified_settings)
    assert len(topics) == 6
    assert topics[0] == "create-user"
    assert topics[1] == "update-user"
    assert topics[2] == "update-user-departments"
    assert topics[3] == "init_company"
    assert topics[4] == "enable_reg_company"
    assert topics[5] == "disable_reg_company"


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
    assert _event_type_from_value(record.value) == "init_company"


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
async def test_dispatch_record_enable_reg_company_no_raise(unified_settings: MagicMock) -> None:
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
    # просто не падаем


@pytest.mark.asyncio
async def test_dispatch_record_disable_reg_company_no_raise(unified_settings: MagicMock) -> None:
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


@pytest.mark.asyncio
async def test_dispatch_record_unknown_topic_logs_no_raise(unified_settings: MagicMock) -> None:
    """Неизвестный топик — только warning в лог, исключение не бросается."""
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
    producer.send_and_wait.assert_not_called()
