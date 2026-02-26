"""
Показать записи из таблицы маппинга отделов (department_service_mapping) по reg.

Запуск:
  python scripts/show_department_mapping.py
  python scripts/show_department_mapping.py --reg 350832
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Показать маппинг отделов по reg")
    parser.add_argument("--reg", default="350832", help="reg (по умолчанию 350832; пустая строка — все записи)")
    args = parser.parse_args()

    settings = get_settings()
    tbl = settings.DB_TABLE_DEPARTMENT_MAPPING
    engine = create_async_engine(settings.DB_URL, pool_size=1)

    if args.reg and args.reg.strip():
        sql = text(f"""
            SELECT id, department_id::text, service_group_name, reg, client_id, client_name, mapping_status
            FROM {tbl} WHERE reg = :reg
            ORDER BY id
        """)
        params = {"reg": args.reg.strip()}
    else:
        sql = text(f"""
            SELECT id, department_id::text, service_group_name, reg, client_id, client_name, mapping_status
            FROM {tbl}
            ORDER BY reg, id
        """)
        params = {}

    async with engine.connect() as conn:
        res = await conn.execute(sql, params)
        rows = res.fetchall()

    await engine.dispose()

    if not rows:
        print("Записей нет.")
        return

    print(f"Таблица {tbl!r}, записей: {len(rows)}\n")
    col_names = ["id", "department_id", "service_group_name", "reg", "client_id", "client_name", "mapping_status"]
    widths = [len(c) for c in col_names]
    for r in rows:
        for i, v in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(v)))
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*col_names))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for r in rows:
        print(fmt.format(*[str(x) for x in r]))
    print()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
