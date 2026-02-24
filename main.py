"""
Точка входа для Auth API.

Запуск: python main.py. Инициализирует uvicorn с приложением из services.auth_service.app (app).
Обработка запросов: FastAPI-приложение с эндпоинтами /health, /metrics, POST /api/expert/reg/{reg}, GET /api/infoboards/link.
"""
from __future__ import annotations

import uvicorn


def main() -> None:
    """Запускает ASGI-приложение Auth Service на 0.0.0.0:8000 без автоперезагрузки."""
    uvicorn.run("services.auth_service.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()

