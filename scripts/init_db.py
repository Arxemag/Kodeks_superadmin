"""
Создание таблиц и заливка справочника reg -> base_url.

Запуск:
  python scripts/init_db.py            # только создать таблицы (DDL)
  python scripts/init_db.py --seed     # создать таблицы + залить актуальные reg (СТАРОЕ В reg_services УДАЛЯЕТСЯ)
  python scripts/init_db.py --seed --keep-existing   # залить reg, не очищая старые (upsert)

Требует .env с DB_URL или PG_* (postgresql+asyncpg).
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings


# Актуальный справочник reg -> base_url каталога (cabinet-0X). 350832 == cabinet-07 (он же platform.kodeks.expert).
DEFAULT_REGS: list[tuple[str, str]] = [
    ("465301", "https://cabinet-02.kodeks.expert"),
    ("465302", "https://cabinet-03.kodeks.expert"),
    ("465303", "https://cabinet-04.kodeks.expert"),
    ("465304", "https://cabinet-05.kodeks.expert"),
    ("465305", "https://cabinet-06.kodeks.expert"),
    ("350832", "https://cabinet-07.kodeks.expert"),
]


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Создание таблиц и заливка reg -> base_url")
    parser.add_argument("--seed", action="store_true", help="Залить актуальные reg (см. DEFAULT_REGS)")
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Не очищать reg_services перед заливкой (upsert поверх существующих)",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_url = settings.DB_URL
    if not db_url:
        raise SystemExit("DB_URL не задан (проверьте .env)")

    tbl_reg = settings.DB_TABLE_REG_SERVICES
    tbl_dsm = settings.DB_TABLE_DEPARTMENT_MAPPING

    migrations_dir = Path(__file__).parent / "migrations"
    migrations = sorted(migrations_dir.glob("*.sql"))
    if not migrations:
        raise SystemExit(f"Миграции не найдены в {migrations_dir}")

    engine = create_async_engine(db_url, pool_size=1, pool_pre_ping=True)

    print(f"Подключение к БД (таблицы: {tbl_reg!r}, {tbl_dsm!r})...")
    async with engine.begin() as conn:
        for m in migrations:
            sql = m.read_text(encoding="utf-8")
            sql = sql.replace("reg_services", tbl_reg).replace("department_service_mapping", tbl_dsm)
            print(f"  Выполняю {m.name}...")
            await conn.execute(text(sql))

    if args.seed:
        async with engine.begin() as conn:
            if not args.keep_existing:
                print(f"  Очистка {tbl_reg} (старые данные не нужны)...")
                await conn.execute(text(f"TRUNCATE TABLE {tbl_reg}"))
            for reg, base_url in DEFAULT_REGS:
                await conn.execute(
                    text(f"""
                        INSERT INTO {tbl_reg} (reg_number, base_url)
                        VALUES (:reg, :base_url)
                        ON CONFLICT (reg_number) DO UPDATE SET base_url = EXCLUDED.base_url
                    """),
                    {"reg": reg, "base_url": base_url},
                )
            print(f"  Залито reg: {[r for r, _ in DEFAULT_REGS]}")

    await engine.dispose()
    print("Готово.")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
