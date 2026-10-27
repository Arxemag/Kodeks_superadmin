"""Opt-in real PostgreSQL checks, restricted to the isolated local test DB.

KODEKS_TEST_POSTGRES_DSN=postgresql+asyncpg://postgres@127.0.0.1:55439/kodeks_codex_test
The database and migrations must already exist. Ordinary pytest runs skip these tests.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from common import catalog_lock
from common.event_store import EventStore, IncomingEvent


@pytest_asyncio.fixture
async def postgres(monkeypatch):
    dsn = os.environ.get('KODEKS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Set KODEKS_TEST_POSTGRES_DSN for the isolated real-PostgreSQL tests')
    target = urlsplit(dsn)
    if (target.scheme, target.hostname, target.port, target.path) != (
        'postgresql+asyncpg', '127.0.0.1', 55439, '/kodeks_codex_test'
    ):
        pytest.fail('Refusing to run PostgreSQL integration tests against a different DB')
    engine = create_async_engine(dsn, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    namespace = 'codex-test-' + uuid4().hex
    settings = SimpleNamespace(INTEGRATION_SOURCE_NAMESPACE=namespace,
        INTEGRATION_EVENT_DEADLINE_SECONDS=3600, INTEGRATION_RETRY_ATTEMPTS=3,
        INTEGRATION_RETRY_BASE_SECONDS=.01, INTEGRATION_RETRY_MAX_SECONDS=.1)
    @asynccontextmanager
    async def session_factory():
        async with maker() as session:
            yield session
    monkeypatch.setattr(catalog_lock, 'get_db_session', session_factory)
    monkeypatch.setattr(catalog_lock, 'get_settings', lambda: SimpleNamespace(CATALOG_LOCK_TIMEOUT=2.0))
    store = EventStore(settings, session_factory)
    yield SimpleNamespace(engine=engine, maker=maker, namespace=namespace, store=store, session_factory=session_factory)
    async with engine.begin() as connection:
        await connection.execute(text('DELETE FROM integration_outbox WHERE operation_id IN '
            '(SELECT id FROM integration_events WHERE source_namespace=:namespace)'), {'namespace': namespace})
        await connection.execute(text('DELETE FROM integration_events WHERE source_namespace=:namespace'), {'namespace': namespace})
        for table, key in [('catalog_user_group_ownership', 'reg'), ('catalog_acl_ownership', 'reg'),
                           ('department_service_mapping', 'reg'), ('reg_services', 'reg_number')]:
            await connection.execute(text(f'DELETE FROM {table} WHERE {key} IN (:reg_a, :reg_b)'),
                {'reg_a': namespace + 'a', 'reg_b': namespace + 'b'})
    await engine.dispose()


def event(pg, payload, offset=1):
    record = SimpleNamespace(value=payload, topic='create-user', partition=0, offset=offset, array_index=None)
    return IncomingEvent.from_record(record, namespace=pg.namespace,
        event_type='create-user', dlq_topic='users-dlq')


async def row(pg, statement, **params):
    async with pg.maker() as session:
        result = await session.execute(text(statement), params)
        return result.mappings().first()


@pytest.mark.asyncio
async def test_real_db_ingest_dedup_conflict_and_atomic_outbox(postgres):
    pg = postgres
    item = event(pg, {'event_id': 'source-id', 'payload': {'reg': '123', 'uid': 'alice'}})
    first = (await pg.store.ingest([item]))[0]
    assert await pg.store.ingest([item]) == [first]
    conflicting = event(pg, {'event_id': 'source-id', 'payload': {'reg': '123', 'uid': 'bob'}}, offset=2)
    conflict = (await pg.store.ingest([conflicting]))[0]
    assert conflict != first
    result = await row(pg, 'SELECT status, error_code, conflicts_with FROM integration_events WHERE id=:id', id=conflict)
    assert dict(result) == {'status': 'conflict', 'error_code': 'EVENT_ID_CONFLICT', 'conflicts_with': first}
    outgoing = await row(pg, 'SELECT topic, payload FROM integration_outbox WHERE operation_id=:id', id=conflict)
    assert outgoing['topic'] == 'users-dlq'
    assert outgoing['payload']['error_code'] == 'EVENT_ID_CONFLICT'
    original = await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=first)
    assert original['status'] == 'received'


@pytest.mark.asyncio
async def test_real_db_failed_savepoint_rolls_back_outbox_then_success_is_atomic(postgres):
    pg = postgres
    operation = (await pg.store.ingest([event(pg, {'reg': '123', 'uid': 'alice'})]))[0]
    async def fail_after_staging(record, producer):
        await producer.send_and_wait('done', {'reg': '123'})
        raise RuntimeError('after staging')
    assert await pg.store.execute_one(fail_after_staging, dlq_topic_for=lambda _: 'dlq')
    saved = await row(pg, 'SELECT status, attempts FROM integration_events WHERE id=:id', id=operation)
    assert dict(saved) == {'status': 'retry_wait', 'attempts': 1}
    assert await row(pg, 'SELECT id FROM integration_outbox WHERE operation_id=:id', id=operation) is None
    async with pg.engine.begin() as connection:
        await connection.execute(text('UPDATE integration_events SET available_at=now() WHERE id=:id'), {'id': operation})
    async def succeed(record, producer):
        await producer.send_and_wait('done', {'reg': '123'})
    assert await pg.store.execute_one(succeed, dlq_topic_for=lambda _: 'dlq')
    saved = await row(pg, 'SELECT status, attempts, result FROM integration_events WHERE id=:id', id=operation)
    assert saved['status'] == 'succeeded' and saved['attempts'] == 2
    outgoing = await row(pg, 'SELECT id, payload FROM integration_outbox WHERE operation_id=:id', id=operation)
    assert outgoing['payload'] == {'reg': '123'}
    assert saved['result']['outgoing'][0]['event_id'] == str(outgoing['id'])


@pytest.mark.asyncio
async def test_real_db_order_and_skip_locked_for_same_company(postgres):
    pg = postgres
    first, second = await pg.store.ingest([
        event(pg, {'reg': '123', 'uid': 'alice'}, offset=10),
        event(pg, {'reg': '123', 'uid': 'bob'}, offset=11),
    ])
    started, release = asyncio.Event(), asyncio.Event()
    async def blocked_dispatch(record, producer):
        started.set()
        await release.wait()
    current = asyncio.create_task(pg.store.execute_one(blocked_dispatch, dlq_topic_for=lambda _: 'dlq'))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not await asyncio.wait_for(pg.store.execute_one(blocked_dispatch, dlq_topic_for=lambda _: 'dlq'), 2)
        after = await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=second)
        assert after['status'] == 'received'
    finally:
        release.set()
        await asyncio.wait_for(current, 2)
    assert (await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=first))['status'] == 'succeeded'
    assert await pg.store.execute_one(lambda record, producer: asyncio.sleep(0), dlq_topic_for=lambda _: 'dlq')
    assert (await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=second))['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_real_db_advisory_lock_excludes_concurrent_writers(postgres):
    acquired_first, release_first, acquired_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def first():
        async with catalog_lock.catalog_operation('https://catalog.example/path'):
            acquired_first.set()
            await release_first.wait()
    async def second():
        async with catalog_lock.catalog_operation('https://CATALOG.example:443/path/'):
            acquired_second.set()
    task_one = asyncio.create_task(first())
    await asyncio.wait_for(acquired_first.wait(), 2)
    task_two = asyncio.create_task(second())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(acquired_second.wait(), .15)
        assert not task_two.done()
    finally:
        release_first.set()
        await asyncio.wait_for(asyncio.gather(task_one, task_two), 2)
    assert acquired_second.is_set()


@pytest.mark.asyncio
async def test_real_db_waiting_dependency_is_unblocked_by_later_user_create(postgres):
    pg = postgres
    group_record = SimpleNamespace(value={'reg': '123', 'id': 'alice', 'departments': []},
        topic='update-user-departments', partition=0, offset=20, array_index=None)
    group_event = IncomingEvent.from_record(group_record, namespace=pg.namespace,
        event_type='update-user-departments', dlq_topic='users-dlq')
    create_event = event(pg, {'reg': '123', 'uid': 'alice'}, offset=21)
    group_id, create_id = await pg.store.ingest([group_event, create_event])
    async def waiting(record, producer):
        await producer.send_and_wait('users-dlq', {'error_code': 'USER_NOT_FOUND', 'error_message': 'waiting'})
    options = {'dlq_topic_for': lambda _: 'users-dlq', 'resolver_types': ('create-user',)}
    assert await pg.store.execute_one(waiting, **options)
    assert (await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=group_id))['status'] == 'waiting_dependency'
    async with pg.engine.begin() as connection:
        await connection.execute(text("UPDATE integration_events SET available_at=now()+interval '1 hour' WHERE id=:id"), {'id': group_id})
    calls = []
    async def create(record, producer):
        calls.append(record.topic)
    assert await pg.store.execute_one(create, **options)
    assert calls == ['create-user']
    assert (await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=create_id))['status'] == 'succeeded'
    async with pg.engine.begin() as connection:
        await connection.execute(text('UPDATE integration_events SET available_at=now() WHERE id=:id'), {'id': group_id})
    assert await pg.store.execute_one(create, **options)
    assert (await row(pg, 'SELECT status FROM integration_events WHERE id=:id', id=group_id))['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_real_db_outbox_remains_pending_until_kafka_ack(postgres):
    pg = postgres
    operation = (await pg.store.ingest([event(pg, {'reg': '123', 'uid': 'alice'})]))[0]
    async def dispatch(record, producer):
        await producer.send_and_wait('done', {'reg': '123'})
    await pg.store.execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    class Producer:
        def __init__(self): self.calls = []; self.fail = True
        async def send_and_wait(self, topic, value, **kwargs):
            self.calls.append((topic, value, kwargs))
            if self.fail: raise RuntimeError('broker unavailable')
    producer = Producer()
    assert await pg.store.publish_one(producer)
    saved = await row(pg, 'SELECT sent_at, attempts FROM integration_outbox WHERE operation_id=:id', id=operation)
    assert saved['sent_at'] is None and saved['attempts'] == 1
    async with pg.engine.begin() as connection:
        await connection.execute(text('UPDATE integration_outbox SET available_at=now() WHERE operation_id=:id'), {'id': operation})
    producer.fail = False
    assert await pg.store.publish_one(producer)
    saved = await row(pg, 'SELECT sent_at, attempts FROM integration_outbox WHERE operation_id=:id', id=operation)
    assert saved['sent_at'] is not None and saved['attempts'] == 2
    assert producer.calls[0] == producer.calls[1]


@pytest.mark.asyncio
async def test_real_db_custom_tables_migrate_in_separate_schema(postgres):
    from scripts.init_db import _apply_migrations
    pg = postgres
    schema = 'codex_it_' + uuid4().hex
    migrations = sorted(Path('scripts/migrations').glob('*.sql'))
    async with pg.engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with pg.engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            result = await _apply_migrations(connection, migrations, 'custom_reg_services', 'custom_department_mapping')
            assert len(result) == len(migrations)
            assert await _apply_migrations(connection, migrations, 'custom_reg_services', 'custom_department_mapping') == []
            indices = await connection.execute(text('''SELECT i.indexname FROM pg_indexes i
                WHERE i.schemaname=:schema AND i.tablename='custom_department_mapping' '''), {'schema': schema})
            names = {row[0] for row in indices}
            assert any(name.startswith('idx_dsm_') and name.endswith('_reg') for name in names)
            assert any(name.startswith('idx_dsm_') and name.endswith('_reg_dept') for name in names)
            assert any(name.startswith('idx_dsm_') and name.endswith('_reg_catalog_group') for name in names)
    finally:
        async with pg.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.asyncio
async def test_real_db_acl_ownership_preserves_another_reg_and_manual_grants(postgres, monkeypatch):
    from common import acl_ownership
    from common.acl_ownership import AclOwnershipRepository, plan_acl_assignments
    pg = postgres
    monkeypatch.setattr(acl_ownership, 'get_db_session', pg.session_factory)
    repo = AclOwnershipRepository()
    reg_a, reg_b = pg.namespace + 'a', pg.namespace + 'b'
    base_url = 'https://catalog-' + uuid4().hex + '.local/docs'
    name = 'acl_infoboard_p1'
    await repo.replace(base_url, reg_a, {name: {2, 3}})
    await repo.replace(base_url + '/', reg_b, {name: {2, 5}})
    owned, elsewhere = await repo.get(base_url, reg_a)
    assert owned == {name: {2, 3}} and elsewhere == {name: {2, 5}}
    result, ownership = plan_acl_assignments({name: {2, 3, 9}}, owned, {}, elsewhere)
    assert result == {name: [2, 9]} and ownership == {}
    await repo.replace(base_url, reg_a, ownership)
    assert (await repo.get(base_url, reg_b))[0] == {name: {2, 5}}


@pytest.mark.asyncio
async def test_real_db_department_group_resolution_by_uuid(postgres, monkeypatch):
    from common import department_groups
    from common.department_groups import DepartmentGroupRepository
    from common.exceptions import NetworkError
    pg = postgres
    monkeypatch.setattr(department_groups, 'get_db_session', pg.session_factory)
    monkeypatch.setattr(department_groups, 'get_settings', lambda: SimpleNamespace(DB_TABLE_DEPARTMENT_MAPPING='department_service_mapping'))
    reg = pg.namespace + 'a'
    department_id, archived_id = uuid4(), uuid4()
    async with pg.engine.begin() as connection:
        await connection.execute(text('''INSERT INTO department_service_mapping
            (department_id, service_group_name, reg, client_id, client_name, mapping_status, catalog_group_id)
            VALUES (:id, 'Department', :reg, '1', 'Test company', 'active', 42),
                   (:archived, 'Archived', :reg, '1', 'Test company', 'archived', 43)'''),
            {'id': department_id, 'archived': archived_id, 'reg': reg})
    repo = DepartmentGroupRepository()
    assert await repo.resolve(reg, [str(department_id)]) == {42}
    with pytest.raises(NetworkError) as error:
        await repo.resolve(reg, [str(archived_id)])
    assert error.value.code == 'DEPARTMENT_GROUP_NOT_READY'


@pytest.mark.asyncio
async def test_real_db_user_ownership_preserves_shared_group_for_two_regs(postgres, monkeypatch):
    from common import group_ownership
    from common.group_ownership import GroupOwnershipRepository
    from common.exceptions import ParseError
    pg = postgres
    monkeypatch.setattr(group_ownership, 'get_db_session', pg.session_factory)
    monkeypatch.setattr(group_ownership, 'get_settings', lambda: SimpleNamespace(DB_TABLE_REG_SERVICES='reg_services'))
    reg_a, reg_b, uid = pg.namespace + 'a', pg.namespace + 'b', pg.namespace
    base_url = 'https://catalog-' + uuid4().hex + '.local/docs'
    async with pg.engine.begin() as connection:
        await connection.execute(text('INSERT INTO reg_services (reg_number, base_url) VALUES (:reg_a, :url_a), (:reg_b, :url_b)'),
            {'reg_a': reg_a, 'url_a': base_url, 'reg_b': reg_b, 'url_b': base_url.upper().replace('/DOCS', '/docs') + '/'})
    repo = GroupOwnershipRepository()
    await repo.set(reg_a, uid, {2, 3})
    await repo.set(reg_b, uid, {2, 5})
    owned, shared = await repo.get_with_shared(reg_a, uid, base_url, desired=set(), current={2, 3, 9})
    assert owned == {2, 3} and shared == {2, 5}
    assert ({2, 3, 9} - (owned - shared)) == {2, 9}
    async with pg.engine.begin() as connection:
        await connection.execute(text('DELETE FROM reg_services WHERE reg_number=:reg'), {'reg': reg_b})
    with pytest.raises(ParseError) as error:
        await repo.get_with_shared(reg_a, uid, base_url, desired=set(), current={2, 3, 9})
    assert error.value.code == 'GROUP_SHARING_UNKNOWN'
