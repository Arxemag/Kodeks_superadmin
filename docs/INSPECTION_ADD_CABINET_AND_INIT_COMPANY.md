# Инспекция модуля: add_cabinet_group и init_company (подготовка к Kafka)

Дата: 2026-02-24. Цель: выявить риски и неожиданности перед подключением Kafka для init_company.

---

## 1. Обзор цепочки

| Компонент | Назначение |
|-----------|------------|
| **scripts/add_cabinet_group.py** | CLI: добавить одну группу в один кабинет (ACL). Запуск вручную или по требованию. |
| **services/infoboards_service/admin_dirs.py** | Парсинг HTML формы ACL, сборка формы для POST /admin/dirs. Общий код для скрипта и InitCompanyService. |
| **services/infoboards_service/init_company_service.py** | Бизнес-логика init_company: sync mapping → группы → boards → ACL. Вызывается из Kafka worker. |
| **services/infoboards_service/init_company_worker.py** | Kafka consumer: чтение топика init_company, retry/DLQ, вызов InitCompanyService. |

---

## 2. Авторизация: два пути

- **add_cabinet_group.py** (при `--reg`): использует **AuthClient** → POST к Auth API `/api/expert/reg/{reg}` → получает готовые cookies.
- **InitCompanyService** (Kafka): использует **AuthService** → прямой логин в каталог (login.asp) с ADMIN_LOGIN/ADMIN_PASSWORD.

Оба дают админские cookies для каталога. При подключении Kafka важно: AuthService должен иметь те же учётные данные, что и Auth API (или Auth API должен быть единственной точкой входа для админа). Иначе сессия каталога может вести себя иначе (например, другой уровень доступа).

---

## 3. ACL: HTTP vs браузер (Playwright)

### 3.1 Скрипт add_cabinet_group.py

- **Без `--use-browser`**: GET /admin/dir, POST setup2, парсинг HTML. Если форма ACL рендерится через JS, в ответе нет полей `acl_infoboard_*` → скрипт предлагает `--use-browser`.
- **С `--use-browser`**: Playwright открывает страницу, жмёт «Настройки сервисов», получает HTML с формой, затем либо заполняет скрытые поля через `evaluate()` и жмёт «Сохранить», либо (при падении браузера) собирает форму и делает POST через httpx.
- Скрытые поля `acl_infoboard_*` заполняются через **JavaScript** (`locator.evaluate("(el, val) => { el.value = val; }", acl_val)`), т.к. `fill()` требует видимый элемент.

### 3.2 InitCompanyService._apply_acl (Kafka)

- Используется **только HTTP** (httpx): GET /admin/dirs, GET /admin/dir?n=X, POST setup2, парсинг, POST /admin/dirs.
- **Нет fallback на Playwright.** Если каталог отдаёт форму ACL только через JS (как у вас), ответ POST /admin/dir не содержит `acl_infoboard_*` → `full_form` неполный → POST /admin/dirs может вернуть **500** или сохранить не то.

**Рекомендация перед Kafka:**  
Либо (a) договориться с каталогом, чтобы ответ POST /admin/dir (setup2) уже содержал все поля формы в HTML, либо (b) добавить в InitCompanyService опциональный fallback на Playwright (флаг в конфиге, например `INIT_COMPANY_USE_BROWSER_FOR_ACL=true`) и вызывать тот же сценарий, что и в скрипте (открыть страницу, setup2, подставить ACL через evaluate, Submit). Вариант (b) усложняет worker (зависимость Playwright, headless в контейнере).

---

## 4. Исправления, внесённые при инспекции

- **init_company_service.py**: начальное значение `post_url` заменено с `.../admin/dir` на `.../admin/dirs`, чтобы сохранение ACL всегда уходило на правильный endpoint даже при любом порядке веток.

---

## 5. admin_dirs.py: парсинг и форма

