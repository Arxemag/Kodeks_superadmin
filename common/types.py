"""
Общие типы для всего проекта.
"""
from __future__ import annotations

# Куки сессии каталога: имя -> значение; возвращаются из Auth API и передаются в запросы к каталогу
Cookies = dict[str, str]

