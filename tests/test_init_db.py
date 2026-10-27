"""Migration ledger and seed behaviour, without connecting to PostgreSQL."""
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import init_db


@pytest.fixture
def migration(tmp_path):
    path = tmp_path / "008_example.sql"
    path.write_text("CREATE TABLE IF NOT EXISTS example (id integer);\nCREATE INDEX IF NOT EXISTS ix_example ON example(id);\n", encoding="utf-8")
    return path


def connection(previous=None):
    calls = []
    async def execute(sql, params=None):
        calls.append((str(sql), params))
        return MagicMock(scalar_one_or_none=lambda: previous)
    driver = SimpleNamespace(execute=AsyncMock())
    conn = SimpleNamespace(execute=AsyncMock(side_effect=execute), get_raw_connection=AsyncMock(
        return_value=SimpleNamespace(driver_connection=driver)))
    return conn, driver, calls


@pytest.mark.asyncio
async def test_new_migration_script_and_checksum_are_recorded_in_order(migration):
    conn, driver, calls = connection()
    assert await init_db._apply_migrations(conn, [migration], "reg_services", "department_service_mapping") == [migration.name]
    assert "pg_advisory_xact_lock" in calls[0][0]
    assert "CREATE TABLE IF NOT EXISTS integration_schema_migrations" in calls[1][0]
    driver.execute.assert_awaited_once_with(migration.read_text())
    assert "INSERT INTO integration_schema_migrations" in calls[-1][0]
    assert calls[-1][1]["sha256"] == hashlib.sha256(migration.read_text().encode()).hexdigest()


@pytest.mark.asyncio
async def test_applied_migration_with_same_checksum_is_not_executed(migration):
    checksum = init_db._migration_sql(migration, "reg_services", "department_service_mapping")[1]
    conn, driver, calls = connection(previous=checksum)
    assert await init_db._apply_migrations(conn, [migration], "reg_services", "department_service_mapping") == []
    driver.execute.assert_not_awaited()
    assert not any("INSERT INTO integration_schema_migrations" in sql for sql, _ in calls)


@pytest.mark.asyncio
async def test_changed_checksum_is_error_not_silent_reapply(migration):
    conn, driver, _ = connection(previous="0" * 64)
    with pytest.raises(RuntimeError, match="Изменена применённая миграция"):
        await init_db._apply_migrations(conn, [migration], "reg_services", "department_service_mapping")
    driver.execute.assert_not_awaited()


def test_checksum_covers_effective_table_configuration(tmp_path):
    path = tmp_path / "001.sql"
    path.write_text("SELECT * FROM reg_services; SELECT * FROM department_service_mapping;", encoding="utf-8")
    default = init_db._migration_sql(path, "reg_services", "department_service_mapping")
    custom = init_db._migration_sql(path, "custom_regs", "custom_departments")
    assert default[1] != custom[1]
    assert "custom_regs" in custom[0] and "custom_departments" in custom[0]


def test_configurable_table_names_cannot_collide_with_default_index_names():
    migrations = Path(init_db.__file__).parent / "migrations"
    for name, original_index in (
        ("003_dsm_indexes.sql", "idx_dsm_reg"),
        ("004_dsm_unique_idx.sql", "idx_dsm_reg_dept"),
        ("006_department_catalog_groups.sql", "idx_dsm_reg_catalog_group"),
    ):
        default, _ = init_db._migration_sql(migrations / name, "reg_services", "department_service_mapping")
        custom, _ = init_db._migration_sql(migrations / name, "reg_services", "custom_departments")
        assert original_index in default
        assert original_index not in custom
        assert "custom_departments" in custom


def test_table_substitution_does_not_replace_inside_already_substituted_identifier(tmp_path):
    migration = tmp_path / "names.sql"
    migration.write_text("SELECT * FROM reg_services; SELECT * FROM department_service_mapping;", encoding="utf-8")
    sql, _ = init_db._migration_sql(migration, "department_service_mapping_archive", "actual_departments")
    assert "FROM department_service_mapping_archive" in sql
    assert "FROM actual_departments" in sql


@pytest.mark.asyncio
async def test_script_failure_does_not_record_migration(migration):
    conn, driver, calls = connection()
    driver.execute.side_effect = RuntimeError("DDL failed")
    with pytest.raises(RuntimeError, match="DDL failed"):
        await init_db._apply_migrations(conn, [migration], "reg_services", "department_service_mapping")
    assert not any("INSERT INTO integration_schema_migrations" in sql for sql, _ in calls)


@pytest.mark.asyncio
async def test_default_seed_never_deletes_existing_registrations():
    conn, _, calls = connection()
    await init_db._seed_regs(conn, "reg_services")
    assert len(calls) == len(init_db.DEFAULT_REGS)
    assert all("ON CONFLICT" in sql for sql, _ in calls)
    assert not any("TRUNCATE" in sql or "DELETE" in sql for sql, _ in calls)


@pytest.mark.asyncio
async def test_reset_seed_only_when_explicitly_requested():
    conn, _, calls = connection()
    await init_db._seed_regs(conn, "reg_services", reset=True)
    assert calls[0][0] == "TRUNCATE TABLE reg_services"


def test_reset_requires_seed_and_compatibility_flag_is_nondestructive():
    with pytest.raises(SystemExit) as error:
        init_db._parse_args(["--reset-reg-services"])
    assert error.value.code == 2
    args = init_db._parse_args(["--seed", "--keep-existing"])
    assert args.seed and args.keep_existing and not args.reset_reg_services
    args = init_db._parse_args(["--seed", "--reset-reg-services"])
    assert args.seed and args.reset_reg_services


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_run_wraps_ddl_and_ledger_in_transaction_and_disposes(monkeypatch, fail):
    events = []
    conn = object()
    @asynccontextmanager
    async def begin():
        events.append("begin")
        try:
            yield conn
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")
    engine = SimpleNamespace(begin=begin, dispose=AsyncMock())
    monkeypatch.setattr(init_db, "get_settings", lambda: SimpleNamespace(
        DB_URL="postgresql+asyncpg://test", DB_TABLE_REG_SERVICES="reg_services",
        DB_TABLE_DEPARTMENT_MAPPING="department_service_mapping"))
    monkeypatch.setattr(init_db, "create_async_engine", lambda *a, **kw: engine)
    migrate = AsyncMock(side_effect=RuntimeError("checksum") if fail else None)
    monkeypatch.setattr(init_db, "_apply_migrations", migrate)
    if fail:
        with pytest.raises(RuntimeError, match="checksum"):
            await init_db._run([])
        assert events == ["begin", "rollback"]
    else:
        await init_db._run([])
        assert events == ["begin", "commit"]
    engine.dispose.assert_awaited_once()
    assert migrate.call_args.args[0] is conn
