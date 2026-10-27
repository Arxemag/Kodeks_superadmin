"""Advisory-lock lifecycle tests; no PostgreSQL connection is opened."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from common import catalog_lock
from common.exceptions import NetworkError


@pytest.fixture
def lock_db(monkeypatch):
    events = []
    execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: True))

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
    async def sessions():
        events.append("open")
        try:
            yield SimpleNamespace(begin=transaction, execute=execute)
        finally:
            events.append("close")

    monkeypatch.setattr(catalog_lock, "get_db_session", sessions)
    monkeypatch.setattr(catalog_lock, "get_settings", lambda: SimpleNamespace(CATALOG_LOCK_TIMEOUT=2.5))
    return events, execute


@pytest.mark.parametrize("url", ["HTTPS://CATALOG:443", "https://catalog/", "https://catalog:443/"])
def test_equivalent_url_spellings_share_lock(url):
    assert catalog_lock._lock_key(url) == catalog_lock._lock_key("https://catalog")


def test_distinct_paths_and_ports_remain_distinct():
    assert catalog_lock._lock_key("https://catalog/a") != catalog_lock._lock_key("https://catalog/b")
    assert catalog_lock._lock_key("https://catalog:8443") != catalog_lock._lock_key("https://catalog")
    assert catalog_lock.canonical_catalog_url("https://CATALOG/SomePath/") == "https://catalog/SomePath"


@pytest.mark.asyncio
async def test_transaction_owns_lock_until_body_finishes(lock_db):
    events, execute = lock_db
    async with catalog_lock.catalog_operation("https://catalog"):
        assert events == ["open", "begin"]
        assert execute.await_count == 1
        lock = execute.await_args
        assert "pg_try_advisory_xact_lock" in str(lock.args[0])
        assert lock.args[1] == {"key": catalog_lock._lock_key("https://catalog")}
    assert events == ["open", "begin", "commit", "close"]
    assert catalog_lock._held.get() == ()


@pytest.mark.asyncio
async def test_nested_same_task_reuses_lock(lock_db):
    events, execute = lock_db
    async with catalog_lock.catalog_operation("https://catalog"):
        async with catalog_lock.catalog_operation("https://CATALOG:443/"):
            assert execute.await_count == 1
    assert events.count("open") == 1


@pytest.mark.asyncio
async def test_child_task_does_not_inherit_lock_ownership(lock_db):
    events, execute = lock_db

    async def child():
        async with catalog_lock.catalog_operation("https://catalog"):
            pass

    async with catalog_lock.catalog_operation("https://catalog"):
        # Mocked SQL does not block, but each task must acquire its own lock.
        await asyncio.create_task(child())
    assert execute.await_count == 2
    assert events.count("open") == 2


@pytest.mark.asyncio
async def test_body_failure_rolls_back_and_clears_reentrancy(lock_db):
    events, execute = lock_db
    with pytest.raises(ValueError, match="body"):
        async with catalog_lock.catalog_operation("https://catalog"):
            raise ValueError("body")
    assert events == ["open", "begin", "rollback", "close"]
    assert catalog_lock._held.get() == ()
    async with catalog_lock.catalog_operation("https://catalog"):
        pass
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_task_cancellation_releases_transaction(lock_db):
    events, _ = lock_db
    entered = asyncio.Event()

    async def waiting():
        async with catalog_lock.catalog_operation("https://catalog"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(waiting())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["open", "begin", "rollback", "close"]


@pytest.mark.asyncio
async def test_database_lock_failure_is_retryable_network_error(lock_db):
    events, execute = lock_db
    execute.side_effect = OperationalError("lock", {}, Exception("timeout"))
    with pytest.raises(NetworkError) as caught:
        async with catalog_lock.catalog_operation("https://catalog"):
            pytest.fail("Lock acquisition failed: protected body must not execute")
    assert caught.value.code == "CATALOG_LOCK_FAILED"
    assert events == ["open", "begin", "rollback", "close"]
    assert catalog_lock._held.get() == ()


@pytest.mark.asyncio
async def test_false_lock_attempt_returns_connection_before_poll_sleep(lock_db, monkeypatch):
    events, execute = lock_db
    execute.side_effect = [MagicMock(scalar_one=lambda: False), MagicMock(scalar_one=lambda: True)]

    async def sleep(delay):
        assert 0 < delay <= 0.1
        assert events == ["open", "begin", "commit", "close"]

    monkeypatch.setattr(catalog_lock.asyncio, "sleep", sleep)
    async with catalog_lock.catalog_operation("https://catalog"):
        assert events == ["open", "begin", "commit", "close", "open", "begin"]
    assert events.count("close") == 2


@pytest.mark.asyncio
async def test_busy_catalog_times_out_without_retaining_connection(lock_db, monkeypatch):
    events, execute = lock_db
    execute.return_value.scalar_one = lambda: False
    clock = iter([0, 0, 3])
    monkeypatch.setattr(catalog_lock, "monotonic", lambda: next(clock))
    with pytest.raises(NetworkError) as caught:
        async with catalog_lock.catalog_operation("https://catalog"):
            pytest.fail("Busy lock must not admit business operations")
    assert caught.value.code == "CATALOG_LOCK_TIMEOUT"
    assert events == ["open", "begin", "commit", "close"]


@pytest.mark.asyncio
async def test_cancellation_while_waiting_holds_no_pool_connection(lock_db, monkeypatch):
    events, execute = lock_db
    execute.return_value.scalar_one = lambda: False

    async def cancel_wait(delay):
        assert events == ["open", "begin", "commit", "close"]
        raise asyncio.CancelledError

    monkeypatch.setattr(catalog_lock.asyncio, "sleep", cancel_wait)
    with pytest.raises(asyncio.CancelledError):
        async with catalog_lock.catalog_operation("https://catalog"):
            pytest.fail("Cancelled waiter must not enter the operation")
    assert catalog_lock._held.get() == ()


@pytest.mark.asyncio
async def test_business_database_exception_is_not_acquisition_error(lock_db):
    error = OperationalError("business write", {}, Exception("business failure"))
    with pytest.raises(OperationalError) as caught:
        async with catalog_lock.catalog_operation("https://catalog"):
            raise error
    assert caught.value is error
