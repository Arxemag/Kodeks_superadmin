# Kodeks superadmin: Auth API + Kafka workers (users, init_company, reg_company)
# Один образ; сервис выбирается через CMD в docker-compose.
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

WORKDIR /app

# Зависимости
COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

# Код приложения
COPY common/ common/
COPY services/ services/
COPY scripts/ scripts/
COPY main.py main_users.py main_init_company.py main_reg_company.py main_unified_worker.py ./

# По умолчанию — Auth API. Порт задаётся через ENV PORT (по умолчанию 8000).
ENV PORT=8000
EXPOSE 8000
CMD ["python", "main.py"]
