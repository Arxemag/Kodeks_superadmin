"""
Метрики Prometheus для users worker.

Инициализация: счётчики и гистограмма объявлены при импорте; HTTP-сервер метрик поднимается в run_worker на USERS_METRICS_PORT.
Инкремент: users_created_total/users_updated_total — в UserService; users_failed_total, users_retry_total, users_dlq_total — в worker;
kafka_lag_seconds и active_jobs — в цикле обработки; processing_latency_histogram — в _process_record.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

users_created_total = Counter("users_created_total", "Total created users")
users_updated_total = Counter("users_updated_total", "Total updated users")
users_failed_total = Counter("users_failed_total", "Total failed user jobs", ["reason"])
users_retry_total = Counter("users_retry_total", "Total retries for user jobs")
users_dlq_total = Counter("users_dlq_total", "Total messages sent to DLQ")
kafka_lag_seconds = Gauge("kafka_lag_seconds", "Estimated kafka lag in seconds")
processing_latency_histogram = Histogram(
    "processing_latency_histogram",
    "Message processing latency in seconds",
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 3, 5, 8, 13),
)
active_jobs = Gauge("users_active_jobs", "In-flight user jobs")
