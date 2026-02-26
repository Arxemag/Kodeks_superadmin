# Документация по коду Kodeks superadmin

Документ описывает назначение модулей, функций и их взаимодействие. Предназначен для сдачи проекта и онбординга разработчиков.

---

## 1. Обзор архитектуры

### 1.1 Компоненты

- **Auth API** (FastAPI) — HTTP-сервис: логин в каталог по `reg`, выдача ссылок на инфоборды. Использует БД (`reg_services`), каталог (HTTP).
- **Единый Kafka worker** — один процесс с одной consumer group, подписка на все топики; по `record.topic` вызывается нужный обработчик (users, init_company, reg_company). Использует БД, Auth API (для куков), каталог (HTTP), Kafka.

### 1.2 Потоки данных

- **Логин:** клиент → Auth API `POST /api/expert/reg/{reg}` или `POST /api/expert/reg/{reg}?name={name}` (без name — куки админа, с name — куки пользователя) → БД (base_url по reg) → каталог (POST login) → ответ с cookies.
- **Инфоборды:** клиент → Auth API `GET /api/expert/infoboards/link?reg=...` → БД, AuthService.login → каталог (GraphQL) → ответ со ссылкой/списком.
- **Воркер (Kafka):** сообщение из топика → десериализация JSON → маршрутизация по топику → обработчик (UserService / InitCompanyService / reg_company) → при успехе commit offset; при ошибке — retry или DLQ.

### 1.3 Зависимости между модулями

- **common** — конфиг, БД, Kafka-фабрики, HTTP, логгер, исключения. Не зависят друг от друга по циклу; используются всеми сервисами.
- **Auth API** зависит от: common (config, db, http, logger, exceptions), auth_service, infoboards_service.
- **Воркер** зависит от: common (config, db, kafka, logger), users_service (service, auth_client, catalog_client, reg_resolver), infoboards_service (init_company), reg_company_service, unified_worker.

---

## 2. Точки входа

### 2.1 `main.py`

- **Функция `main()`:** читает порт из конфига (`get_settings().PORT`), запускает uvicorn с приложением `services.auth_service.app:app`, host `0.0.0.0`.
- **Использование:** запуск Auth API (локально или в контейнере).

### 2.2 `main_unified_worker.py`

- **Функция `main()`:** вызывает `asyncio.run(run_unified_worker())`.
- **Использование:** запуск единого Kafka-воркера (одна группа, все топики).

### 2.3 Отдельные воркеры (опционально)

- **main_users.py** — только топики create-user, update-user, update-user-departments.
- **main_init_company.py** — только топик init_company.
- **main_reg_company.py** — топики enable_reg_company, disable_reg_company.

В продакшене используется единый воркер (`main_unified_worker.py`).

### 2.4 Деплой и скрипты-пробники

- **На деплое скриптов нет** — в контуре только Kafka и воркер(ы). Запуск: `main_unified_worker.py` (один процесс на все топики) или отдельно `main_users.py`, `main_init_company.py`, `main_reg_company.py`.
- **Скрипты `scripts/*_probe.py`** — только для локальных/интеграционных тестов без Kafka. Они вызывают **те же сервисы** и **ту же нормализацию payload**, что и воркеры:
  - **init_company:** `unwrap_payload` → обёртка `data` → подстановка `reg` из альтернативных ключей → `InitCompanyDTO.model_validate` → `InitCompanyService.handle(dto)`. Воркер (`_handle_with_retries`) делает то же самое; единственное отличие — пробник поддерживает `--dry-run`.
  - **users:** `event_type_from_message` + `unwrap_payload` → по event_type вызывается `UserService.handle_create` / `handle_update` / `handle_update_departments`. Воркер использует `unwrap_payload(record.value)` и роутит по `record.topic`; в едином воркере в `record.topic` подставляется эффективный топик из `event_type`, чтобы при отправке в один топик Kafka маршрутизация совпадала с пробником.
- **Формат сообщения Kafka:** либо `{ "event_id", "event_type", "payload" }` (и при необходимости `event_type` внутри `payload`), либо плоский payload. `common.kafka`: `unwrap_payload`, `event_type_from_message` — единая точка разбора для воркеров и пробников.

---

## 3. Модуль `common`

### 3.1 `common/config.py`

