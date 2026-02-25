"""
Точка входа для Auth API.

Запуск: python main.py. Инициализирует uvicorn с приложением из services.auth_service.app (app).
Порт задаётся переменной окружения PORT (по умолчанию 8000).
Обработка запросов: FastAPI-приложение с эндпоинтами /health, /metrics, POST /api/expert/reg/{reg}, GET /api/infoboards/link.
"""
from __future__ import annotations

import uvicorn

from common.config import get_settings


def main() -> None:
    """Запускает ASGI-приложение Auth Service; порт берётся из ENV (PORT), по умолчанию 8000."""
    settings = get_settings()
    uvicorn.run("services.auth_service.app:app", host="0.0.0.0", port=settings.PORT, reload=False)


if __name__ == "__main__":
    main()