- **parse_full_form_from_html**: HTMLParser по всем `<input>`, плюс regex-fallback. Учитывает `type=checkbox/radio` (только отмеченные).
- **parse_acl_from_html**: несколько regex для `acl_infoboard_*`, в т.ч. value в одинарных кавычках `'{"kw":[...]}'`.
- **build_admin_dirs_form**: объединяет `full_form` с `_BASE_PARAMS_FALLBACK`, добавляет `cmd=save`, для каждого кабинета из `acl_overrides` и `extra_board_ids` выставляет `acl_infoboard_*`, `infoboard_*=on` только при непустом ACL.
- **Риск**: если в HTML появятся новые обязательные поля или изменится имя кнопки/поля, парсер или форма могут стать неполными. Имеет смысл при изменении каталога перепроверять Fiddler и при необходимости расширять `_BASE_PARAMS_FALLBACK` или парсер.

---

## 6. Исключения и retry/DLQ (Kafka)

- **init_company_worker**: ValidationError / ParseError / ValueError → сразу в DLQ, без retry. AuthError с кодом REG_NOT_FOUND → DLQ. Остальные AuthError и NetworkError → retry с backoff, после исчерпания попыток → DLQ.
- **InitCompanyService** пробрасывает AuthError, NetworkError, ParseError — worker их различает по типам и коду.
- В логах и DLQ полезно сохранять `trace_id` (уже есть `set_trace_id(f"init_company-{topic}-{partition}-{offset}")`) для трассировки.

---

## 7. Конфиг и метрики

- **common/config.py**: KAFKA_INIT_COMPANY_TOPIC, KAFKA_INIT_COMPANY_DLQ_TOPIC, KAFKA_INIT_COMPANY_GROUP_ID, INIT_COMPANY_METRICS_PORT. USERS_RETRY_* используются и для init_company.
- **services/infoboards_service/metrics.py**: init_company_total, init_company_failed_total (по reason), init_company_retry_total, init_company_dlq_total, init_company_lag_seconds, init_company_processing_histogram, init_company_active_jobs.

При подключении Kafka проверьте, что в .env заданы KAFKA_BOOTSTRAP_SERVERS (или KAFKA_BROKER), топики и group_id, и что метрики (INIT_COMPANY_METRICS_PORT) не конфликтуют с другими сервисами.

---

## 8. Зависимости скрипта

- add_cabinet_group.py: httpx, AuthClient, CatalogClient, admin_dirs (parse_*, build_*, acl_to_json, add_group_to_acl_json), common.config, common.exceptions, common.http, common.logger. Для --use-browser: playwright.
- InitCompanyService: AuthService (логин в каталог), CatalogClient, admin_dirs, RegResolver/БД. Без Playwright.

---

## 9. Тесты

- **tests/test_init_company.py**: валидация InitCompanyDTO и DepartmentPayload. Нет тестов на _apply_acl, build_admin_dirs_form, парсинг ACL из HTML.
- Рекомендация: добавить юнит-тесты на `admin_dirs.parse_acl_from_html`, `parse_full_form_from_html`, `build_admin_dirs_form` (на примерах HTML-сниппетов и ожидаемого form_data), чтобы при смене формата каталога падали тесты, а не прод.

---

## 10. Чек-лист перед включением Kafka

- [ ] Убедиться, что POST /admin/dir (setup2) от каталога возвращает в HTML поля acl_infoboard_* (или включить INIT_COMPANY_USE_BROWSER_FOR_ACL и протестировать).
- [ ] Проверить, что AuthService и Auth API дают эквивалентные админские cookies для одного и того же reg.
- [ ] В .env заданы KAFKA_* и INIT_COMPANY_*.
- [ ] Метрики init_company (порт, Prometheus) не конфликтуют с другими воркерами.
- [ ] Прогон init_company_probe.py без Kafka на тестовом reg и проверка, что _apply_acl не падает (и по возможности что ACL в каталоге совпадает с ожиданием).
- [ ] При необходимости — юнит-тесты на admin_dirs (парсинг и сборка формы).

После выполнения пунктов выше подключение Kafka для init_company должно проходить без неожиданностей.