- **Settings** — класс настроек (pydantic-settings). Все поля читаются из `.env`. Валидатор `_resolve_compat_values`: при пустом `KAFKA_BOOTSTRAP_SERVERS` подставляется `KAFKA_BROKER`; при пустом `DB_URL` собирается из `PG_*`.
- **get_settings()** — возвращает единственный экземпляр Settings (кэш). При ошибке валидации выбрасывает `ConfigError`.

### 3.2 `common/db.py`

- **_get_engine()** — создаёт глобальные `_engine` и `_sessionmaker` при первом вызове (один пул на процесс). Использует `get_settings().DB_URL`, `POOL_SIZE`, `POOL_TIMEOUT`.
- **get_db()** — асинхронный генератор, выдаёт одну сессию на запрос. Используется как FastAPI Depends в эндпоинтах Auth API.
- **get_db_session()** — контекстный менеджер сессии для воркеров и скриптов. Тот же пул, что и `get_db()`.
- **healthcheck_db()** — выполняет `SELECT 1`; при ошибке — `DatabaseError`. Вызывается из `GET /api/expert/health`.
- **shutdown_db()** — освобождает пул (`engine.dispose()`), обнуляет глобальные переменные. Вызывается при завершении Auth API (lifespan) и воркеров (RegResolver.shutdown).

### 3.3 `common/kafka.py`

- **_json_deserializer(raw: bytes)** — `json.loads(raw.decode("utf-8"))`. Используется как value_deserializer в consumer.
- **_json_serializer(value)** — `json.dumps(..., ensure_ascii=False).encode("utf-8")`. Используется как value_serializer в producer.
- **unwrap_payload(value)** — если value это dict с ключом `payload`, возвращает payload (при необходимости декодированный из JSON-строки); иначе возвращает value. Используется во всех воркерах (init_company, users, reg_company) и в скриптах-пробниках для единообразного извлечения тела сообщения.
- **event_type_from_message(value)** — извлекает `event_type` с верхнего уровня или из вложенного `payload`. Используется в unified_worker и в users_real_probe для маршрутизации по типу события.
- **create_consumer(*topics, group_id, settings=..., max_poll_records=..., ...)** — создаёт `AIOKafkaConsumer` с `bootstrap_servers` из settings, JSON-десериализацией, `enable_auto_commit=False`, `auto_offset_reset="earliest"`. Остальные параметры — из конфига или kwargs.
- **create_producer(settings=..., ...)** — создаёт `AIOKafkaProducer` с bootstrap из settings и JSON-сериализацией.

Взаимодействие: воркеры не создают consumer/producer напрямую, а вызывают эти фабрики — единая точка конфигурации Kafka.

### 3.4 `common/http.py`

- **CircuitBreaker** — при накоплении ошибок выше порога отклоняет запросы с `NetworkError` до истечения `open_time`. `guard()` — проверка перед запросом; `on_success()` / `on_error()` — обновление счётчика.
- **_get_transport()** — возвращает глобальный `httpx.AsyncHTTPTransport(retries=0)`.
- **shutdown_http()** — закрывает глобальный транспорт. Вызывается в lifespan Auth API.
- **_default_headers()** — заголовки для запросов к каталогу: X-Trace-Id, User-Agent с "kodeks", Accept, Accept-Language.
- **create_http_client()** — создаёт `httpx.AsyncClient` с таймаутом и лимитами из настроек, общим транспортом и _default_headers. Закрытие — ответственность вызывающего.
- **request_with_retry(client, method, url, data=..., params=..., headers=...)** — проверяет circuit breaker, выполняет запрос; при 5xx/таймауте/сетевой ошибке — один повтор с задержкой. При успехе сбрасывает breaker; при ошибке увеличивает счётчик (после порога цепь размыкается). Используется в AuthService при обращении к каталогу.

### 3.5 `common/logger.py`

- **trace_id_var** — ContextVar для идентификатора трассировки запроса/сообщения.
- **set_trace_id(value)** — устанавливает trace_id в текущем контексте (middleware Auth API, воркеры).
- **get_trace_id()** — возвращает текущий trace_id или генерирует новый и сохраняет.
- **JsonFormatter** — форматирует запись лога в одну строку JSON (ts, level, service, msg, trace_id, при необходимости exc).
- **TraceIdFilter** — добавляет атрибут trace_id в запись перед форматированием.
- **get_logger(name)** — возвращает логгер с именем name; при первом обращении настраивает уровень из `LOG_LEVEL`, stdout, JsonFormatter и TraceIdFilter.

### 3.6 `common/exceptions.py`

Иерархия исключений (все наследуют `ServiceError` с полями code, message, http_status):

