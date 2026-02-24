"""
Метрики Prometheus для init_company worker.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

init_company_total = Counter("init_company_total", "Total successfully processed init_company messages")
init_company_failed_total = Counter(
    "init_company_failed_total",
    "Total failed init_company jobs",
    ["reason"],
)
init_company_dlq_total = Counter("init_company_dlq_total", "Total init_company messages sent to DLQ")
init_company_retry_total = Counter("init_company_retry_total", "Total retries for init_company jobs")
init_company_active_jobs = Gauge("init_company_active_jobs", "In-flight init_company jobs")
init_company_lag_seconds = Gauge("init_company_lag_seconds", "Estimated init_company kafka lag")
init_company_processing_histogram = Histogram(
    "init_company_processing_seconds",
    "Init company message processing latency",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20),
)
