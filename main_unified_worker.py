"""
Единый Kafka worker: одна consumer group, все топики (users, init_company, reg_company).

Запуск: python main_unified_worker.py
Требует в .env: KAFKA_GROUP_ID (одна группа), KAFKA_BOOTSTRAP_SERVERS и остальные настройки Kafka/БД.
"""
from __future__ import annotations

import asyncio

from services.unified_worker import run_unified_worker


def main() -> None:
    asyncio.run(run_unified_worker())


if __name__ == "__main__":
    main()