- **ServiceError** — базовое (500).
- **ConfigError** — ошибка конфигурации (500).
- **DatabaseError** — ошибка БД (500).
- **NetworkError** — сеть, таймаут, 5xx (502). В воркерах — retry, затем DLQ.
- **AuthError** — авторизация (401). `REG_NOT_FOUND` в воркере — сразу в DLQ без retry.
- **ParseError** — разбор HTML/JSON (422). В воркере — в DLQ без retry.

В Auth API все `ServiceError` перехватываются в app.py и отдаются клиенту с code, message и http_status.

### 3.7 `common/types.py`

- Типы (например `Cookies`) для переиспользования в сервисах.

---

## 4. Сервис Auth API (`services/auth_service`)

### 4.1 `app.py`

- **lifespan(app)** — при старте вызывает `get_settings()`, логирует; при завершении — `shutdown_http()`, `shutdown_db()`.
- **create_app()** — создаёт FastAPI с lifespan, middleware, обработчиками ошибок и маршрутами.
  - **Middleware:** для каждого запроса увеличивает счётчик активных запросов, выставляет trace_id (из заголовка или новый), замеряет время, прокидывает X-Trace-Id и X-Elapsed-Ms в ответ.
  - **Обработчики:** ServiceError → JSON с code, message и http_status; RequestValidationError → 422; Exception → 500 INTERNAL_ERROR.
  - **GET /api/expert/health** — вызывает `healthcheck_db()`, возвращает `{"status": "ok"}`.
  - **GET /api/expert/metrics** — Prometheus-метрики (generate_latest).
  - **POST /api/expert/reg/{reg}** — опциональный query-параметр **`name`**: без `name` возвращаются куки админа, с `name` — куки указанного пользователя (пароль запрашивается через админа). Примеры: `POST /api/expert/reg/350832`, `POST /api/expert/reg/350832?name=user1`. Передаёт в `AuthService(db, settings).login(reg, name)` и возвращает cookies.
  - **GET /api/expert/infoboards/link** — query `reg`, опционально `title`. Передаёт в `InfoboardsService(db, settings).get_link_by_reg_and_title(reg, title)` и возвращает ссылку или список кабинетов.

Взаимодействие: app использует common.db (get_db, healthcheck_db, shutdown_db), common.config (get_settings), common.http (shutdown_http), common.logger, common.exceptions; делегирует логику в AuthService и InfoboardsService.

### 4.2 `service.py` (AuthService)

- **AuthService(db, settings)** — сервис логина в каталог.
- **login(reg, name=None)** — ограничение по `AUTH_HARD_TIMEOUT` (в DEBUG — AUTH_HARD_TIMEOUT_DEBUG), вызов `_login_flow(reg, name)`, метрики auth_latency_ms и auth_requests_total. При таймауте — NetworkError.
- **_login_flow(reg, name)** — получает base_url через `_get_base_url(reg)`, проверяет HTTPS; без name — `_admin_login(base_url)`; с name — `_fetch_user_password_as_admin(base_url, name)` и `_user_login(base_url, name, password)`.
- **_get_base_url(reg)** — SELECT base_url FROM reg_services WHERE reg_number = :reg; при отсутствии — AuthError REG_NOT_FOUND.
- **_admin_login(base_url)** — POST на users/login.asp с ADMIN_LOGIN/ADMIN_PASSWORD через request_with_retry; возвращает cookies.
- **_fetch_user_password_as_admin(base_url, name)** — получает админские куки, GET страницы пользователя по name, парсит HTML и извлекает value поля psw.
- **_user_login(base_url, name, password)** — POST на users/login.asp с name и извлечённым паролем; возвращает cookies пользователя.

Взаимодействие: использует db (reg_services), common.http (request_with_retry, создаёт свой AsyncClient для изоляции сессии), common.exceptions, common.logger, services.auth_service.metrics.

### 4.3 `schemas.py`, `metrics.py`

- **schemas** — Pydantic-модели для запросов/ответов API (LoginRequest, OkResponse, ErrorResponse).
- **metrics** — Prometheus-метрики (active_requests, auth_errors_total, auth_latency_ms, auth_requests_total, catalog_latency_ms).

---

## 5. Сервис Users (`services/users_service`)

### 5.1 `service.py` (UserService)

