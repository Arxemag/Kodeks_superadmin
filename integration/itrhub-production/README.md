# ITR Hub production: выдача свежей ссылки SSO при клике

Пакет добавляет `POST /api/expert/sso-link` в существующий Nest backend и
перехватывает открытие каталогов в `ModuleItem.tsx` и ссылках `DepartmentSidebar.tsx`. Сохранённые ссылки дерева и
существующий `getCabinetLink` остаются постоянными адресами; одноразовая ссылка
выдаётся при каждом клике. JWT и текущая HttpOnly `Auth` cookie проходят обычным
путём портала. Пароль браузер не передаёт.

Требуются настроенные `EXPERT_BASE_URL` (существующий адрес Auth API с `/api/expert`)
и `EXPERT_SSO_API_TOKEN` (тот же секрет, что `SSO_API_TOKEN` в Auth API).
При необходимости `EXPERT_SSO_HOST_ALIASES` задаётся как JSON, например
`{"platform.kodeks.expert":"350832"}`. Сам секрет не хранится в этом пакете.

Под пользователем с правами записи в исходники сначала выполнить план:

```sh
python3 integration/itrhub-production/install.py \
  --backend-root /path/to/existing/backend --frontend-root /path/to/existing/frontend
```

Проверить восемь целевых файлов, затем применить с новым закрытым каталогом копий:

```sh
python3 integration/itrhub-production/install.py \
  --backend-root /path/to/existing/backend --frontend-root /path/to/existing/frontend \
  --apply --backup-dir /private/backups/itrhub-sso-YYYYMMDD-HHMMSS
```

Любое несовпадение известных фрагментов останавливает установку до записи.
Копируются только четыре изменяемых исходника и manifest, полный проект не архивируется.
Идемпотентный повтор не меняет файлы. Новые модули, уже существующие с другим
содержимым, не перезаписываются. После применения обязательны реальные сборки
backend/frontend и проверка доступа под действующим JWT компании.

Пакет не изменяет nginx и не пересобирает/перезапускает сервисы. Обычные внешние
ссылки sidebar продолжают открываться штатным браузерным переходом.

Проверки пакета (Node 22 с поддержкой strip-types и Python с pytest):

```sh
node --experimental-strip-types --test integration/itrhub-production/tests/policy.test.mjs integration/itrhub-production/tests/frontend.test.mjs
python -m pytest integration/itrhub-production/tests/test_install.py -q
```
