# Kodeks superadmin

Auth API и Kafka-воркеры для каталога Кодекс: пользователи, init_company, enable/disable reg company.

---

## Деплой на сервер (для DevOps)

**Что поднять:** два контейнера (или два процесса): **auth-api** и **worker**. Один Docker-образ, в `docker-compose` выбор через `command`.

### Перед первым запуском

1. **Создать `.env`** в корне репозитория (скопировать с `.env.example`). В прод не коммитить `.env`.

2. **Обязательно задать в `.env`:**
   - **БД:** `DB_URL` (один URL) **или** все из `PG_HOST`, `PG_PORT`, `PG_USERNAME`, `PG_PASSWORD`, `PG_DATABASE`.
   - **Kafka:** `KAFKA_BOOTSTRAP_SERVERS` (или `KAFKA_BROKER`), например `kafka:9092` или хост:порт брокера.
   - **Одна consumer group:** `KAFKA_GROUP_ID` (по умолчанию `superadmin-workers`).
   - **Учётные данные каталога:** `ADMIN_LOGIN`, `ADMIN_PASSWORD` (обязательные, без них приложение не стартует).

3. **Порты (что слушает приложение):**

   | Сервис   | Порт (по умолчанию) | Назначение                          |
   |----------|----------------------|-------------------------------------|
   | auth-api | 8000 (или `PORT`)    | HTTP API, health, metrics           |
   | worker   | 9100                 | Только Prometheus-метрики (HTTP)    |

   Для доступа снаружи обычно пробрасывают только порт API (8000). Порт 9100 — для Prometheus/мониторинга.

