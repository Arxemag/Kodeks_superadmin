"""
Логирование в JSON и трассировка запросов.

Инициализация: при первом вызове get_logger(name) для имени без обработчиков
создаётся логгер с JsonFormatter и TraceIdFilter. trace_id хранится в ContextVar,
задаётся в middleware Auth API или в worker по offset сообщения.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from common.config import get_settings
from common.exceptions import ConfigError


# Идентификатор трассировки запроса/сообщения; потокобезопасно через ContextVar
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(value: str | None) -> None:
    """Устанавливает trace_id для текущего контекста (вызывается из middleware или worker)."""
    trace_id_var.set(value)


def get_trace_id() -> str:
    """Возвращает текущий trace_id или генерирует новый и сохраняет в контексте."""
    current = trace_id_var.get()
    if current:
        return current
    new_id = str(uuid.uuid4())
    trace_id_var.set(new_id)
    return new_id


class JsonFormatter(logging.Formatter):
    """Формат одной записи лога в одну строку JSON (ts, level, service, msg, trace_id, при необходимости exc)."""
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", None) or record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or get_trace_id(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TraceIdFilter(logging.Filter):
    """Добавляет в каждую запись атрибут trace_id перед форматированием."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


def get_logger(name: str) -> logging.Logger:
    """
    Возвращает логгер с именем name. При первом обращении для этого имени настраивает
    уровень из LOG_LEVEL, stdout, JsonFormatter и TraceIdFilter; при повторных — тот же логгер.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = "INFO"
    try:
        level = get_settings().LOG_LEVEL.upper()
    except ConfigError:
        level = "INFO"

    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceIdFilter())

    logger.addHandler(handler)
    logger.propagate = False
    return logger

