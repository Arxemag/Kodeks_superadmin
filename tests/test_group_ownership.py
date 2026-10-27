"""Ownership persistence must not claim existing manual assignments."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from common import group_ownership
from common.exceptions import NetworkError, ParseError
from common.group_ownership import GroupOwnershipRepository


@pytest.fixture
def ownership_db(monkeypatch):
    events = []
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def transaction():
        events.append("begin")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    @asynccontextmanager
    async def session():
        events.append("open")
        try:
            yield SimpleNamespace(execute=execute, begin=transaction)
        finally:
            events.append("close")

    monkeypatch.setattr(group_ownership, "get_db_session", session)
    return events, execute, result


@pytest.mark.asyncio
async def test_first_seen_user_has_no_owned_groups(ownership_db):
    events, execute, _ = ownership_db
    assert await GroupOwnershipRepository().get("company", "alice") == set()
    assert "INSERT" not in str(execute.call_args.args[0])
    assert execute.call_args.args[1] == {"reg": "company", "uid": "alice"}
    assert events == ["open", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [[9, 2, 9], "[9, 2, 9]"])
async def test_get_returns_only_recorded_integer_ids(ownership_db, stored):
    _, _, result = ownership_db
    result.scalar_one_or_none.return_value = stored
    assert await GroupOwnershipRepository().get("company", "alice") == {2, 9}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ['{broken', {"kw": [2]}, [True], ["2"], [-1], [1.5]])
async def test_invalid_stored_state_is_not_treated_as_empty(ownership_db, stored):
    _, _, result = ownership_db
    result.scalar_one_or_none.return_value = stored
    with pytest.raises(ParseError) as error:
        await GroupOwnershipRepository().get("company", "alice")
    assert error.value.code == "GROUP_OWNERSHIP_INVALID"


@pytest.mark.asyncio
async def test_set_is_transactional_upsert_and_canonical_json(ownership_db):
    events, execute, _ = ownership_db
    await GroupOwnershipRepository().set("company", "alice", {9, 2})
    query, values = execute.call_args.args
    assert "ON CONFLICT (reg, user_uid)" in str(query)
    assert "updated_at = now()" in str(query)
    assert values == {"reg": "company", "uid": "alice", "group_ids": "[2, 9]"}
    assert events == ["open", "begin", "commit", "close"]


@pytest.mark.asyncio
async def test_empty_owned_set_is_persisted_without_deleting_user(ownership_db):
    _, execute, _ = ownership_db
    await GroupOwnershipRepository().set("company", "alice", set())
    assert execute.call_args.args[1]["group_ids"] == "[]"
    assert "DELETE" not in str(execute.call_args.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [{True}, {"1"}, {-1}, [2.0], "[2]", None])
async def test_invalid_write_fails_before_opening_database(ownership_db, ids):
    events, execute, _ = ownership_db
    with pytest.raises(ParseError):
        await GroupOwnershipRepository().set("company", "alice", ids)
    assert events == []
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set"])
async def test_invalid_identity_fails_before_database(ownership_db, operation):
    events, _, _ = ownership_db
    with pytest.raises(ParseError):
        if operation == "get":
            await GroupOwnershipRepository().get("", "alice")
        else:
            await GroupOwnershipRepository().set("company", " ", {1})
    assert events == []


@pytest.mark.asyncio
async def test_database_write_failure_rolls_back_and_is_retryable(ownership_db):
    events, execute, _ = ownership_db
    execute.side_effect = OperationalError("upsert", {}, Exception("offline"))
    with pytest.raises(NetworkError) as error:
        await GroupOwnershipRepository().set("company", "alice", {1})
    assert error.value.code == "GROUP_OWNERSHIP_WRITE_FAILED"
    assert events == ["open", "begin", "rollback", "close"]


@pytest.mark.asyncio
async def test_database_read_failure_is_not_empty_ownership(ownership_db):
    _, execute, _ = ownership_db
    execute.side_effect = OperationalError("select", {}, Exception("offline"))
    with pytest.raises(NetworkError) as error:
        await GroupOwnershipRepository().get("company", "alice")
    assert error.value.code == "GROUP_OWNERSHIP_READ_FAILED"


@pytest.mark.asyncio
async def test_shared_groups_are_scoped_to_canonical_catalog_and_user(ownership_db, monkeypatch):
    _, execute, result = ownership_db
    monkeypatch.setattr(group_ownership, 'get_settings', lambda: SimpleNamespace(DB_TABLE_REG_SERVICES='reg_services'))
    result.scalar_one_or_none.return_value = [3]
    execute.side_effect = [result, [
        ('reg-b', [3, 4], 'HTTPS://CATALOG:443/'),
        ('reg-c', [8], 'https://another-catalog/'),
    ]]
    owned, shared = await GroupOwnershipRepository().get_with_shared(
        'reg-a', 'alice', 'https://catalog/', desired=set(), current={3, 4, 8},
    )
    assert owned == {3}
    assert shared == {3, 4}
    assert 'LEFT JOIN reg_services' in str(execute.await_args.args[0])
    assert execute.await_args.args[1] == {'uid': 'alice', 'reg': 'reg-a'}


@pytest.mark.asyncio
@pytest.mark.parametrize('other_url', [None, 'not-a-url'])
async def test_missing_catalog_blocks_only_a_potential_shared_removal(ownership_db, monkeypatch, other_url):
    _, execute, result = ownership_db
    monkeypatch.setattr(group_ownership, 'get_settings', lambda: SimpleNamespace(DB_TABLE_REG_SERVICES='reg_services'))
    result.scalar_one_or_none.return_value = [3, 5]
    execute.side_effect = [result, [('reg-b', [3], other_url)]]
    with pytest.raises(ParseError) as error:
        await GroupOwnershipRepository().get_with_shared(
            'reg-a', 'alice', 'https://catalog/', desired={5}, current={3, 5},
        )
    assert error.value.code == 'GROUP_SHARING_UNKNOWN'


@pytest.mark.asyncio
async def test_unknown_other_catalog_does_not_block_unrelated_removal(ownership_db, monkeypatch):
    _, execute, result = ownership_db
    monkeypatch.setattr(group_ownership, 'get_settings', lambda: SimpleNamespace(DB_TABLE_REG_SERVICES='reg_services'))
    result.scalar_one_or_none.return_value = [3]
    execute.side_effect = [result, [('reg-b', [9], None)]]
    assert await GroupOwnershipRepository().get_with_shared(
        'reg-a', 'alice', 'https://catalog/', desired=set(), current={3},
    ) == ({3}, set())


@pytest.mark.asyncio
async def test_shared_ownership_read_failure_is_retryable(ownership_db, monkeypatch):
    _, execute, result = ownership_db
    monkeypatch.setattr(group_ownership, 'get_settings', lambda: SimpleNamespace(DB_TABLE_REG_SERVICES='reg_services'))
    execute.side_effect = [result, OperationalError('select', {}, Exception('offline'))]
    with pytest.raises(NetworkError) as error:
        await GroupOwnershipRepository().get_with_shared(
            'reg-a', 'alice', 'https://catalog/', desired=set(), current={3},
        )
    assert error.value.code == 'GROUP_OWNERSHIP_READ_FAILED'
