"""Mocked journal lifecycle tests; these do not substitute for PostgreSQL acceptance.

The transaction trace checks commit/rollback and side-effect ordering. SQL locking,
concurrent claims and migration compatibility still require a real PG test run.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from common.event_store import EventStore, IncomingEvent, OutboxProducer, WaitingDependency
from common.kafka import _json_deserializer


class Result:
    def __init__(self, row=None, scalar=None):
        self.row, self.scalar = row, scalar
    def mappings(self):
        return self
    def first(self):
        return self.row
    def one(self):
        assert self.row is not None
        return self.row
    def scalar_one_or_none(self):
        return self.scalar
    def scalar_one(self):
        return self.scalar


class Session:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.trace = []
        self.calls = []
        self.execute = AsyncMock(side_effect=self._execute)
    async def _execute(self, statement, params=None):
        sql = str(statement)
        self.trace.append(('sql', sql))
        self.calls.append((sql, params or {}))
        response = self.responses.popleft() if self.responses else Result()
        if isinstance(response, BaseException):
            raise response
        return response
    @asynccontextmanager
    async def _transaction(self, name):
        self.trace.append((name, 'begin'))
        try:
            yield
        except BaseException:
            self.trace.append((name, 'rollback'))
            raise
        else:
            self.trace.append((name, 'commit'))
    def begin(self):
        return self._transaction('transaction')
    def begin_nested(self):
        return self._transaction('savepoint')
    @asynccontextmanager
    async def factory(self):
        self.trace.append(('session', 'open'))
        try:
            yield self
        finally:
            self.trace.append(('session', 'close'))


@pytest.fixture
def settings():
    return SimpleNamespace(INTEGRATION_SOURCE_NAMESPACE='source-a',
                           INTEGRATION_EVENT_DEADLINE_SECONDS=3600,
                           INTEGRATION_RETRY_ATTEMPTS=3,
                           INTEGRATION_RETRY_BASE_SECONDS=.1,
                           INTEGRATION_RETRY_MAX_SECONDS=2)


def record(value, offset=8, index=None, topic='transport'):
    return SimpleNamespace(value=value, topic=topic, partition=0, offset=offset, array_index=index)


def incoming(value, **kwargs):
    return IncomingEvent.from_record(record(value, **kwargs), namespace='source-a',
                                     event_type='init_company', dlq_topic='dlq',
                                     snapshot_types=('init_company', 'sync_departments'))


def operation(**overrides):
    row = dict(id=12, source_namespace='source-a', dedupe_key='event:e1', event_id='e1',
               fingerprint='a' * 64, source_topic='transport', source_partition=0,
               source_offset=8, array_index=-1, event_type='init_company', entity_key='reg:123',
               reg='123', source_revision=None, revision_stream=None,
               payload={'event_id': 'e1', 'payload': {'reg': '123'}}, disposition=None,
               status='received', attempts=0, deadline_at=datetime.now(timezone.utc) + timedelta(hours=1))
    row.update(overrides)
    return row


def updates(session, marker):
    return [params for sql, params in session.calls if marker in sql]


def test_normalized_fingerprint_ignores_json_member_order_and_transport_location():
    first = incoming({'event_id': 'e1', 'payload': {'reg': '123', 'id': 5}})
    second = incoming({'payload': '{"id":5,"reg":"123"}', 'event_id': 'e1'}, offset=99)
    assert first.dedupe_key == second.dedupe_key == 'event:e1'
    assert first.fingerprint == second.fingerprint
    assert incoming({'event_id': 'e1', 'payload': {'reg': 'different'}}).fingerprint != first.fingerprint


def test_legacy_keys_include_physical_topic_partition_offset_and_array_index():
    first = incoming({'reg': '123'}, offset=8, index=0)
    second = incoming({'reg': '123'}, offset=8, index=1)
    assert first.dedupe_key != second.dedupe_key
    assert first.fingerprint == second.fingerprint
    assert first.source_topic == 'transport'
    assert first.array_index == 0


@pytest.mark.parametrize('raw', [b'not-json', b'\xff', b'{"payload":"bad-json"}', b'{"value":NaN}'])
def test_bad_json_has_explicit_disposition_and_jsonb_safe_payload(raw):
    event = incoming(_json_deserializer(raw))
    assert event.disposition == 'INVALID_JSON'
    json.dumps(event.payload, allow_nan=False)


def test_snapshot_revision_only_uses_explicit_integer_and_shared_stream():
    value = {'event_id': 'x', 'source_revision': 3, 'payload': {'reg': '123'}}
    event = incoming(value)
    assert event.source_revision == 3
    assert event.revision_stream == 'company_departments'
    bad = incoming({**value, 'source_revision': '2026-10-26'})
    assert bad.source_revision is None
    assert bad.disposition == 'INVALID_SOURCE_REVISION'


@pytest.mark.parametrize('version', [True, False, 0, 2, '1', {}, []])
def test_unknown_schema_versions_do_not_reach_business_handlers(version):
    assert incoming({'event_id': 'x', 'schema_version': version, 'payload': {'reg': '123'}}).disposition == 'INVALID_SCHEMA_VERSION'


def test_reg_alias_and_nested_data_share_company_order_key():
    regular = incoming({'reg': '123', 'id': 1})
    alias = incoming({'data': {'companyReg': 123, 'id': 1}})
    assert alias.entity_key == regular.entity_key == 'reg:123'


def test_oversized_identifiers_are_journalled_as_invalid_not_index_failures():
    event = incoming({'event_id': 'a' * 3000, 'payload': {'reg': '1'}})
    assert event.event_id is None
    assert event.disposition == 'INVALID_EVENT_ID'
    assert event.dedupe_key.startswith('transport:')
    event = incoming({'reg': 'x' * 3000})
    assert event.reg is None and event.disposition == 'INVALID_REG'


def test_oversized_user_revision_stream_is_rejected():
    event = IncomingEvent.from_record(record({'reg': '1', 'uid': 'x' * 3000, 'source_revision': 1}),
        namespace='source-a', event_type='update-user', dlq_topic='dlq',
        user_revision_types=('update-user', 'update-user-departments'))
    assert event.disposition == 'INVALID_ENTITY_ID'
    assert event.revision_stream is None


@pytest.mark.parametrize('value', [
    {'event_id': '\ud800', 'payload': {'reg': '1'}},
    {'event_id': 'x', 'payload': {'reg': '\ud800'}},
    {'event_id': 'x', 'payload': {'reg': '1', 'title': '\ud800'}},
])
def test_lone_unicode_surrogates_are_durable_invalid_events(value):
    event = incoming(value)
    assert event.disposition == 'INVALID_JSON'
    json.dumps(event.payload, ensure_ascii=False).encode('utf-8')


def test_user_revisions_are_scoped_by_user_and_operation_type():
    def make(value, kind='update-user'):
        return IncomingEvent.from_record(record(value), namespace='source-a', event_type=kind,
            dlq_topic='dlq', user_revision_types=('update-user', 'update-user-departments'))
    alice = make({'reg': '123', 'uid': 'alice', 'source_revision': 5})
    bob = make({'reg': '123', 'uid': 'bob', 'source_revision': 5})
    groups = make({'reg': '123', 'id': 'alice', 'source_revision': 5}, 'update-user-departments')
    assert len({alice.revision_stream, bob.revision_stream, groups.revision_stream}) == 3


@pytest.mark.asyncio
async def test_all_array_elements_are_inserted_in_one_transaction(settings):
    session = Session([Result(scalar=10), Result(scalar=11)])
    store = EventStore(settings, session.factory)
    ids = await store.ingest([incoming({'reg': '123'}, index=0), incoming('bad', index=1)])
    assert ids == [10, 11]
    assert session.trace.count(('transaction', 'commit')) == 1
    assert [p['array_index'] for sql, p in session.calls] == [0, 1]


@pytest.mark.asyncio
async def test_second_array_insert_failure_rolls_back_the_entire_record(settings):
    session = Session([Result(scalar=10), RuntimeError('database disconnected')])
    store = EventStore(settings, session.factory)
    with pytest.raises(RuntimeError, match='database disconnected'):
        await store.ingest([incoming({'reg': '123'}, index=0), incoming('bad', index=1)])
    assert ('transaction', 'rollback') in session.trace
    assert ('transaction', 'commit') not in session.trace


@pytest.mark.asyncio
async def test_same_id_same_fingerprint_leaves_terminal_result_untouched(settings):
    event = incoming({'event_id': 'same', 'payload': {'reg': '123'}})
    session = Session([Result(), Result(row={'id': 42, 'fingerprint': event.fingerprint})])
    assert await EventStore(settings, session.factory).ingest([event]) == [42]
    assert not any('UPDATE ' in sql or 'INSERT INTO integration_outbox' in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_conflicting_id_has_separate_terminal_row_and_atomic_dlq(settings):
    event = incoming({'event_id': 'same', 'payload': {'reg': '123'}})
    session = Session([Result(), Result(row={'id': 42, 'fingerprint': 'different'}), Result(scalar=43)])
    assert await EventStore(settings, session.factory).ingest([event]) == [43]
    conflict = updates(session, "'conflict'")[0]
    assert conflict['conflicts_with'] == 42
    assert conflict['dedupe_key'].startswith('conflict:event:same:')
    outbox = updates(session, 'INSERT INTO integration_outbox')[0]
    assert outbox['operation_id'] == 43
    assert json.loads(outbox['payload'])['error_code'] == 'EVENT_ID_CONFLICT'
    assert not any('UPDATE integration_events' in sql for sql, _ in session.calls)
    assert session.trace.index(('transaction', 'commit')) > next(i for i, entry in enumerate(session.trace) if entry[0] == 'sql' and 'INSERT INTO integration_outbox' in entry[1])


@pytest.mark.asyncio
async def test_success_and_outbox_use_the_same_session_transaction(settings):
    session = Session([Result(row=operation())])
    async def dispatch(record, producer):
        assert producer.session is session
        assert ('transaction', 'commit') not in session.trace
        await producer.send_and_wait('done', {'ok': True})
    assert await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    finish = updates(session, 'completed_at = now()')[0]
    assert finish['status'] == 'succeeded'
    assert json.loads(finish['result'])['outgoing'][0]['topic'] == 'done'
    assert session.trace.count(('transaction', 'commit')) == 1
    assert any('SKIP LOCKED' in sql and 'NOT EXISTS' in sql for sql, _ in session.calls)


@pytest.mark.asyncio
async def test_dispatch_staged_output_rolls_back_before_saving_retry(settings):
    session = Session([Result(row=operation())])
    async def dispatch(record, producer):
        await producer.send_and_wait('done', {'ok': True})
        raise RuntimeError('crash before finishing')
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    retry = updates(session, 'available_at = :available_at')[0]
    assert retry['status'] == 'retry_wait'
    assert retry['code'] == 'UNEXPECTED_ERROR'
    assert isinstance(retry['available_at'], datetime)
    assert ('savepoint', 'rollback') in session.trace
    assert ('transaction', 'commit') in session.trace
    assert not updates(session, 'completed_at = now()')


@pytest.mark.asyncio
async def test_cancellation_rolls_back_claim_and_output(settings):
    session = Session([Result(row=operation())])
    async def dispatch(record, producer):
        await producer.send_and_wait('done', {'ok': True})
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    assert ('savepoint', 'rollback') in session.trace
    assert ('transaction', 'rollback') in session.trace
    assert ('transaction', 'commit') not in session.trace


@pytest.mark.asyncio
async def test_retry_limit_has_terminal_error_and_outbox_in_one_commit(settings):
    session = Session([Result(row=operation(attempts=2))])
    async def dispatch(record, producer):
        raise RuntimeError('permanent surprise')
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    finish = updates(session, 'completed_at = now()')[0]
    assert finish['status'] == 'failed'
    assert finish['code'] == 'EVENT_RETRY_EXHAUSTED'
    assert updates(session, 'INSERT INTO integration_outbox')
    assert ('transaction', 'commit') in session.trace


@pytest.mark.asyncio
async def test_expired_or_invalid_input_does_not_call_catalog(settings):
    for overrides, expected in [
        ({'deadline_at': datetime.now(timezone.utc) - timedelta(seconds=1)}, 'EVENT_DEADLINE_EXCEEDED'),
        ({'disposition': 'INVALID_JSON'}, 'INVALID_JSON'),
    ]:
        session = Session([Result(row=operation(**overrides))])
        dispatch = AsyncMock()
        await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
        dispatch.assert_not_awaited()
        assert updates(session, 'completed_at = now()')[0]['code'] == expected


@pytest.mark.asyncio
async def test_stale_snapshot_is_recorded_without_external_execution(settings):
    session = Session([Result(row=operation(source_revision=7, revision_stream='company_departments')), Result(), Result(scalar=8)])
    dispatch = AsyncMock()
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq', snapshot_types=('init_company',))
    dispatch.assert_not_awaited()
    finish = updates(session, 'completed_at = now()')[0]
    assert finish['status'] == 'superseded'
    assert finish['code'] == 'STALE_REVISION'


@pytest.mark.asyncio
async def test_dependency_wait_has_no_dlq_and_can_be_resolved_by_later_snapshot(settings):
    session = Session([Result(row=operation(event_type='update-user-departments'))])
    async def dispatch(record, producer):
        await producer.send_and_wait('dlq', {'error_code': 'DEPARTMENT_GROUP_NOT_READY', 'error_message': 'Wait'})
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq', snapshot_types=('init_company',))
    assert not updates(session, 'INSERT INTO integration_outbox')
    assert updates(session, 'available_at = :available_at')[0]['status'] == 'waiting_dependency'
    claim_sql, claim_params = session.calls[0]
    assert "prior.status = 'waiting_dependency'" in claim_sql
    assert claim_params['resolver_types'] == ['init_company']


@pytest.mark.asyncio
async def test_dependency_wait_does_not_exhaust_general_retry_count(settings):
    session = Session([Result(row=operation(event_type='update-user-departments', attempts=99))])
    async def dispatch(record, producer):
        await producer.send_and_wait('dlq', {'error_code': 'USER_NOT_FOUND', 'error_message': 'Wait'})
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq',
        resolver_types=('create-user',))
    assert not updates(session, 'INSERT INTO integration_outbox')
    assert updates(session, 'available_at = :available_at')[0]['status'] == 'waiting_dependency'
    assert 'create-user' in session.calls[0][1]['resolver_types']


@pytest.mark.asyncio
async def test_user_not_found_for_regular_update_stays_terminal(settings):
    session = Session([Result(row=operation(event_type='update-user'))])
    async def dispatch(record, producer):
        await producer.send_and_wait('dlq', {'error_code': 'USER_NOT_FOUND', 'error_message': 'Missing'})
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    assert updates(session, 'completed_at = now()')[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_transient_catalog_failure_returns_to_durable_retry(settings):
    session = Session([Result(row=operation())])
    async def dispatch(record, producer):
        await producer.send_and_wait('dlq', {'error_code': 'CATALOG_READ_FAILED', 'error_message': 'Unavailable'})
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    assert not updates(session, 'INSERT INTO integration_outbox')
    retry = updates(session, 'available_at = :available_at')[0]
    assert retry['status'] == 'retry_wait'
    assert retry['code'] == 'CATALOG_READ_FAILED'


@pytest.mark.asyncio
async def test_uncertain_external_write_is_not_retried_without_reconciliation(settings):
    session = Session([Result(row=operation())])
    async def dispatch(record, producer):
        await producer.send_and_wait('dlq', {'error_code': 'ACL_WRITE_UNCERTAIN', 'error_message': 'Unknown'})
    await EventStore(settings, session.factory).execute_one(dispatch, dlq_topic_for=lambda _: 'dlq')
    assert updates(session, 'completed_at = now()')[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_outbox_stable_id_is_a_header_without_changing_existing_payload():
    session = Session()
    producer = OutboxProducer(session, operation())
    await producer.send_and_wait('done', {'reg': '123'}, headers=[('trace', b'a')])
    first = updates(session, 'INSERT INTO integration_outbox')[0]
    again = OutboxProducer(session, operation())
    await again.send_and_wait('done', {'reg': '123'})
    second = updates(session, 'INSERT INTO integration_outbox')[1]
    assert first['id'] == second['id']
    assert json.loads(first['payload']) == {'reg': '123'}
    headers = json.loads(first['headers'])
    assert bytes.fromhex(dict(headers)['event_id']).decode() == str(first['id'])


def outbox_row():
    message_id = uuid4()
    return dict(id=message_id, operation_id=12, sequence=0, topic='done', message_key=None,
                payload={'reg': '123'}, headers=[['event_id', str(message_id).encode().hex()]], attempts=0)


@pytest.mark.asyncio
async def test_publisher_marks_sent_only_after_kafka_ack(settings):
    row = outbox_row()
    session = Session([Result(row=row)])
    async def send(*args, **kwargs):
        assert not updates(session, 'sent_at = now()')
        assert ('transaction', 'commit') not in session.trace
        assert dict(kwargs['headers'])['event_id'].decode() == str(row['id'])
    producer = SimpleNamespace(send_and_wait=AsyncMock(side_effect=send))
    await EventStore(settings, session.factory).publish_one(producer)
    assert updates(session, 'sent_at = now()')[0]['id'] == row['id']
    assert ('transaction', 'commit') in session.trace


@pytest.mark.asyncio
async def test_failed_publish_remains_pending_with_retry_time(settings):
    row = outbox_row()
    session = Session([Result(row=row)])
    producer = SimpleNamespace(send_and_wait=AsyncMock(side_effect=RuntimeError('broker down')))
    await EventStore(settings, session.factory).publish_one(producer)
    assert not updates(session, 'sent_at = now()')
    retry = updates(session, 'available_at = :available_at')[0]
    assert isinstance(retry['available_at'], datetime)
    assert ('transaction', 'commit') in session.trace


@pytest.mark.asyncio
async def test_publisher_crash_after_ack_rolls_back_and_repeats_stable_message_id(settings):
    row = outbox_row()
    failure = OperationalError('update outbox', {}, RuntimeError('DB disconnected'))
    first = Session([Result(row=row), failure])
    producer = AsyncMock()
    with pytest.raises(OperationalError):
        await EventStore(settings, first.factory).publish_one(producer)
    assert ('transaction', 'rollback') in first.trace
    second = Session([Result(row=row)])
    await EventStore(settings, second.factory).publish_one(producer)
    assert producer.send_and_wait.await_count == 2
    assert producer.send_and_wait.await_args_list[0].kwargs['headers'] == producer.send_and_wait.await_args_list[1].kwargs['headers']