4. **Зависимости до старта:** PostgreSQL и Kafka должны быть доступны по адресам из `.env`. В БД должны быть созданы таблицы до первого запуска: `python scripts/init_db.py` (подробнее — раздел [БД](#бд) ниже: какие таблицы и колонки нужны). Иначе контейнеры падают при подключении (при `restart: unless-stopped` будут перезапускаться).

5. **Проверка после деплоя:**
   - Auth API: `curl http://<хост>:8000/api/expert/health` → `{"status":"ok"}`.
   - Метрики воркера: `curl http://<хост>:9100/metrics` (если порт проброшен).

6. **Логи:** всё пишется в stdout/stderr. Уровень — `LOG_LEVEL` в `.env` (по умолчанию `INFO`).

### Команды (Docker)

```bash
docker compose build
docker compose up -d
```

Поднятся оба сервиса: `auth-api` и `worker`. Только API без воркера: `docker compose up -d auth-api`. БД поднимается отдельно; в `.env` для Docker укажите хост БД: **имя контейнера** с PostgreSQL (если БД в Docker) или `host.docker.internal` (если БД на хосте), не `localhost`.

### Если БД и Kafka на хосте, а не в Docker

В контейнере `127.0.0.1` — это сам контейнер. Нужно указывать адрес хоста:

- **Windows / macOS:** в `.env` задать `KAFKA_BOOTSTRAP_SERVERS=host.docker.internal:9092`, в `DB_URL` или `PG_HOST` — `host.docker.internal`.
- **Linux:** IP хоста в сети Docker (например `172.17.0.1`) или вынести Kafka/PostgreSQL в тот же `docker-compose` и указывать имя сервиса (например `kafka:9092`, `postgres:5432`).

Иначе будут ошибки вида `Connect call failed ('127.0.0.1', 9092)` и постоянные перезапуски.

**Если при запросах к API (например `/api/expert/infoboards/link`) в логах появляется `ConnectionRefusedError` к БД:** в контейнере хост `localhost` указывает на сам контейнер. В `.env` замените в `DB_URL` хост на `host.docker.internal` (Windows/macOS), например:
`DB_URL=postgresql+asyncpg://user:password@host.docker.internal:5432/your_db`. Либо задайте `PG_HOST=host.docker.internal` и не заполняйте `DB_URL` (тогда URL соберётся из `PG_*`).

---

## Требования

- Python 3.12+ (для локальной разработки)
- PostgreSQL (таблица `reg_services` и др.)
- Kafka (для воркера)

## Установка и запуск локально

1. Виртуальное окружение и зависимости:

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

2. Файл `.env` на основе `.env.example`: задать `DB_URL` (или `PG_*`), `KAFKA_BOOTSTRAP_SERVERS`, `ADMIN_LOGIN`, `ADMIN_PASSWORD`.

3. Запуск (для разработки можно поднимать по отдельности):

```bash
# Auth API (порт 8000 или PORT из .env)
python main.py

# Единый воркер (все топики, одна группа) — как в продакшене
python main_unified_worker.py
```

Отдельные воркеры (если нужны): `python main_users.py`, `python main_init_company.py`, `python main_reg_company.py` — в этом случае в `.env` задать отдельные группы (`KAFKA_GROUP_ID`, `KAFKA_INIT_COMPANY_GROUP_ID`, `KAFKA_REG_COMPANY_GROUP_ID`).

## Docker (сводка)

Один образ, в `docker-compose` два сервиса: **auth-api** и **worker**.

| Сервис   | Команда                      | Порт (по умолчанию) |
|----------|------------------------------|----------------------|
| auth-api | `python main.py`             | 8000                 |
| worker   | `python main_unified_worker.py` | метрики 9100      |

Единый воркер: одна consumer group (`KAFKA_GROUP_ID`), подписка на все топики (create-user, update-user, init_company, enable_reg_company, disable_reg_company и др.), маршрутизация по `record.topic` внутри процесса.

Переменные окружения — из файла `.env` (см. раздел «Деплой на сервер» и `.env.example`).

## Конфигурация

- **Формат БД:** если `DB_URL` пустой, он собирается из `PG_HOST`, `PG_PORT`, `PG_USERNAME`, `PG_PASSWORD`, `PG_DATABASE`.
- **Kafka:** если `KAFKA_BOOTSTRAP_SERVERS` пустой, подставляется `KAFKA_BROKER`.

Полный список переменных — в `.env.example`.

## Сервисы

**Auth API** (`main.py`)  
- `POST /api/expert/reg/{reg}` — cookies каталога по reg. Опционально query-параметр `name`: без `name` — куки админа, с `name` — куки пользователя (пароль запрашивается через админа). Пример: `POST /api/expert/reg/350832` или `POST /api/expert/reg/350832?name=user1`  
- `GET /api/expert/infoboards/link?reg=...&title=...` — ссылка на кабинет (или список кабинетов без `title`)  
- `GET /api/expert/health`, `GET /api/expert/metrics`

**Единый Kafka worker** (`main_unified_worker.py`) — одна consumer group, все топики:
- Топики: `create-user`, `update-user`, `update-user-departments`, `init_company`, `enable_reg_company`, `disable_reg_company`  
- Внутри: маршрутизация по `record.topic`, те же обработчики (users, init_company, reg_company)  
- Метрики на `UNIFIED_WORKER_METRICS_PORT` (9100)

Отдельные воркеры (`main_users.py`, `main_init_company.py`, `main_reg_company.py`) по-прежнему в репозитории — можно запускать их вместо единого, с разными группами (KAFKA_GROUP_ID, KAFKA_INIT_COMPANY_GROUP_ID, KAFKA_REG_COMPANY_GROUP_ID).

## Скрипты

- `scripts/add_cabinet_group.py` — добавить группу в кабинет (ACL): `--reg`, `--cabinet`, `--group`; при JS-форме — `--use-browser`
- `scripts/init_company_probe.py` — прогон init_company без Kafka
- `scripts/init_db.py` — создание таблиц БД (и опционально seed)

## Тесты

```bash
pip install -r requirements.txt
pytest tests -v
```

**Проверка логики Kafka (чтобы не сломать обработку сообщений):**

1. **Юнит-тесты** — без реальной Kafka проверяются:
   - `tests/test_kafka_handling.py` — валидный payload → вызов сервиса; невалидный payload / ошибка валидации → отправка в DLQ; reg_company: валидный и невалидный payload без падения.
   - `tests/test_worker.py` — OffsetTracker и backoff.
   - `tests/test_users_service.py` — UserService (create/update) со стабами.

2. **Пробы без Kafka** — та же бизнес-логика, что в воркерах, но с JSON из файла:
   - `python scripts/users_real_probe.py --topic create-user --payload-file payload.json` (нужен поднятый Auth API и каталог).
   - `python scripts/init_company_probe.py --payload-file payload.json` (нужны БД, каталог).

3. **Дымовой прогон с реальной Kafka** — поднять `docker compose up -d`, отправить тестовое сообщение в топик (например, через kafkacat или скрипт с aiokafka), убедиться по логам воркера, что сообщение обработано и нет исключений.

## Документация по коду

Подробное описание модулей, функций и их взаимодействия — в **[docs/DOCUMENTATION.md](docs/DOCUMENTATION.md)**. Там описаны: архитектура и потоки данных, точки входа, модуль `common` (config, db, kafka, http, logger, exceptions), сервисы auth, users, infoboards, reg_company, единый воркер, скрипты и сводная таблица взаимодействий. Нужно для сдачи проекта и онбординга.

## БД

Требуется **PostgreSQL**. Схему создаёт `scripts/init_db.py` (читает SQL из `scripts/migrations/` по порядку). Имена таблиц задаются через ENV: `DB_TABLE_REG_SERVICES` (по умолчанию `reg_services`), `DB_TABLE_DEPARTMENT_MAPPING` (по умолчанию `department_service_mapping`) — для подстройки под разные схемы БД.

### Таблицы и колонки

**1. `reg_services`** (или значение `DB_TABLE_REG_SERVICES`) — соответствие рега и базового URL каталога (используют Auth API, воркеры).

| Колонка     | Тип   | Ограничения   | Описание                    |
|-------------|-------|---------------|-----------------------------|
| `reg_number`| `text`| PRIMARY KEY   | Код рега (например 350832)  |
| `base_url`  | `text`| NOT NULL      | URL каталога без завершающего `/` |

**2. `department_service_mapping`** (или значение `DB_TABLE_DEPARTMENT_MAPPING`) — маппинг отделов компании на группы в каталоге (использует init_company).

Перед таблицей создаётся enum: `mapping_status_enum` — значения `'active'`, `'archived'`.

| Колонка            | Тип                     | Ограничения        | Описание                          |
|--------------------|-------------------------|--------------------|-----------------------------------|
| `id`               | `bigint`                | PRIMARY KEY, serial| Суррогатный ключ                  |
| `department_id`    | `uuid`                  | NOT NULL           | ID отдела из входящего payload    |
| `service_group_name` | `varchar(255)`        | NOT NULL           | Название группы в каталоге       |
| `reg`              | `varchar(50)`           | NOT NULL           | Код рега                          |
| `client_id`        | `varchar(100)`          | NOT NULL           | Идентификатор клиента             |
| `client_name`      | `varchar(255)`          | NOT NULL           | Название компании                 |
| `mapping_status`   | `mapping_status_enum`   | NOT NULL, default `'active'` | `active` или `archived` |

Индексы: по `reg`; уникальный по паре `(reg, department_id)`.

### Создание схемы перед первым запуском

```bash
# из корня репозитория, с настроенным .env (DB_URL или PG_*)
python scripts/init_db.py
```

Опционально тестовый reg: `python scripts/init_db.py --seed --reg 350832 --base-url https://platform.kodeks.expert`.
