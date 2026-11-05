from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common import editor_portal
from common.editor_portal import EditorModule, EditorSnapshot, read_editor_snapshot
from common.exceptions import NetworkError, ParseError


DSN = "postgresql+asyncpg://portal-reader@localhost/portal"
ACTIVE = {"company_id": 42, "available": True, "mark_deletion": False}


@pytest.fixture
def portal(monkeypatch):
    def connect(*responses):
        active = False
        fetch = AsyncMock(side_effect=responses)

        async def checked_fetch(*args):
            assert active
            return await fetch(*args)

        @asynccontextmanager
        async def transaction(*, isolation, readonly):
            nonlocal active
            assert (isolation, readonly) == ("repeatable_read", True)
            active = True
            try:
                yield
            finally:
                active = False

        connection = SimpleNamespace(fetch=checked_fetch, transaction=transaction, close=AsyncMock())
        connect_mock = AsyncMock(return_value=connection)
        monkeypatch.setattr(editor_portal.asyncpg, "connect", connect_mock)
        return connect_mock, connection, fetch

    return connect


@pytest.mark.asyncio
async def test_snapshot_keeps_every_linked_boundary_and_uses_email_localpart(portal):
    connect, connection, fetch = portal(
        [ACTIVE],
        [{"id": 51}],
        [
            {"link": "https://catalog.test/?infoboard=enabled", "editor_email": "User.Name+plus@example.test"},
            {"link": "https://catalog.test/?infoboard=disabled", "editor_email": None},
            {"link": None, "editor_email": None},
        ],
    )
    assert await read_editor_snapshot(DSN, "000123") == EditorSnapshot((
        EditorModule("https://catalog.test/?infoboard=enabled", "User.Name+plus"),
        EditorModule("https://catalog.test/?infoboard=disabled", None),
    ))
    assert connect.await_args.args[0] == "postgresql://portal-reader@localhost/portal"
    assert fetch.await_args_list[0].args[1:] == ("000123",)
    assert fetch.await_args_list[1].args[1:] == (42,)
    assert fetch.await_args_list[2].args[1:] == (
        51,
        True,
        ("Действующий пользователь", "Потенциальный пользователь", "Администратор компании"),
    )
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [
    {**ACTIVE, "available": False},
    {**ACTIVE, "mark_deletion": True},
])
async def test_inactive_company_keeps_boundaries_and_passes_revocation_guard(portal, owner):
    _, _, fetch = portal([owner], [{"id": 51}], [{"link": "?infoboard=root", "editor_email": None}])
    assert await read_editor_snapshot(DSN, "123") == EditorSnapshot((EditorModule("?infoboard=root", None),))
    assert fetch.await_args.args[2] is False


@pytest.mark.asyncio
async def test_missing_registration_is_authoritative_empty(portal):
    _, connection, fetch = portal([])
    assert await read_editor_snapshot(DSN, "123") == EditorSnapshot(())
    fetch.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("responses", "code"), [
    (([ACTIVE, {**ACTIVE, "company_id": 43}],), "EDITOR_PORTAL_AMBIGUOUS_REG"),
    (([{"company_id": None}],), "EDITOR_PORTAL_AMBIGUOUS_REG"),
    (([ACTIVE], [{"id": 51}, {"id": 52}]), "EDITOR_PORTAL_AMBIGUOUS_STRUCTURE"),
    (([ACTIVE], [{"id": 51}], [{"link": "?infoboard=root", "editor_email": "missing-domain"}]),
     "EDITOR_PORTAL_INVALID_EMAIL"),
])
async def test_ambiguous_or_malformed_authority_fails_without_empty_snapshot(portal, responses, code):
    _, connection, _ = portal(*responses)
    with pytest.raises(ParseError) as error:
        await read_editor_snapshot(DSN, "123")
    assert error.value.code == code
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_failure_is_not_an_authoritative_empty_snapshot(portal):
    _, connection, _ = portal([ACTIVE], TimeoutError("DSN details would go here"))
    with pytest.raises(NetworkError) as error:
        await read_editor_snapshot(DSN, "123")
    assert error.value.code == "EDITOR_PORTAL_READ_FAILED"
    assert "DSN" not in str(error.value)
    connection.close.assert_awaited_once()
