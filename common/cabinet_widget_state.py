"""Durable widget transitions for one physical catalog board.

Callers must hold catalog_operation(base_url) around get, preparation, put and
the external write. Store pending before the external write; clear it only
after a fresh native read confirms the result. The database cannot transact
atomically with the catalog.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.catalog_lock import canonical_catalog_url
from common.db import get_db_session
from common.exceptions import NetworkError, ParseError


@dataclass
class CabinetWidgetState:
    archive: list[dict] = field(default_factory=list)
    pending: dict | None = None


def _invalid() -> ParseError:
    return ParseError(code="CABINET_WIDGET_STATE_INVALID", message="Invalid cabinet widget state")


def _identity(base_url: str, reg: str, board_id: str) -> str:
    if any(not isinstance(value, str) or not value or value != value.strip() for value in (reg, board_id)):
        raise _invalid()
    try:
        return canonical_catalog_url(base_url)
    except (ValueError, TypeError, AttributeError) as error:
        raise _invalid() from error


def _json_value(value) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for element in value:
            _json_value(element)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for element in value.values():
            _json_value(element)
        return
    raise _invalid()


def _widgets(value) -> None:
    if not isinstance(value, list) or any(not isinstance(widget, dict) for widget in value):
        raise _invalid()
    _json_value(value)


def _archive(value) -> None:
    if not isinstance(value, list):
        raise _invalid()
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {"widget", "index"}
                or not isinstance(entry["widget"], dict)
                or type(entry["index"]) is not int or entry["index"] < 0):
            raise _invalid()
        _json_value(entry["widget"])


def _validate(state: CabinetWidgetState) -> None:
    if not isinstance(state, CabinetWidgetState):
        raise _invalid()
    try:
        _archive(state.archive)
        if state.pending is not None:
            if not isinstance(state.pending, dict) or set(state.pending) != {"before", "after", "archive"}:
                raise _invalid()
            _widgets(state.pending["before"])
            _widgets(state.pending["after"])
            _archive(state.pending["archive"])
    except RecursionError as error:
        raise _invalid() from error


def _decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, RecursionError) as error:
            raise _invalid() from error
    return value


class CabinetWidgetStateRepository:
    async def get(self, base_url: str, reg: str, board_id: str) -> CabinetWidgetState:
        catalog_url = _identity(base_url, reg, board_id)
        try:
            async with get_db_session() as session:
                result = await session.execute(text("""
                    SELECT reg, archive, pending FROM catalog_cabinet_widget_state
                    WHERE catalog_url = :catalog_url AND board_id = :board_id
                """), {"catalog_url": catalog_url, "board_id": board_id})
                row = result.first()
        except SQLAlchemyError as error:
            raise NetworkError(code="CABINET_WIDGET_STATE_READ_FAILED", message="Cannot read cabinet widget state") from error
        if row is None:
            return CabinetWidgetState()
        if row[0] != reg:
            raise ParseError(code="CABINET_WIDGET_OWNER_CONFLICT", message="Cabinet widget state belongs to another registration")
        state = CabinetWidgetState(archive=_decode(row[1]), pending=_decode(row[2]))
        _validate(state)
        return state

    async def put(self, base_url: str, reg: str, board_id: str, state: CabinetWidgetState) -> None:
        catalog_url = _identity(base_url, reg, board_id)
        _validate(state)
        archive = json.dumps(state.archive, ensure_ascii=False, allow_nan=False)
        pending = json.dumps(state.pending, ensure_ascii=False, allow_nan=False) if state.pending is not None else None
        try:
            async with get_db_session() as session:
                async with session.begin():
                    result = await session.execute(text("""
                        INSERT INTO catalog_cabinet_widget_state (catalog_url, board_id, reg, archive, pending)
                        VALUES (:catalog_url, :board_id, :reg, CAST(:archive AS jsonb), CAST(:pending AS jsonb))
                        ON CONFLICT (catalog_url, board_id) DO UPDATE
                        SET archive = EXCLUDED.archive, pending = EXCLUDED.pending, updated_at = now()
                        WHERE catalog_cabinet_widget_state.reg = EXCLUDED.reg
                        RETURNING reg
                    """), {"catalog_url": catalog_url, "board_id": board_id, "reg": reg,
                           "archive": archive, "pending": pending})
                    if result.scalar_one_or_none() is None:
                        raise ParseError(code="CABINET_WIDGET_OWNER_CONFLICT", message="Cabinet widget state belongs to another registration")
        except SQLAlchemyError as error:
            raise NetworkError(code="CABINET_WIDGET_STATE_WRITE_FAILED", message="Cannot store cabinet widget state") from error
