"""Durable intake and atomic PostgreSQL operation/outbox state for unified worker.

A row lock is held while dispatch runs. This serializes journal claims, but cannot
make an HTTP mutation atomic with PostgreSQL: recovery must reconcile the catalog.
Kafka offsets mean that intake is durable, never that the business action succeeded.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.db import get_db_session
from common.kafka import MalformedJSON, dlq_payload, record_coordinates, unwrap_payload
from common.logger import get_logger


logger = get_logger('integration.events')
_ACTIVE = "('received', 'running', 'retry_wait', 'waiting_dependency')"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_text(value: str, max_bytes: int) -> bool:
    try:
        return bool(value.strip()) and len(value.encode('utf-8')) <= max_bytes
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True)
class IncomingEvent:
    source_namespace: str
    dedupe_key: str
    event_id: str | None
    fingerprint: str
    source_topic: str
    source_partition: int
    source_offset: int
    array_index: int
    event_type: str
    entity_key: str
    reg: str | None
    source_revision: int | None
    revision_stream: str | None
    payload: Any
    disposition: str | None
    dlq_topic: str

    @classmethod
    def from_record(cls, record, *, namespace: str, event_type: str,
                    dlq_topic: str, snapshot_types=(), user_revision_types=()) -> IncomingEvent:
        value = record.value
        decoded = unwrap_payload(value)
        disposition = None
        if isinstance(decoded, MalformedJSON):
            disposition = 'INVALID_JSON'
        elif not isinstance(decoded, dict):
            disposition = 'INVALID_PAYLOAD'
        stored = dlq_payload(value)
        try:
            _json(stored).encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            # PostgreSQL text/JSONB cannot encode lone surrogate code points.
            # Preserve a safe escaped representation and DLQ the record instead
            # of repeatedly crashing intake at the same offset.
            try:
                safe_raw = json.dumps(stored, ensure_ascii=True)
            except (TypeError, ValueError):
                safe_raw = repr(stored).encode('ascii', 'backslashreplace').decode('ascii')
            stored = {'raw_json': safe_raw,
                      'decode_error': 'Non-finite, malformed Unicode or unsupported JSON value'}
            disposition = 'INVALID_JSON'
        coordinates = record_coordinates(record)
        event_id = value.get('event_id') if isinstance(value, dict) else None
        if event_id is not None and (
            not isinstance(event_id, str) or not _valid_text(event_id, 200)
        ):
            event_id = None
            disposition = disposition or 'INVALID_EVENT_ID'
        version = value.get('schema_version') if isinstance(value, dict) else None
        if version is not None and (type(version) is not int or version != 1):
            disposition = disposition or 'INVALID_SCHEMA_VERSION'
        reg = decoded.get('reg') if isinstance(decoded, dict) else None
        if reg is None and isinstance(decoded, dict) and event_type in snapshot_types:
            data = decoded.get('data') if isinstance(decoded.get('data'), dict) else decoded
            for key in ('reg', 'registration', 'reg_number', 'regNumber', 'companyReg',
                        'registrationNumber', 'company_reg', 'companyRegNumber', 'reg_id'):
                if data.get(key) is not None:
                    reg = data[key]
                    break
        reg = str(reg) if reg is not None else None
        if reg is not None and (len(reg) > 50 or not _valid_text(reg, 200)):
            # Prevent oversized values from breaking the journal's B-tree index
            # and permanently stalling consumption before the DTO can reject it.
            reg = None
            disposition = disposition or 'INVALID_REG'
        # Only an explicit source_revision is accepted. No timestamps or aliases
        # are guessed as revisions. init_company/sync_departments share one stream.
        revision = value.get('source_revision') if isinstance(value, dict) else None
        if revision is None and isinstance(decoded, dict):
            revision = decoded.get('source_revision')
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or revision > 9223372036854775807):
            revision = None
            disposition = disposition or 'INVALID_SOURCE_REVISION'
        stream = None
        if not isinstance(event_type, str) or not _valid_text(event_type, 256):
            event_type = '__invalid_event_type__'
            disposition = disposition or 'INVALID_EVENT_TYPE'
        if event_type in snapshot_types:
            stream = 'company_departments'
        elif event_type in user_revision_types and isinstance(decoded, dict):
            # Each user and each type of state has an independent revision.
            user = decoded.get('uid') if event_type == user_revision_types[0] else decoded.get('id')
            if user is not None:
                if not _valid_text(str(user), 256):
                    disposition = disposition or 'INVALID_ENTITY_ID'
                else:
                    stream = _json([event_type, str(user)])
        topic = coordinates['source_topic']
        partition = coordinates['source_partition']
        offset = coordinates['source_offset']
        index = coordinates['array_index'] if coordinates['array_index'] is not None else -1
        transport = _json([topic, partition, offset, index])
        dedupe_key = f'event:{event_id}' if event_id is not None else f'transport:{transport}'
        canonical = {'event_type': event_type, 'source_revision': revision,
                     'schema_version': value.get('schema_version') if isinstance(value, dict) else None,
                     'payload': dlq_payload(decoded) if disposition != 'INVALID_JSON' else stored}
        return cls(namespace, dedupe_key, event_id,
                   hashlib.sha256(_json(canonical).encode('utf-8')).hexdigest(),
                   topic, partition, offset, index, event_type,
                   f'reg:{reg}' if reg is not None else f'transport:{topic}:{partition}',
                   reg, revision, stream if revision is not None else None,
                   stored, disposition, dlq_topic)

    def params(self, deadline: datetime) -> dict[str, Any]:
        return {**self.__dict__, 'payload': _json(self.payload), 'deadline_at': deadline}


def operation_record(operation):
    return SimpleNamespace(value=operation['payload'], topic=operation['event_type'],
                           source_topic=operation['source_topic'], partition=operation['source_partition'],
                           offset=operation['source_offset'],
                           array_index=None if operation['array_index'] == -1 else operation['array_index'])


class WaitingDependency(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RetryableOperation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# An uncertain mutation (notably ACL_WRITE_UNCERTAIN) is intentionally not on
# this list: the caller must establish its outcome before repeating a write.
_RETRYABLE_ERRORS = {
    'NETWORK_ERROR', 'TIMEOUT_ERROR', 'CATALOG_UNAVAILABLE', 'CATALOG_LOCK_FAILED',
    'CATALOG_LOCK_TIMEOUT', 'CATALOG_READ_FAILED', 'CATALOG_5XX',
    'AUTH_SERVICE_UNAVAILABLE', 'AUTH_SERVICE_5XX', 'DEPARTMENT_MAPPING_UNAVAILABLE',
    'ACL_OWNERSHIP_READ_FAILED', 'ACL_OWNERSHIP_WRITE_FAILED',
    'GROUP_OWNERSHIP_READ_FAILED', 'GROUP_OWNERSHIP_WRITE_FAILED',
    'INFOBOARDS_5XX', 'INFOBOARDS_REQUEST_FAILED', 'GRAPHQL_5XX', 'GRAPHQL_REQUEST_FAILED',
}


class OutboxProducer:
    """Producer-shaped adapter: writes rows in the caller's active transaction.

    No Kafka network call and no commit is allowed here. The stable message ID is
    derived from the durable operation and sequence; delivery adds it as a header.
    """
    def __init__(self, session, operation):
        self.session = session
        self.operation = operation
        self.sequence = 0
        self.messages: list[dict[str, Any]] = []
        self.failure: dict[str, Any] | None = None

    async def send_and_wait(self, topic, value, *, key=None, headers=None, **kwargs):
        if kwargs:
            raise ValueError('Outbox supports only topic/value/key/headers')
        if isinstance(value, dict):
            code = value.get('error_code')
            message = value.get('error_message', 'Operation failed')
            if code == 'DEPARTMENT_GROUP_NOT_READY' or (
                code == 'USER_NOT_FOUND' and self.operation['event_type'] in self.operation.get(
                    '_user_dependency_types', ('update-user-departments',))
            ):
                raise WaitingDependency(code, message)
            if code in _RETRYABLE_ERRORS:
                raise RetryableOperation(code, message)
        message_id = uuid5(NAMESPACE_URL, f"kodeks:{self.operation['source_namespace']}:{self.operation['id']}:{self.sequence}")
        encoded_headers = []
        for name, raw in headers or []:
            if name == 'event_id':
                continue
            encoded_headers.append([name, None if raw is None else bytes(raw).hex()])
        encoded_headers.append(['event_id', str(message_id).encode('ascii').hex()])
        if isinstance(key, str):
            key = key.encode('utf-8')
        await self.session.execute(text('''
            INSERT INTO integration_outbox (id, operation_id, sequence, topic, message_key, payload, headers)
            VALUES (:id, :operation_id, :sequence, :topic, :key, CAST(:payload AS jsonb), CAST(:headers AS jsonb))
            ON CONFLICT (operation_id, sequence) DO NOTHING
        '''), {'id': message_id, 'operation_id': self.operation['id'], 'sequence': self.sequence,
               'topic': topic, 'key': key, 'payload': _json(value), 'headers': _json(encoded_headers)})
        self.sequence += 1
        self.messages.append({'event_id': str(message_id), 'topic': topic, 'payload': value})
        if isinstance(value, dict) and value.get('error_code'):
            self.failure = value
        return SimpleNamespace(event_id=str(message_id))


class EventStore:
    def __init__(self, settings, session_factory=None):
        self.settings = settings
        self.session_factory = session_factory or get_db_session

    def delay(self, attempts: int) -> float:
        base = min(self.settings.INTEGRATION_RETRY_MAX_SECONDS,
                   self.settings.INTEGRATION_RETRY_BASE_SECONDS * 2 ** min(max(attempts - 1, 0), 20))
        return min(self.settings.INTEGRATION_RETRY_MAX_SECONDS, base + random.uniform(0, base * .25))

    async def pending_count(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(text(f'''SELECT count(*) FROM integration_events
                WHERE source_namespace = :namespace AND status IN {_ACTIVE}'''),
                {'namespace': self.settings.INTEGRATION_SOURCE_NAMESPACE})
            return int(result.scalar_one())

    async def ingest(self, events: list[IncomingEvent]) -> list[int]:
        """All elements of one physical Kafka record are durable in one commit."""
        deadline = _now() + timedelta(seconds=self.settings.INTEGRATION_EVENT_DEADLINE_SECONDS)
        ids = []
        async with self.session_factory() as session:
            async with session.begin():
                for event in events:
                    params = event.params(deadline)
                    result = await session.execute(text('''
                        INSERT INTO integration_events (
                            source_namespace, dedupe_key, event_id, fingerprint, source_topic,
                            source_partition, source_offset, array_index, event_type, entity_key,
                            reg, source_revision, revision_stream, payload, disposition, deadline_at)
                        VALUES (:source_namespace, :dedupe_key, :event_id, :fingerprint, :source_topic,
                            :source_partition, :source_offset, :array_index, :event_type, :entity_key,
                            :reg, :source_revision, :revision_stream, CAST(:payload AS jsonb), :disposition, :deadline_at)
                        ON CONFLICT (source_namespace, dedupe_key) DO NOTHING RETURNING id
                    '''), params)
                    inserted = result.scalar_one_or_none()
                    if inserted is not None:
                        ids.append(inserted)
                        continue
                    existing = (await session.execute(text('''
                        SELECT id, fingerprint FROM integration_events
                        WHERE source_namespace = :source_namespace AND dedupe_key = :dedupe_key
                    '''), params)).mappings().one()
                    if existing['fingerprint'] == event.fingerprint:
                        ids.append(existing['id'])
                        continue
                    # A conflicting publication is its own immutable failure;
                    # never overwrite the original input, status or result.
                    conflict_params = {**params, 'dedupe_key': f'conflict:{event.dedupe_key}:{event.fingerprint}',
                                       'conflicts_with': existing['id']}
                    conflict = await session.execute(text('''
                        INSERT INTO integration_events (
                            source_namespace, dedupe_key, event_id, fingerprint, source_topic,
                            source_partition, source_offset, array_index, event_type, entity_key,
                            reg, payload, disposition, status, deadline_at, completed_at,
                            error_code, error_message, conflicts_with)
                        VALUES (:source_namespace, :dedupe_key, :event_id, :fingerprint, :source_topic,
                            :source_partition, :source_offset, :array_index, :event_type, :entity_key,
                            :reg, CAST(:payload AS jsonb), 'EVENT_ID_CONFLICT', 'conflict', :deadline_at,
                            now(), 'EVENT_ID_CONFLICT', 'Event ID was reused with different content', :conflicts_with)
                        ON CONFLICT (source_namespace, dedupe_key) DO NOTHING RETURNING id
                    '''), conflict_params)
                    conflict_id = conflict.scalar_one_or_none()
                    if conflict_id is not None:
                        operation = {**params, 'id': conflict_id}
                        producer = OutboxProducer(session, operation)
                        await producer.send_and_wait(event.dlq_topic, {
                            'error_code': 'EVENT_ID_CONFLICT',
                            'error_message': 'Event ID was reused with different content',
                            'original_message': event.payload,
                            'conflicts_with': existing['id'],
                            **record_coordinates(operation_record(operation)),
                        })
                        ids.append(conflict_id)
        return ids

    async def _terminal_error(self, session, operation, code: str, message: str, dlq_topic: str):
        producer = OutboxProducer(session, operation)
        await producer.send_and_wait(dlq_topic, {
            'original_message': operation['payload'], 'error_code': code, 'error_message': message,
            **record_coordinates(operation_record(operation)),
        })
        await self._finish(session, operation['id'], 'failed', code, message,
                           {'outgoing': producer.messages})

    async def _finish(self, session, operation_id: int, status: str,
                      code=None, message=None, result=None):
        await session.execute(text('''UPDATE integration_events
            SET status = :status, completed_at = now(), error_code = :code,
                error_message = :message, result = CAST(:result AS jsonb)
            WHERE id = :id'''), {'id': operation_id, 'status': status, 'code': code,
                                 'message': message, 'result': _json(result)})

    async def execute_one(self, dispatch, *, dlq_topic_for, snapshot_types=(), resolver_types=(), user_dependency_types=('update-user-departments',)) -> bool:
        """Claim, execute and atomically commit status plus any staged outcomes."""
        async with self.session_factory() as session:
            async with session.begin():
                claimed = await session.execute(text(f'''
                    SELECT e.* FROM integration_events e
                    WHERE e.source_namespace = :namespace
                      AND e.status IN ('received', 'retry_wait', 'waiting_dependency')
                      AND e.available_at <= now()
                      AND NOT EXISTS (
                        SELECT 1 FROM integration_events prior
                        WHERE prior.source_namespace = e.source_namespace
                          AND prior.entity_key = e.entity_key AND prior.id < e.id
                          AND prior.status IN {_ACTIVE}
                          AND NOT (e.event_type = ANY(CAST(:resolver_types AS text[]))
                                   AND prior.status = 'waiting_dependency'))
                    ORDER BY e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED
                '''), {'namespace': self.settings.INTEGRATION_SOURCE_NAMESPACE,
                       'resolver_types': list(dict.fromkeys((*snapshot_types, *resolver_types)))})
                operation = claimed.mappings().first()
                if operation is None:
                    return False
                operation = dict(operation)
                operation['_user_dependency_types'] = user_dependency_types
                attempts = operation['attempts'] + 1
                await session.execute(text('''UPDATE integration_events
                    SET status = 'running', attempts = :attempts, last_attempt_at = now()
                    WHERE id = :id'''), {'id': operation['id'], 'attempts': attempts})
                dlq_topic = dlq_topic_for(operation['event_type'])
                if operation['deadline_at'] <= _now():
                    await self._terminal_error(session, operation, 'EVENT_DEADLINE_EXCEEDED',
                                               'Operation deadline expired before execution', dlq_topic)
                    return True
                if operation['disposition']:
                    await self._terminal_error(session, operation, operation['disposition'],
                                               'Incoming event did not pass format validation', dlq_topic)
                    return True
                if operation['source_revision'] is not None and operation['reg'] is not None and operation['revision_stream'] is not None:
                    latest = (await session.execute(text('''SELECT max(source_revision)
                        FROM integration_events WHERE source_namespace = :namespace AND reg = :reg
                        AND revision_stream = :stream AND status = 'succeeded' AND id <> :id'''),
                        {'namespace': operation['source_namespace'], 'reg': operation['reg'],
                         'stream': operation['revision_stream'], 'id': operation['id']})).scalar_one_or_none()
                    if latest is not None and operation['source_revision'] <= latest:
                        await self._finish(session, operation['id'], 'superseded',
                                           'STALE_REVISION', 'A newer or equal snapshot is already confirmed',
                                           {'confirmed_revision': latest})
                        return True
                producer = OutboxProducer(session, operation)
                try:
                    # If dispatch stages output then fails, that output must roll
                    # back before the retry is saved in the outer transaction.
                    async with session.begin_nested():
                        await dispatch(operation_record(operation), producer)
                except asyncio.CancelledError:
                    raise  # Outer transaction rollback leaves work claimable.
                except SQLAlchemyError:
                    raise  # A broken DB transaction must not masquerade as a business failure.
                except Exception as error:
                    code = error.code if isinstance(error, (WaitingDependency, RetryableOperation)) else 'UNEXPECTED_ERROR'
                    message = str(error)
                    delay = self.delay(attempts)
                    now = _now()
                    if (not isinstance(error, WaitingDependency) and attempts >= self.settings.INTEGRATION_RETRY_ATTEMPTS) or now + timedelta(seconds=delay) >= operation['deadline_at']:
                        await self._terminal_error(session, operation, 'EVENT_RETRY_EXHAUSTED',
                                                   f'{code}: {message}', dlq_topic)
                    else:
                        await session.execute(text('''UPDATE integration_events
                            SET status = :status, available_at = :available_at, error_code = :code,
                                error_message = :message WHERE id = :id'''),
                            {'id': operation['id'], 'status': 'waiting_dependency' if isinstance(error, WaitingDependency) else 'retry_wait',
                             'available_at': now + timedelta(seconds=delay), 'code': code, 'message': message})
                    return True
                failure = producer.failure
                await self._finish(session, operation['id'], 'failed' if failure else 'succeeded',
                                   failure.get('error_code') if failure else None,
                                   failure.get('error_message') if failure else None,
                                   {'outgoing': producer.messages})
                return True

    async def publish_one(self, kafka_producer) -> bool:
        """Send an already committed outbox row; duplicates keep the same ID."""
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(text('''
                    SELECT o.* FROM integration_outbox o JOIN integration_events e ON e.id = o.operation_id
                    WHERE e.source_namespace = :namespace AND o.sent_at IS NULL AND o.available_at <= now()
                    AND NOT EXISTS (SELECT 1 FROM integration_outbox prior
                        WHERE prior.operation_id = o.operation_id AND prior.sequence < o.sequence AND prior.sent_at IS NULL)
                    ORDER BY o.operation_id, o.sequence LIMIT 1 FOR UPDATE OF o SKIP LOCKED
                '''), {'namespace': self.settings.INTEGRATION_SOURCE_NAMESPACE})
                row = result.mappings().first()
                if row is None:
                    return False
                headers = [(name, None if value is None else bytes.fromhex(value)) for name, value in row['headers']]
                try:
                    await kafka_producer.send_and_wait(row['topic'], row['payload'], key=row['message_key'], headers=headers)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await session.execute(text('''UPDATE integration_outbox
                        SET attempts = attempts + 1, available_at = :available_at, error_message = :error
                        WHERE id = :id'''), {'id': row['id'], 'error': str(error),
                        'available_at': _now() + timedelta(seconds=self.delay(row['attempts'] + 1))})
                    logger.warning('Outbox delivery deferred event_id=%s: %s', row['id'], type(error).__name__)
                    return True
                await session.execute(text('''UPDATE integration_outbox SET attempts = attempts + 1,
                    sent_at = now(), error_message = NULL WHERE id = :id'''), {'id': row['id']})
                return True
