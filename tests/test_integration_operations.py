from argparse import Namespace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import integration_operations as cli


@pytest.fixture
def session(monkeypatch):
    instance = MagicMock()
    instance.execute = AsyncMock()
    instance.execute.return_value = MagicMock()
    @asynccontextmanager
    async def transaction():
        yield
    @asynccontextmanager
    async def factory():
        yield instance
    instance.begin = transaction
    monkeypatch.setattr(cli, 'get_db_session', factory)
    monkeypatch.setattr(cli, 'get_settings', lambda: SimpleNamespace(
        INTEGRATION_SOURCE_NAMESPACE='test-cluster', INTEGRATION_EVENT_DEADLINE_SECONDS=3600))
    return instance


@pytest.mark.asyncio
async def test_retry_creates_new_linked_row_without_mutating_original(session):
    session.execute.return_value.mappings.return_value.first.return_value = {'id': 101, 'retry_of': 5, 'status': 'received'}
    result = await cli.inspect_operations(Namespace(command='retry', id=5, reason='Parser fixed'))
    sql, params = session.execute.call_args.args
    assert 'INSERT INTO integration_events' in str(sql)
    assert 'UPDATE integration_events' not in str(sql)
    assert "status = 'failed'" in str(sql)
    assert 'retry_of, retry_reason' in str(sql)
    assert params['namespace'] == 'test-cluster'
    assert params['reason'] == 'Parser fixed'
    assert params['dedupe_key'].startswith('manual-retry:5:')
    assert result['id'] == 101


@pytest.mark.asyncio
async def test_retry_does_not_reset_active_or_conflicting_operation(session):
    session.execute.return_value.mappings.return_value.first.return_value = None
    with pytest.raises(ValueError, match='failed operation'):
        await cli.inspect_operations(Namespace(command='retry', id=5, reason='reason'))


@pytest.mark.asyncio
async def test_show_omits_echoed_message_payload(session):
    session.execute.return_value.mappings.return_value.first.return_value = {
        'id': 5, 'result': {'outgoing': [{'event_id': 'stable', 'topic': 'done', 'payload': {'psw': 'private'}}]}}
    result = await cli.inspect_operations(Namespace(command='show', id=5))
    assert result == {'id': 5, 'outgoing': [{'event_id': 'stable', 'topic': 'done'}]}


def test_cli_rejects_blank_retry_reason_before_opening_database():
    with pytest.raises(SystemExit):
        cli.main(['retry', '5', '--reason', ' '])
