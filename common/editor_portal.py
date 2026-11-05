"""Read the Portal state that determines who may edit digital cabinets."""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from common.exceptions import ConfigError, NetworkError, ParseError


_EDITOR_ROLES = (
    "Действующий пользователь",
    "Потенциальный пользователь",
    "Администратор компании",
)


@dataclass(frozen=True)
class EditorModule:
    link: str
    editor_uid: str | None


@dataclass(frozen=True)
class EditorSnapshot:
    modules: tuple[EditorModule, ...]


_REG_OWNERS = """
    SELECT DISTINCT r."companyId" AS owner_id, c.id AS company_id,
                    c.available, c.mark_deletion
    FROM regs r
    LEFT JOIN companies c ON c.id = r."companyId"
    WHERE r.reg_number = $1
"""

_COMPANY_STRUCTURES = """
    SELECT id FROM structs
    WHERE "companyId" = $1 AND is_template IS FALSE
    ORDER BY id
"""

_STRUCTURE_MODULES = """
    SELECT m.link,
           CASE WHEN $2::boolean
                     AND m.available IS TRUE
                     AND m."settingDC" IS TRUE
                     AND uc.mark_deletion IS FALSE
                     AND uc.status::text = ANY($3::text[])
                     AND u.available IS TRUE
                     AND u.role::text = ANY($3::text[])
                THEN u.email END AS editor_email
    FROM modules m
    JOIN structs s ON s.id = m."structId"
    LEFT JOIN user_companies uc ON uc.id = m."mainUserModuleId"
                              AND uc."companyId" = s."companyId"
    LEFT JOIN users u ON u.id = uc."userId"
    WHERE m."structId" = $1
    ORDER BY m.id
"""


def _editor_uid(email: str | None) -> str | None:
    if email is None:
        return None
    local, separator, domain = email.partition("@")
    if not separator or not local or not domain or "@" in domain or any(char.isspace() for char in email):
        raise ParseError(code="EDITOR_PORTAL_INVALID_EMAIL", message="Portal editor email cannot be converted to a catalog UID")
    return local


async def _read_snapshot(connection: asyncpg.Connection, reg: str) -> EditorSnapshot:
    owners = await connection.fetch(_REG_OWNERS, reg)
    if not owners:
        # A successful lookup with no registry row represents removed ownership.
        return EditorSnapshot(())
    if len(owners) != 1 or owners[0]["company_id"] is None:
        raise ParseError(code="EDITOR_PORTAL_AMBIGUOUS_REG", message="Portal registration does not identify a single existing company")
    owner = owners[0]
    structures = await connection.fetch(_COMPANY_STRUCTURES, owner["company_id"])
    if len(structures) > 1:
        raise ParseError(code="EDITOR_PORTAL_AMBIGUOUS_STRUCTURE", message="Portal company has multiple non-template structures")
    if not structures:
        return EditorSnapshot(())
    company_active = owner["available"] is True and owner["mark_deletion"] is False
    rows = await connection.fetch(_STRUCTURE_MODULES, structures[0]["id"], company_active, _EDITOR_ROLES)
    return EditorSnapshot(tuple(
        EditorModule(row["link"].strip(), _editor_uid(row["editor_email"]))
        for row in rows
        if row["link"] and row["link"].strip()
    ))


async def read_editor_snapshot(dsn: str, reg: str) -> EditorSnapshot:
    """Read module boundaries and desired editors in one coherent read-only snapshot."""
    if not dsn.strip():
        raise ConfigError(code="EDITOR_PORTAL_DSN_MISSING", message="Portal snapshot DSN is missing")
    if not reg or not reg.strip():
        raise ParseError(code="EDITOR_PORTAL_INVALID_REG", message="Portal registration is blank")
    # Accept the plain asyncpg URL and the SQLAlchemy spelling used elsewhere.
    dsn = dsn.strip()
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        connection = await asyncpg.connect(dsn, timeout=10, command_timeout=15)
        try:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                return await _read_snapshot(connection, reg)
        finally:
            await connection.close(timeout=5)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, ValueError):
        raise NetworkError(code="EDITOR_PORTAL_READ_FAILED", message="Could not read authoritative Portal editor snapshot") from None
