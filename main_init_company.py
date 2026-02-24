"""
Точка входа для Kafka worker init_company.

Запуск: python main_init_company.py. Слушает топик init_company,
синхронизирует department_service_mapping, группы и ACL кабинетов.
"""
from __future__ import annotations

import asyncio

from services.infoboards_service.init_company_worker import run_worker


def main() -> None:
    """Запускает init_company worker."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
