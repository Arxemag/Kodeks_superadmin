"""
Создание таблиц и заливка справочника reg -> base_url.

Запуск:
  python -m scripts.init_db            # применить новые миграции
  python -m scripts.init_db --seed     # миграции + upsert стандартных reg, без очистки
  python -m scripts.init_db --seed --reset-reg-services  # явно очистить справочник перед seed

Требует .env с DB_URL или PG_* (postgresql+asyncpg).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
import re

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


_MIGRATION_LOCK_KEY = int.from_bytes(b"KD_MIGR1", "big", signed=True)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Создание таблиц и заливка reg -> base_url")
    parser.add_argument("--seed", action="store_true", help="Залить актуальные reg (см. DEFAULT_REGS)")
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Устаревший совместимый флаг: seed уже сохраняет существующие строки",
    )
    parser.add_argument(
        "--reset-reg-services", action="store_true",
        help="Явно очистить весь справочник reg_services перед --seed",
    )
    args = parser.parse_args(argv)
    if args.reset_reg_services and not args.seed:
        parser.error("--reset-reg-services требует --seed")
    return args


def _migration_sql(path: Path, tbl_reg: str, tbl_dsm: str) -> tuple[str, str]:
    sql = path.read_text(encoding="utf-8")
    # PostgreSQL index names are schema-wide, not table-scoped. The default
    # names can already belong to the default table when a custom table is
    # configured, so custom tables need stable, distinct index identifiers.
    if tbl_dsm != "department_service_mapping":
        suffix = hashlib.sha256(tbl_dsm.casefold().encode("utf-8")).hexdigest()[:12]
        indexes = {
            "idx_dsm_reg": f"idx_dsm_{suffix}_reg",
            "idx_dsm_reg_dept": f"idx_dsm_{suffix}_reg_dept",
            "idx_dsm_reg_catalog_group": f"idx_dsm_{suffix}_reg_catalog_group",
        }
        sql = re.sub(r"\b(?:idx_dsm_reg_catalog_group|idx_dsm_reg_dept|idx_dsm_reg)\b",
                     lambda match: indexes[match.group(0)], sql)
    names = {"reg_services": tbl_reg, "department_service_mapping": tbl_dsm}
    sql = re.sub(r"\b(?:reg_services|department_service_mapping)\b",
                 lambda match: names[match.group(0)], sql)
    return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()


async def _execute_script(conn, sql: str) -> None:
    """Execute an asyncpg script inside the current SQLAlchemy transaction.

    Normal prepared execute rejects multiple top-level SQL commands. Borrow the
    same driver connection and use asyncpg's no-arguments script execution so
    DO dollar-quoted blocks and multi-command files are not naively split.
    Do not close this borrowed raw connection: the enclosing transaction owns it.
    """
    raw = await conn.get_raw_connection()
    await raw.driver_connection.execute(sql)


async def _apply_migrations(conn, migrations: list[Path], tbl_reg: str, tbl_dsm: str) -> list[str]:
    """Caller owns one transaction containing DDL and ledger changes."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS integration_schema_migrations (
            migration_name text PRIMARY KEY,
            sha256 varchar(64) NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
    """))
    applied = []
    for migration in sorted(migrations):
        sql, checksum = _migration_sql(migration, tbl_reg, tbl_dsm)
        result = await conn.execute(
            text("SELECT sha256 FROM integration_schema_migrations WHERE migration_name = :name"),
            {"name": migration.name},
        )
        previous = result.scalar_one_or_none()
        if previous is not None:
            if previous != checksum:
                raise RuntimeError(
                    f"Изменена применённая миграция {migration.name} или имена целевых таблиц; "
                    "добавьте новую миграцию, не изменяйте историю"
                )
            print(f"  Пропускаю {migration.name}: уже применена")
            continue
        print(f"  Выполняю {migration.name}...")
        await _execute_script(conn, sql)
        await conn.execute(
            text("INSERT INTO integration_schema_migrations (migration_name, sha256) VALUES (:name, :sha256)"),
            {"name": migration.name, "sha256": checksum},
        )
        applied.append(migration.name)
    return applied


async def _seed_regs(conn, table_name: str, *, reset: bool = False) -> None:
    if reset:
        print(f"  Явная очистка {table_name} (--reset-reg-services)...")
        await conn.execute(text(f"TRUNCATE TABLE {table_name}"))
    for reg, base_url in DEFAULT_REGS:
        await conn.execute(
            text(f"""
                INSERT INTO {table_name} (reg_number, base_url)
                VALUES (:reg, :base_url)
                ON CONFLICT (reg_number) DO UPDATE SET base_url = EXCLUDED.base_url
            """),
            {"reg": reg, "base_url": base_url},
        )
    print(f"  Обновлено стандартных reg: {len(DEFAULT_REGS)}; остальные строки сохранены" if not reset
          else f"  Залито стандартных reg: {len(DEFAULT_REGS)}")


async def _run(argv=None) -> None:
    args = argv if isinstance(argv, argparse.Namespace) else _parse_args(argv)
    if args.keep_existing:
        print("--keep-existing устарел: seed по умолчанию выполняет upsert без очистки")

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

    try:
        print(f"Подключение к БД (таблицы: {tbl_reg!r}, {tbl_dsm!r})...")
        async with engine.begin() as conn:
            await _apply_migrations(conn, migrations, tbl_reg, tbl_dsm)
        if args.seed:
            async with engine.begin() as conn:
                await _seed_regs(conn, tbl_reg, reset=args.reset_reg_services)
    finally:
        await engine.dispose()
    print("Готово.")


def main(argv=None) -> None:
    # Parse --help/invalid destructive flags before opening an asyncio runtime.
    args = _parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