- **UserService(auth_client, catalog_client, reg_resolver)** — операции с пользователями каталога.
- **handle_create(payload: CreateUserDTO)** — resolve base_url по reg, получает админские куки (AuthClient), проверяет существование пользователя (get_user_page). Если пользователь есть — переходит в _update_from_state (идемпотентность); иначе формирует form_data и POST через catalog_client.post_user. Инкремент users_created_total при создании.
- **handle_update(payload: UpdateUserDTO)** — base_url, куки, загрузка текущего состояния (get_user_page, parse_user_state). При отсутствии пользователя — ParseError USER_NOT_FOUND. Слияние входящих полей с существующим и POST при наличии изменений через _update_from_state.
- **handle_update_departments(payload: UpdateUserDepartmentsDTO)** — разбор групп каталога (get_groups_page, parse_groups_catalog), создание отсутствующих групп (create_group), сопоставление title отделов с id групп, замена групп пользователя и проверка после сохранения.
- **_update_from_state(incoming, fields_set, existing, fallback_id, base_url, cookies)** — формирует form_data из слияния existing и incoming по fields_set; при отличии от текущего состояния — post_user.

Взаимодействие: RegResolver (base_url), AuthClient (куки), CatalogClient (get_user_page, post_user, get_groups_page, create_group), html_parser (parse_user_state, parse_groups_catalog), dto, metrics.

### 5.2 `reg_resolver.py` (RegResolver)

- **RegResolver** — без собственного пула БД; использует common.db.
- **startup()** — вызывает _get_engine() для инициализации пула.
- **shutdown()** — вызывает shutdown_db().
- **resolve_base_url(reg)** — async with get_db_session(), SELECT base_url FROM reg_services WHERE reg_number = :reg; при отсутствии — AuthError REG_NOT_FOUND.
- **with_session()** — контекстный менеджер, yield сессии из get_db_session(). Используется в init_company для передачи сессии в InitCompanyService.

### 5.3 `auth_client.py` (AuthClient)

- **AuthClient(http_client, auth_service_url)** — клиент к Auth API.
- **get_admin_cookies(reg)** — POST (при 405 — GET) на /api/expert/reg/{reg}, парсит JSON cookies. При 5xx — NetworkError, при 4xx — AuthError, при отсутствии cookies — AuthError AUTH_COOKIES_MISSING.

### 5.4 `catalog_client.py` (CatalogClient)

- **CatalogClient(http_client)** — запросы к каталогу по HTML/формам.
- **get_user_page(base_url, uid, cookies)** — GET /users/usr?id=uid; при 4xx (кроме 401/403) возвращает None.
- **post_user(base_url, form_data, cookies)** — POST /users/users, application/x-www-form-urlencoded.
- **get_groups_page(base_url, cookies)** — GET страницы групп.
- **create_group(base_url, title, cookies)** — POST создания группы (с fallback на /users/grp при 404/405).

Внутри используется _post_form; при 401/403 — AuthError, при сетевых ошибках — NetworkError.

### 5.5 `worker.py` (воркер users)

- **run_worker()** — запуск HTTP-сервера метрик (USERS_METRICS_PORT), создание consumer (create-user, update-user, update-user-departments) и producer через common.kafka, RegResolver().startup(), UserService(auth_client, catalog_client, reg_resolver). Цикл: getmany → для каждой записи create_task(_process_record(...)); при успехе — OffsetTracker.complete_and_build_commit и consumer.commit; при завершении — consumer.stop(), producer.stop(), resolver.shutdown().
- **_process_record(record, service, producer, consumer, tracker, semaphore, settings)** — вызов _handle_with_retries; при успехе — commit через tracker; в finally — метрики и semaphore.release().
- **_handle_with_retries(record, service, producer, settings)** — валидация payload (dict); по record.topic вызывается handle_create / handle_update / handle_update_departments. При ValidationError, ParseError, REG_NOT_FOUND — отправка в DLQ и return. При NetworkError/AuthError — retry с backoff (USERS_RETRY_ATTEMPTS); при исчерпании — DLQ. При неизвестном топике — DLQ.
- **_send_dlq(producer, dlq_topic, payload, error_code, error_message)** — отправка сообщения в DLQ с original_message, error_code, error_message, timestamp; инкремент users_dlq_total.
- **OffsetTracker** — учёт завершённых offset по партициям для последовательного commit при неупорядоченном завершении задач. register(tp, offset); complete_and_build_commit(tp, offset) возвращает карту для commit.
- **_backoff_with_jitter(attempt, base, max_delay)** — экспоненциальная задержка с jitter.

### 5.6 `dto.py`, `html_parser.py`, `metrics.py`

