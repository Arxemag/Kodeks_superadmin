"""
Метрики Prometheus для Auth API.

Инициализация: счётчики и гистограммы объявлены при импорте; инкремент/observe в service и app.
auth_requests_total — по режиму (admin/user); auth_errors_total — по коду в обработчике ServiceError;
auth_latency_ms — время всего login; catalog_latency_ms — время отдельных вызовов каталога; active_requests — gauge в middleware.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

auth_requests_total = Counter("auth_requests_total", "Total auth requests", ["mode"])
auth_errors_total = Counter("auth_errors_total", "Total auth errors", ["code"])
auth_latency_ms = Histogram("auth_latency_ms", "Auth latency (ms)", buckets=(50, 100, 200, 400, 800, 1200, 2000, 5000))
catalog_latency_ms = Histogram(
    "catalog_latency_ms",
    "Catalog call latency (ms)",
    ["op"],
    buckets=(20, 50, 100, 200, 400, 800, 1200, 2000, 5000),
)
active_requests = Gauge("active_requests", "Active requests")

