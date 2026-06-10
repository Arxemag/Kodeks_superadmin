"""
Прописать (upsert) маппинг reg -> base_url в таблицу reg_services нашей БД.

Использует settings.DB_URL из .env. Запуск:
  python scripts/seed_reg_service.py                 # 465302 -> https://cabinet-03.kodeks.expert/
  python scripts/seed_reg_service.py --reg 465302 --base-url https://cabinet-03.kodeks.expert/
  python scripts/seed_reg_service.py --list          # показать содержимое reg_services
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from common.config import get_settings
from common.logger import get_logger


logger = get_logger("seed_reg_service")

DEFAULT_REG = "465302"
DEFAULT_BASE_URL = "https://cabinet-03.kodeks.expert/"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Upsert reg -> base_url в reg_services")
    p.add_argument("--reg", default=DEFAULT_REG, help=f"reg_number (по умолчанию {DEFAULT_REG})")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"base_url (по умолчанию {DEFAULT_BASE_URL})")
    p.add_argument("--list", action="store_true", help="Только показать текущее содержимое reg_services")
    return p.parse_args()


async def _run() -> None:
    args = _parse_args()
    s = get_settings()
    tbl = s.DB_TABLE_REG_SERVICES
    base_url = args.base_url.rstrip("/") + "/"  # храним с завершающим / как в примере
    logger.info("DB=%s table=%s", s.DB_URL.split("@")[-1], tbl)

    engine = create_async_engine(s.DB_URL, pool_size=1)
    try:
        sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with sm() as session:
            if args.list:
                res = await session.execute(text(f"SELECT reg_number, base_url FROM {tbl} ORDER BY reg_number"))
                rows = list(res)
                print(f"reg_services ({len(rows)}):")
                for r in rows:
                    print(f"  {r[0]} -> {r[1]}")
                return

            await session.execute(
                text(
                    f"""
                    INSERT INTO {tbl} (reg_number, base_url)
                    VALUES (:reg, :base_url)
                    ON CONFLICT (reg_number) DO UPDATE SET base_url = EXCLUDED.base_url
                    """
                ),
                {"reg": args.reg, "base_url": base_url},
            )
            await session.commit()
            logger.info("upsert ok: %s -> %s", args.reg, base_url)

            res = await session.execute(
                text(f"SELECT reg_number, base_url FROM {tbl} WHERE reg_number = :r"), {"r": args.reg}
            )
            row = res.first()
            print(f"OK: {row[0]} -> {row[1]}" if row else "ВНИМАНИЕ: запись не найдена после upsert")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