- **dto** — CreateUserDTO, UpdateUserDTO, UpdateUserDepartmentsDTO (Pydantic); redact_payload для логирования без секретов.
- **html_parser** — parse_user_state(html), parse_groups_catalog(html) — разбор HTML каталога.
- **metrics** — active_jobs, kafka_lag_seconds, processing_latency_histogram, users_dlq_total, users_failed_total, users_retry_total, users_created_total, users_updated_total.

---

## 6. Сервис Infoboards (`services/infoboards_service`)

### 6.1 `service.py` (InfoboardsService)

- **InfoboardsService(db, settings)** — получение ссылки на кабинет по reg и опционально title.
- **get_link_by_reg_and_title(reg, title)** — _get_base_url(reg), AuthService.login(reg), create_http_client(), POST GraphQL к каталогу (board.many), разбор JSON; при title — один кабинет с совпадающим title, иначе {"items": [...]}. При 4xx/5xx — NetworkError.

### 6.2 `init_company_service.py` (InitCompanyService)

- **InitCompanyService(db, settings, http_client, catalog_client)** — инициализация компании: маппинг, группы, ACL.
- **handle(dto: InitCompanyDTO, dry_run=False)** — _get_base_url(reg), _sync_mapping_table(reg, dto). При dry_run выходит. Иначе: auth login, _sync_groups (страница групп, создание недостающих), _get_boards (GraphQL), _build_acl_async (матрица board_id → group_ids из department_service_mapping и dep_id_to_modules), _apply_acl (GET /admin/dir?n=X, парсинг формы, POST /admin/dirs с новой матрицей).
- **_sync_mapping_table(reg, dto)** — SELECT существующих записей из department_service_mapping по reg; обновление или INSERT по входящим departments; архивация (mapping_status = 'archived') для отделов, отсутствующих во входящем payload.
- **_sync_groups(base_url, cookies, reg)** — get_groups_page, parse_groups_catalog; для активных service_group_name из маппинга создаёт отсутствующие группы через create_group, возвращает словарь title → group_id.
- **_get_boards(base_url, cookies)** — делегирует в cabinet_acl.get_boards (GraphQL).
- **_build_acl_async(reg, groups_dict, boards_dict, dep_id_to_modules)** — SELECT department_id, service_group_name из department_service_mapping по reg (active); для каждого отдела и модуля (board) строит acl_matrix: board_id → список group_id.
- **_apply_acl(base_url, cookies, acl_matrix, boards_dict)** — admin_dirs: GET /admin/dir?n=X (users), парсинг формы, build_admin_dirs_form с новой матрицей, POST /admin/dirs.

Взаимодействие: db (reg_services, department_service_mapping), AuthService, CatalogClient, admin_dirs, cabinet_acl, dto, html_parser (parse_groups_catalog).

### 6.3 `init_company_worker.py`

- **run_worker()** — метрики (INIT_COMPANY_METRICS_PORT), consumer (init_company), producer, RegResolver().startup(). Цикл getmany → _process_record (init_company_handle с retry/DLQ), commit при успехе.
- **_handle_with_retries(record, resolver, catalog_client, http_client, producer, settings)** — валидация payload и InitCompanyDTO; with_session → InitCompanyService(db, ...).handle(dto); при ошибках (ValidationError, AuthError REG_NOT_FOUND, NetworkError) — DLQ или retry с backoff.

### 6.4 `admin_dirs.py`, `cabinet_acl.py`, `dto.py`, `schemas.py`, `metrics.py`

- **admin_dirs** — parse_acl_from_html, parse_full_form_from_html, build_admin_dirs_form — работа с HTML-формой /admin/dirs.
- **cabinet_acl** — get_boards (GraphQL board.many), get_docs_n_from_dirs — получение кабинетов и параметров для формы.
- **dto** — InitCompanyDTO, DepartmentPayload (Pydantic).
- **schemas** — ответы API инфобордов (InfoboardLinkResponse, InfoboardsListResponse и т.д.).
- **metrics** — init_company_* (active_jobs, dlq_total, failed_total, lag_seconds, processing_histogram, retry_total, total).

---

## 7. Сервис Reg company (`services/reg_company_service`)

### 7.1 `worker.py`

- **run_worker()** — метрики (REG_COMPANY_METRICS_PORT), consumer (enable_reg_company, disable_reg_company). Цикл: getmany → для каждой записи _process_one(record, settings), затем commit(offset+1).
- **_process_one(record, settings)** — валидация payload (dict); по topic — EnableRegCompanyDTO или DisableRegCompanyDTO.model_validate(payload); логирование «принял, всё ок». При ValidationError или не dict — warning, без падения. Реализация бизнес-логики (запрос к API) не реализована.

