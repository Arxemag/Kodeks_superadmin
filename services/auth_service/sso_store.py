"""Одноразовые SSO-тикеты одного процесса API с TTL и ограничением памяти."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import secrets
from threading import Lock
import time
from types import MappingProxyType
from typing import Mapping
from typing import Callable


@dataclass(frozen=True)
class TicketData:
    reg: str
    uid: str
    path: str
    base_url: str
    expires_at: float
    _cookies: Mapping[str, str] | None = field(default=None, repr=False)

    @property
    def cookies(self) -> dict[str, str] | None:
        """Отдельная копия подтверждённой сессии; внутренний снимок неизменяем."""
        return dict(self._cookies) if self._cookies is not None else None


class TicketStoreFull(Exception):
    pass


class MemoryTicketStore:
    """Атомарный pop, включая конкуренцию потоков; хранит только хеши тикетов."""

    def __init__(self, *, ttl: int = 60, capacity: int = 10000,
                 clock: Callable[[], float] = time.monotonic):
        if ttl <= 0 or capacity <= 0:
            raise ValueError("Ticket TTL and capacity must be positive")
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self._tickets: OrderedDict[str, TicketData] = OrderedDict()
        self._lock = Lock()

    def _expire(self, now: float) -> None:
        while self._tickets:
            first = next(iter(self._tickets))
            if self._tickets[first].expires_at > now:
                break
            self._tickets.pop(first)

    def issue(self, *, reg: str, uid: str, path: str, base_url: str,
              cookies: dict[str, str] | None = None) -> str:
        with self._lock:
            now = self.clock()
            self._expire(now)
            if len(self._tickets) >= self.capacity:
                raise TicketStoreFull()
            while True:
                token = secrets.token_urlsafe(32)
                key = hashlib.sha256(token.encode()).hexdigest()
                if key not in self._tickets:
                    break
            snapshot = MappingProxyType(deepcopy(cookies)) if cookies is not None else None
            self._tickets[key] = TicketData(reg, uid, path, base_url, now + self.ttl, snapshot)
            return token

    def pop(self, token: str) -> TicketData | None:
        with self._lock:
            now = self.clock()
            self._expire(now)
            data = self._tickets.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            return data if data is not None and data.expires_at > now else None
