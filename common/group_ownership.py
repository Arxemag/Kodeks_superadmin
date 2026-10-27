"""Track only groups whose assignment by this integration was confirmed.

An unknown user has no owned groups. Existing catalog membership must not be
adopted wholesale. Call set only after the external write has been read back and
verified, while holding the catalog operation lock. If the process fails before
set commits, the next operation preserves the unrecorded membership rather than
assuming it may remove it. This journal is not a transaction with the catalog.
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.catalog_lock import canonical_catalog_url
from common.config import get_settings
from common.db import get_db_session
from common.exceptions import NetworkError, ParseError


def _group_ids(values) -> set[int]:
    """Never coerce bool, strings, floats or malformed stored JSON into IDs."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ParseError(code="GROUP_OWNERSHIP_INVALID", message="Owned group IDs must be a collection of integers")
    if any(type(value) is not int or value < 0 for value in values):
        raise ParseError(code="GROUP_OWNERSHIP_INVALID", message="Owned group IDs must be nonnegative integers")
    return set(values)


def _identity(reg: str, uid: str) -> None:
    if not isinstance(reg, str) or not reg.strip() or not isinstance(uid, str) or not uid.strip():
        raise ParseError(code="GROUP_OWNERSHIP_INVALID", message="Registration and user login must be nonempty strings")


def _stored_ids(stored) -> set[int]:
    if stored is None:
        return set()
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (TypeError, ValueError) as error:
            raise ParseError(code="GROUP_OWNERSHIP_INVALID", message="Stored group ownership is not valid JSON") from error
    if not isinstance(stored, list):
        raise ParseError(code="GROUP_OWNERSHIP_INVALID", message="Stored group ownership must be a JSON array")
    return _group_ids(stored)


class GroupOwnershipRepository:
    async def get(self, reg: str, uid: str) -> set[int]:
        _identity(reg, uid)
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("SELECT group_ids FROM catalog_user_group_ownership WHERE reg = :reg AND user_uid = :uid"),
                    {"reg": reg, "uid": uid},
                )
                stored = result.scalar_one_or_none()
        except SQLAlchemyError as error:
            raise NetworkError(code="GROUP_OWNERSHIP_READ_FAILED", message="Cannot read confirmed group ownership") from error
        return _stored_ids(stored)

    async def get_with_shared(self, reg: str, uid: str, base_url: str, *, desired: set[int], current: set[int]) -> tuple[set[int], set[int]]:
        """Return this registration's ownership and shared ownership for the same catalog.

        Other registrations can point to different catalogs, even if usernames
        and numeric group IDs coincide. An unresolvable catalog blocks only an
        actual removal of groups that the ambiguous registration also owns.
        """
        _identity(reg, uid)
        desired = _group_ids(desired)
        current = _group_ids(current)
        try:
            catalog = canonical_catalog_url(base_url)
        except ValueError as error:
            raise ParseError(code="GROUP_SHARING_UNKNOWN", message="Cannot identify the current catalog") from error
        owned = await self.get(reg, uid)
        candidates = (owned & current) - desired
        table = get_settings().DB_TABLE_REG_SERVICES
        try:
            async with get_db_session() as session:
                result = await session.execute(text(f"""
                    SELECT o.reg, o.group_ids, s.base_url
                    FROM catalog_user_group_ownership AS o
                    LEFT JOIN {table} AS s ON s.reg_number = o.reg
                    WHERE o.user_uid = :uid AND o.reg <> :reg
                """), {"reg": reg, "uid": uid})
                rows = list(result)
        except SQLAlchemyError as error:
            raise NetworkError(code="GROUP_OWNERSHIP_READ_FAILED", message="Cannot read shared group ownership") from error
        shared: set[int] = set()
        for other_reg, raw_ids, other_url in rows:
            other_ids = _stored_ids(raw_ids)
            try:
                other_catalog = canonical_catalog_url(other_url) if isinstance(other_url, str) else None
            except ValueError:
                other_catalog = None
            if other_catalog is None:
                if candidates & other_ids:
                    raise ParseError(code="GROUP_SHARING_UNKNOWN", message=f"Cannot identify catalog for registration {other_reg!r} that may share removed groups")
                continue
            if other_catalog == catalog:
                shared.update(other_ids)
        return owned, shared

    async def set(self, reg: str, uid: str, ids: set[int]) -> None:
        """Persist the confirmed managed set; caller verifies the catalog first."""
        _identity(reg, uid)
        values = sorted(_group_ids(ids))
        try:
            async with get_db_session() as session:
                async with session.begin():
                    await session.execute(
                        text("""
                            INSERT INTO catalog_user_group_ownership (reg, user_uid, group_ids)
                            VALUES (:reg, :uid, CAST(:group_ids AS jsonb))
                            ON CONFLICT (reg, user_uid) DO UPDATE
                            SET group_ids = EXCLUDED.group_ids, updated_at = now()
                        """),
                        {"reg": reg, "uid": uid, "group_ids": json.dumps(values)},
                    )
        except SQLAlchemyError as error:
            raise NetworkError(code="GROUP_OWNERSHIP_WRITE_FAILED", message="Cannot store confirmed group ownership") from error