### 7.2 `dto.py`

- **EnableRegCompanyDTO**, **DisableRegCompanyDTO** — Pydantic-модели для payload топиков.

---

## 8. Единый воркер (`services/unified_worker.py`)

- **_all_topics(settings)** — возвращает список из шести топиков: create-user, update-user, update-user-departments, init_company, enable_reg_company, disable_reg_company.
- **_record_with_effective_topic(record, effective_topic)** — строит виртуальный record с `topic=effective_topic`, чтобы users_handle и reg_company_process_one роутили по event_type при едином топике Kafka.
- **_dispatch_record(record, ...)** — эффективный топик: `_effective_topic(record)` (event_type из сообщения или record.topic). Для users и reg_company передаётся виртуальный record с этим топиком; вызываются users_handle, init_company_handle, reg_company_process_one. Неизвестный топик — только warning.
- **_process_one_record(record, ..., consumer, tracker, semaphore, settings)** — register partition/offset в tracker, вызов _dispatch_record; при успешном возврате — tracker.complete_and_build_commit и consumer.commit; при исключении — логирование; в finally — semaphore.release().
- **run_unified_worker()** — старт метрик (UNIFIED_WORKER_METRICS_PORT), один consumer (create_consumer(*topics, group_id=KAFKA_GROUP_ID)), один producer, RegResolver().startup(), UserService, цикл getmany → для каждой записи create_task(_process_one_record(...)); в конце — consumer.stop(), producer.stop(), resolver.shutdown().

Взаимодействие: common.kafka (create_consumer, create_producer), common.config, common.logger, users_service (OffsetTracker, _handle_with_retries, AuthClient, CatalogClient, RegResolver, UserService), infoboards_service.init_company_worker (_handle_with_retries), reg_company_service.worker (_process_one).

---

## 9. Скрипты (`scripts/`)

- **init_db.py** — создание таблиц по миграциям из scripts/migrations/ (reg_services, department_service_mapping, enum, индексы). Опция --seed: вставка тестового reg в reg_services (--reg, --base-url). Требует .env с DB_URL или PG_*.
- **init_company_probe.py** — прогон обработки init_company без Kafka: загрузка payload из файла или JSON, RegResolver(), InitCompanyService.handle(dto, dry_run=...). Требует БД (reg_services, department_service_mapping), доступ к каталогу при не dry_run.
- **users_real_probe.py** — прогон обработки users (create-user / update-user / update-user-departments) без Kafka: payload из файла, UserService с AuthClient и CatalogClient, вызов handle_create / handle_update / handle_update_departments. Требует поднятый Auth API и каталог.
- **add_cabinet_group.py** — добавление группы в кабинет (ACL): по reg (из reg_services) или --base-url, запрос к каталогу (admin/dirs). Опции --reg, --cabinet, --group; при JS-форме — --use-browser.

---

## 10. Сводка по взаимодействию

| Кто | С кем взаимодействует | Как |
|-----|------------------------|-----|
| Auth API (app) | common.db | get_db, healthcheck_db, shutdown_db |
| Auth API | AuthService, InfoboardsService | вызов login, get_link_by_reg_and_title |
| AuthService | БД | reg_services (base_url) |
| AuthService | Каталог | HTTP POST login, GET страницы пользователя |
| InfoboardsService | БД | reg_services |
| InfoboardsService | AuthService | login для cookies |
| InfoboardsService | Каталог | GraphQL board.many |
| InitCompanyService | БД | reg_services, department_service_mapping |
| InitCompanyService | AuthService, CatalogClient | login, группы, create_group |
| InitCompanyService | admin_dirs, cabinet_acl | формы, GraphQL boards |
| UserService | RegResolver | base_url по reg |
| UserService | AuthClient | админские cookies (через Auth API) |
| UserService | CatalogClient | get_user_page, post_user, get_groups_page, create_group |
| Воркеры | common.kafka | create_consumer, create_producer |
| Воркеры | common.db | через RegResolver (get_db_session, shutdown_db) |
| unified_worker | users_handle, init_company_handle, reg_company_process_one | по record.topic |

Документ можно дополнять при появлении новых модулей или изменении потоков. Для деплоя и переменных окружения см. README и раздел «БД» в нём.
