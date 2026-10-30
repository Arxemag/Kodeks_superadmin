"""Маскирование одноразового SSO-тикета в access-логе uvicorn."""
from __future__ import annotations

import logging


class SSOAccessLogFilter(logging.Filter):
    """Изменяет только аргумент пути LogRecord, не ASGI-запрос или его scope."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3 or not isinstance(args[2], str):
            return True
        path, separator, _ = args[2].partition("?")
        if separator and path in {"/api/expert/sso", "/api/expert/sso/"}:
            record.args = (*args[:2], path + "?[REDACTED]", *args[3:])
        return True
