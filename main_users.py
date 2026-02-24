"""
Точка входа для Kafka worker пользователей.

Запуск: python main_users.py. Инициализирует метрики, consumer/producer, RegResolver и входит в цикл
опроса топиков create-user, update-user, update-user-departments. Обработка сообщений — в run_worker;
завершение по SIGINT/SIGTERM с ожиданием завершения активных задач и остановкой consumer/producer.
"""
from __future__ import annotations

import asyncio

from services.users_service.worker import run_worker


def main() -> None:
    """Запускает асинхронный воркер run_worker в цикле событий."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
