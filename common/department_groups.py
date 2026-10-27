"""Resolve platform department IDs through confirmed catalog bindings."""
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.config import get_settings
from common.db import get_db_session
from common.exceptions import NetworkError


class DepartmentGroupRepository:
    async def resolve(self, reg: str, department_ids: list[str]) -> set[int]:
        if not department_ids:
            return set()
        table = get_settings().DB_TABLE_DEPARTMENT_MAPPING
        try:
            async with get_db_session() as session:
                result = await session.execute(text(
                    f"SELECT department_id::text, catalog_group_id FROM {table} "
                    "WHERE reg = :reg AND mapping_status = 'active'"
                ), {"reg": reg})
                bindings = {str(row[0]): row[1] for row in result}
        except SQLAlchemyError as error:
            raise NetworkError(code="DEPARTMENT_MAPPING_UNAVAILABLE", message="Cannot read department bindings") from error
        ids = set()
        for department in department_ids:
            group = bindings.get(department)
            if type(group) is not int or group < 0:
                raise NetworkError(code="DEPARTMENT_GROUP_NOT_READY", message="Company synchronisation must confirm department group IDs first")
            ids.add(group)
        return ids
