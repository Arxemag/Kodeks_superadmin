# Kodeks superadmin

Auth API и Kafka-воркеры для каталога Кодекс: пользователи, init_company, enable/disable reg company.

## Требования

- Python 3.12+
- PostgreSQL (таблица `reg_services` и др.)
- Kafka (для воркеров)

## Установка и запуск локально

1. Создайте виртуальное окружение и установите зависимости:

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

2. Создайте `.env` на основе `.env.example` и задайте `DB_URL` (или `PG_*`), `KAFKA_BOOTSTRAP_SERVERS`, `ADMIN_LOGIN`, `ADMIN_PASSWORD`.

3. Запуск сервисов:

```bash
# Auth API (порт 8000)
python main.py

# Воркеры (в отдельных терминалах или через процесс-менеджер)
python main_users.py
python main_init_company.py
python main_reg_company.py
```

## Docker

Один образ, несколько сервисов — выбор через `command` в `docker-compose`.

**Сборка и запуск:**

```bash
docker compose build
docker compose up -d
```

**Сервисы в `docker-compose.yml`:**

| Сервис | Команда | Порт |
|--------|---------|------|
| auth-api | `python main.py` | 8000 |
| users-worker | `python main_users.py` | — |
| init-company-worker | `python main_init_company.py` | — |
| reg-company-worker | `python main_reg_company.py` | — |

Переменные окружения задаются через `env_file: .env`. Укажите в `.env`:

- `DB_URL` или `PG_HOST`, `PG_PORT`, `PG_USERNAME`, `PG_PASSWORD`, `PG_DATABASE`
- `KAFKA_BOOTSTRAP_SERVERS` (или `KAFKA_BROKER`)
- `ADMIN_LOGIN`, `ADMIN_PASSWORD`
- при необходимости топики и group id (см. `.env.example`)

**Важно:** В контейнере `127.0.0.1` — это сам контейнер, а не хост. Если Kafka (или БД) крутится на вашей машине, воркерам нужно подключаться по адресу хоста:

- **Windows / macOS:** задайте `KAFKA_BOOTSTRAP_SERVERS=host.docker.internal:9092` и для БД в `DB_URL` или `PG_HOST` используйте `host.docker.internal`.
- **Linux:** используйте IP хоста в сети Docker (например `172.17.0.1`) или запустите Kafka/PostgreSQL в контейнерах в том же `docker-compose` и укажите имя сервиса (например `kafka:9092`).

Иначе воркеры падают с `Connect call failed ('127.0.0.1', 9092)` и контейнеры постоянно перезапускаются.

Запуск только части сервисов:

```bash
docker compose up -d auth-api reg-company-worker
```

## Конфигурация

- **Формат БД:** если `DB_URL` пустой, он собирается из `PG_HOST`, `PG_PORT`, `PG_USERNAME`, `PG_PASSWORD`, `PG_DATABASE`.
- **Kafka:** если `KAFKA_BOOTSTRAP_SERVERS` пустой, подставляется `KAFKA_BROKER`.

Полный список переменных — в `.env.example`.

## Сервисы

**Auth API** (`main.py`)  
- `POST /api/expert/reg/{reg}` — админские cookies по reg  
- `GET /api/infoboards/link?reg=...&title=...` — ссылка на кабинет (или список кабинетов без `title`)  
- `GET /health`, `GET /metrics`

**Users worker** (`main_users.py`)  
- Топики: `create-user`, `update-user`, `update-user-departments`  
- Retry при временных ошибках, DLQ для невалидных и исчерпавших retry  
- Метрики на `USERS_METRICS_PORT` (9101)

**Init company worker** (`main_init_company.py`)  
- Топик: `init_company`  
- Синхронизация маппинга отделов, групп в каталоге, ACL кабинетов  
- Метрики на `INIT_COMPANY_METRICS_PORT` (9102)

**Reg company worker** (`main_reg_company.py`)  
- Топики: `enable_reg_company`, `disable_reg_company`  
- Пока только приём и лог; реализация (запрос к API) — далее  
- Метрики на `REG_COMPANY_METRICS_PORT` (9103)

## Скрипты

- `scripts/add_cabinet_group.py` — добавить группу в кабинет (ACL): `--reg`, `--cabinet`, `--group`; при JS-форме — `--use-browser`
- `scripts/init_company_probe.py` — прогон init_company без Kafka
- `scripts/init_db.py` — создание таблиц БД (и опционально seed)

## Тесты

```bash
pip install -r requirements.txt
pytest tests -v
```

## БД

Ожидается таблица `reg_services` (и при необходимости таблицы для маппинга отделов). Пример создания и миграции — в `scripts/init_db.py` и `scripts/migrations/`.
