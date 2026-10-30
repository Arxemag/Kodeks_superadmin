"""
Точка входа для Auth API.

Запуск: python main.py. Инициализирует uvicorn с приложением из services.auth_service.app (app).
Порт задаётся переменной окружения PORT (по умолчанию 8000).
Обработка запросов: FastAPI-приложение с эндпоинтами /api/expert/health, /api/expert/metrics, POST /api/expert/reg/{reg} (опционально ?name=), GET /api/expert/infoboards/link.
"""
from __future__ import annotations

from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from common.config import get_settings


def main() -> None:
    """Запускает ASGI-приложение Auth Service; порт берётся из ENV (PORT), по умолчанию 8000."""
    settings = get_settings()
    log_config = deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["sso_query"] = {
        "()": "common.sso_logging.SSOAccessLogFilter",
    }
    log_config["handlers"]["access"].setdefault("filters", []).append("sso_query")
    uvicorn.run("services.auth_service.app:app", host="0.0.0.0", port=settings.PORT,
                reload=False, log_config=log_config)


if __name__ == "__main__":
    main()

