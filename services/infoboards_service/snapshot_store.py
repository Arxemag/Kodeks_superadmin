"""
Хранилище снимков конфигурации кабинетов (таблица cabinet_snapshots).

Снимок — полный конфиг борда (id, author, title, searchString, hiddenOnStartPage, widgets)
на момент ПЕРВОГО уточнения по опроснику. Пишется один раз на пару (reg, board_id) и
дальше не перезаписывается: это эталон, от которого считается каждое следующее уточнение.
Благодаря ему повторное прохождение опросника умеет не только удалять виджеты, но и
возвращать удалённые ранее.

Инициализация: CabinetSnapshotStore(settings) создаётся в воркере; сессии БД берутся из
общего пула (common.db.get_db_session), отдельную сессию передавать не нужно.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from common.config import Settings
from common.db import get_db_session
from common.logger import get_logger


logger = get_logger("infoboards.snapshot_store")


@dataclass
class CabinetSnapshotStore:
    """Снимки конфигурации бордов в БД: save_if_absent (эталон пишется один раз) и get."""

    settings: Settings

    @property
    def _table(self) -> str:
        return self.settings.DB_TABLE_CABINET_SNAPSHOTS

    async def save_if_absent(self, reg: str, board_id: str, title: str, config: dict[str, Any]) -> bool:
        """
        Сохранить снимок, если для (reg, board_id) его ещё нет. Возвращает True, если запись создана.
        ON CONFLICT DO NOTHING: эталон никогда не перезаписывается, повторный вызов безопасен.
        """
        async with get_db_session() as session:
            res = await session.execute(
                text(f"""
                    INSERT INTO {self._table} (reg, board_id, title, config)
                    VALUES (:reg, :board_id, :title, CAST(:config AS JSONB))
                    ON CONFLICT (reg, board_id) DO NOTHING
                """),
                {
                    "reg": reg,
                    "board_id": board_id,
                    "title": title,
                    "config": json.dumps(config, ensure_ascii=False),
                },
            )
            await session.commit()
            created = bool(res.rowcount)
        if created:
            logger.info(
                "снимок кабинета сохранён reg=%r board=%r title=%r widgets=%s",
                reg, board_id, title, len(config.get("widgets") or []),
            )
        else:
            logger.debug("снимок кабинета уже есть, не трогаем reg=%r board=%r", reg, board_id)
        return created

    async def get(self, reg: str, board_id: str) -> dict[str, Any] | None:
        """Прочитать снимок конфига по (reg, board_id); None — если снимка нет."""
        async with get_db_session() as session:
            res = await session.execute(
                text(f"SELECT config FROM {self._table} WHERE reg = :reg AND board_id = :board_id"),
                {"reg": reg, "board_id": board_id},
            )
            row = res.scalar_one_or_none()
        if row is None:
            return None
        # asyncpg отдаёт JSONB уже разобранным, но при других драйверах может прийти строка.
        return json.loads(row) if isinstance(row, str) else dict(row)
