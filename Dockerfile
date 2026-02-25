# Kodeks superadmin: Auth API + Kafka workers (users, init_company, reg_company)
# Один образ; сервис выбирается через CMD в docker-compose.
FROM python:3.12-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY common/ common/
COPY services/ services/
COPY main.py main_users.py main_init_company.py main_reg_company.py main_unified_worker.py ./

# По умолчанию — Auth API. Порт задаётся через ENV PORT (по умолчанию 8000).
ENV PORT=8000
EXPOSE 8000
CMD ["python", "main.py"]
