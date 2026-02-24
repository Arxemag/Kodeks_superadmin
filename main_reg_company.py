"""
Точка входа для Kafka worker enable_reg_company / disable_reg_company.

Запуск: python main_reg_company.py
Слушает топики enable_reg_company и disable_reg_company, пока только логирует приём.
Реализацию (запрос к API) добавим позже.
"""
from __future__ import annotations

import asyncio

from services.reg_company_service.worker import run_worker


def main() -> None:
    """Запускает reg_company worker."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
