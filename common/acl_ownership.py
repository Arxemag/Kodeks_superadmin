"""Track only individual ACL assignments confirmed as added by this integration.

Existing ACLs of adopted groups are never implicitly owned. Access and view
controls are independent. Call replace only after fresh catalog verification,
under catalog_operation. This metadata is not a transaction with the catalog;
after a crash before metadata persistence, unrecorded grants are preserved.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.catalog_lock import canonical_catalog_url
from common.db import get_db_session
from common.exceptions import NetworkError, ParseError


def _ids(values) -> set[int]:
    if not isinstance(values, (set, frozenset, list, tuple)) or any(type(value) is not int or value < 0 for value in values):
        raise ParseError(code="ACL_OWNERSHIP_INVALID", message="ACL group IDs must be a collection of nonnegative integers")
    return set(values)


def _field(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"acl_infoboard_[a-zA-Z0-9_]+", name):
        raise ParseError(code="ACL_OWNERSHIP_INVALID", message="Invalid ACL ownership control name")


def plan_acl_assignments(
    current: dict[str, set[int]], owned: dict[str, set[int]], desired: dict[str, set[int]],
    owned_elsewhere: dict[str, set[int]] | None = None,
) -> tuple[dict[str, list[int]], dict[str, set[int]]]:
    """Preserve manual grants and grants managed by other registrations."""
    overrides = {}
    next_owned = {}
    owned_elsewhere = owned_elsewhere or {}
    for name in sorted(owned.keys() | desired.keys()):
        _field(name)
        if name not in current:
            raise ParseError(code="ACL_BOARD_NOT_FOUND", message=f"Required or previously managed ACL field is absent: {name!r}")
        actual = _ids(current[name])
        previous = _ids(owned.get(name, set()))
        wanted = _ids(desired.get(name, set()))
        other = _ids(owned_elsewhere.get(name, set()))
        overrides[name] = sorted((actual - (previous - other)) | wanted)
        confirmed_owned = (previous & wanted) | (wanted - actual) | (wanted & other)
        if confirmed_owned:
            next_owned[name] = confirmed_owned
    return overrides, next_owned


class AclOwnershipRepository:
    async def get(self, base_url: str, reg: str) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
        catalog_url = canonical_catalog_url(base_url)
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("SELECT reg, control_name, group_ids FROM catalog_acl_ownership WHERE catalog_url = :catalog_url"),
                    {"catalog_url": catalog_url},
                )
                rows = list(result)
        except SQLAlchemyError as error:
            raise NetworkError(code="ACL_OWNERSHIP_READ_FAILED", message="Cannot read confirmed ACL ownership") from error
        ownership = {}
        elsewhere = {}
        for row_reg, name, values in rows:
            _field(name)
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except (ValueError, TypeError) as error:
                    raise ParseError(code="ACL_OWNERSHIP_INVALID", message="Stored ACL ownership is not valid JSON") from error
            if not isinstance(values, list):
                raise ParseError(code="ACL_OWNERSHIP_INVALID", message="Stored ACL ownership must be a JSON array")
            target = ownership if row_reg == reg else elsewhere
            target.setdefault(name, set()).update(_ids(values))
        return ownership, elsewhere

    async def replace(self, base_url: str, reg: str, ownership: dict[str, set[int]]) -> None:
        catalog_url = canonical_catalog_url(base_url)
        values = {}
        for name, groups in ownership.items():
            _field(name)
            ids = _ids(groups)
            if ids:
                values[name] = sorted(ids)
        try:
            async with get_db_session() as session:
                async with session.begin():
                    await session.execute(
                        text("DELETE FROM catalog_acl_ownership WHERE catalog_url = :catalog_url AND reg = :reg"),
                        {"catalog_url": catalog_url, "reg": reg},
                    )
                    for name, ids in values.items():
                        await session.execute(
                            text("INSERT INTO catalog_acl_ownership (catalog_url, reg, control_name, group_ids) VALUES (:catalog_url, :reg, :name, CAST(:group_ids AS jsonb))"),
                            {"catalog_url": catalog_url, "reg": reg, "name": name, "group_ids": json.dumps(ids)},
                        )
        except SQLAlchemyError as error:
            raise NetworkError(code="ACL_OWNERSHIP_WRITE_FAILED", message="Cannot store verified ACL ownership") from error
