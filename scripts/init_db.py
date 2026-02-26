"""
Создание таблиц для тестовой БД (в т.ч. в Docker).

Запуск:
  python scripts/init_db.py

Требует .env с DB_URL или PG_* (postgresql+asyncpg). Создаёт reg_services
и department_service_mapping, вставляет тестовый reg при --seed.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Создание таблиц в БД")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Вставить тестовый reg в reg_services",
    )
    parser.add_argument(
        "--reg",
        default="350832",
        help="Значение reg для --seed (по умолчанию 350832)",
    )
    parser.add_argument(
        "--base-url",
        default="https://platform.kodeks.expert",
        help="base_url для --seed",
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

    engine = create_async_engine(
        db_url,
        pool_size=1,
        pool_pre_ping=True,
    )

    print(f"Подключение к БД (таблицы: {tbl_reg!r}, {tbl_dsm!r})...")
    async with engine.begin() as conn:
        for m in migrations:
            sql = m.read_text(encoding="utf-8")
            sql = sql.replace("reg_services", tbl_reg).replace("department_service_mapping", tbl_dsm)
            print(f"  Выполняю {m.name}...")
            await conn.execute(text(sql))

    if args.seed:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"""
                    INSERT INTO {tbl_reg} (reg_number, base_url)
                    VALUES (:reg, :base_url)
                    ON CONFLICT (reg_number) DO UPDATE SET base_url = EXCLUDED.base_url
                """),
                {"reg": args.reg, "base_url": args.base_url},
            )
        print(f"  reg={args.reg!r} добавлен в {tbl_reg}")

    await engine.dispose()
    print("Готово.")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
