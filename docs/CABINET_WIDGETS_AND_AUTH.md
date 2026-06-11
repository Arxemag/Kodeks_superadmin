# Кабинеты (инфоборды): виджеты, их создание и аутентификация

Справочник для **сервиса создания кабинетов/виджетов**. Описывает: как авторизоваться в каталоге,
как устроена правка/создание виджетов через GraphQL `editConfig`, и все типы виджетов с полями.

Источники правды: живая GraphQL-схема каталога (интроспекция) и реальные запросы из Fiddler.

---

## 1. Аутентификация (принципы)

Все запросы к каталогу идут с **session-cookies**, которые выдаёт логин. Хост каталога у каждой
компании свой и определяется по `reg`.

### 1.1 Определение хоста по reg
`base_url` каталога берётся из таблицы **`reg_services`**: `reg_number → base_url`.
```sql
SELECT base_url FROM reg_services WHERE reg_number = :reg;
-- напр. 465302 -> https://cabinet-03.kodeks.expert/
```
Требование: `base_url` обязан быть **`https://`**.

### 1.2 Логин администратора → cookies
Каталог на классическом ASP, логин — обычная форма:
```
POST {base_url}/users/login.asp
Content-Type: application/x-www-form-urlencoded
Headers: Origin: {base_url} ; Referer: {base_url}/
Body (form): user={ADMIN_LOGIN} & pass={ADMIN_PASSWORD} & path=/admin
follow_redirects: true
```
- Успех (2xx/3xx) → в ответе `Set-Cookie` сессии. Эти cookies (как словарь `name→value`)
  передаются дальше во **все** запросы (GraphQL, формы).
- `ADMIN_LOGIN`/`ADMIN_PASSWORD` — учётка администратора каталога (из `.env`).
- Под админом доступны и чтение, и `editConfig` (правка виджетов).

### 1.3 Логин под конкретным пользователем (опционально, для правки виджетов не нужен)
1. Залогиниться админом (см. 1.2).
2. `GET {base_url}/users/usr?id={username}` — страница пользователя.
3. Из HTML взять `value` поля `<input name="psw" ...>` (пароль пользователя).
4. `POST {base_url}/users/login.asp` с `user={username}&pass={извлечённый}&path=/` → cookies пользователя.

### 1.4 Надёжность
- Жёсткий таймаут на логин; при 5xx/таймауте — один повтор; circuit breaker (см. `common/http.py`).
- 401/403 от каталога → ошибка авторизации; 5xx → сетевая ошибка (ретрай).

---

## 2. GraphQL: чтение и запись виджетов

Единый эндпоинт каталога:
```
POST {base_url}/infoboard/graphql?context=docs
Headers: Content-Type: application/json ; Accept: application/json ;
         Origin: {base_url} ; Referer: {base_url}/docs/?frame=left
Cookies: <сессия из логина>
Body: { "query": "<...>", "variables": { ... } }
```

### 2.1 Чтение конфигурации борда — `QueryBoardOne`
```graphql
query QueryBoardOne($id: String!) {
  board { one(id: $id) {
    id author title
    searchString { header palette { colorWidget colorText colorIcon colorInput colorButton colorButtonText } }
    widgets { __typename ... }   # см. фрагменты по типам в разделе 4
  } }
}
```
`variables: { "id": "P004H" }`. Возвращает текущие виджеты борда.

### 2.2 Запись — мутация `editConfig` (создание / удаление / правка)
**Главный принцип: `editConfig` — это ПОЛНАЯ ЗАМЕНА набора виджетов борда.**
```graphql
mutation EditConfigWidgets($ConfigInput: ConfigInput) {
  editConfig(input: $ConfigInput) { id title }
}
```
`variables`:
```json
{
  "ConfigInput": {
    "id": "P004H",
    "author": "kodeks",
    "title": "Заголовок кабинета",
    "searchString": { "header": "", "palette": { "colorWidget": "#D3223C", "colorText": "#FFFFFF",
        "colorIcon": "#FFFFFF", "colorInput": "#FFFFFF", "colorButton": "#EDEDED", "colorButtonText": "#333333" } },
    "widgets": "[ {<виджет1>}, {<виджет2>}, ... ]"
  }
}
```
⚠️ **`widgets` — это СТРОКА** (JSON-массив виджетов, сериализованный в строку), а не вложенный массив.

Следствия:
| Операция | Что отправить в `widgets` |
|---|---|
| **Создать** виджет | весь текущий массив **плюс** новый объект виджета |
| **Удалить** виджет | весь массив **без** этого объекта |
| **Удалить все** | `"[]"` |
| **Изменить** виджет | массив, где у нужного объекта поправлены поля |

Поэтому сервис создания обычно: 1) читает борд (`QueryBoardOne`), 2) добавляет/меняет объекты в списке,
3) шлёт `editConfig` со всем списком (id/author/title/searchString берёт из прочитанного борда).

### 2.3 Новый виджет
- У нового виджета `widgetUuid` = **пустая строка `""`** — сервер присвоит uuid сам.
- У существующего виджета `widgetUuid` сохраняется (round-trip), иначе создастся дубль.

### 2.4 Создание нового кабинета «с нуля»
Правка виджетов (`editConfig`) работает по **существующему** борду (нужен `id`). Создание пустого
борда отдельной мутацией в капче **не зафиксировано**. Зацепка: в одном из запросов `editConfig`
использовался `id: "virtual"` (черновик/новый борд) — вероятно, через него создаётся новый кабинет,
но это нужно подтвердить отдельной капчей «создание кабинета».

---

## 3. Общие поля любого виджета (вход в `widgets`)

| Поле | Тип | Назначение |
|---|---|---|
| `widgetUuid` | string | `""` для нового; uuid для существующего |
| `position` | string | колонка: `"left"` / `"right"` |
| `header` | string | заголовок виджета (по нему идёт матчинг в уточнении кабинета) |
| `description` | string | подпись/описание (часто `""`) |
| `height` | number/null | высота (часто `null`) |
| `isSpecial` | bool | системный блок (true) или контентный (false) |
| `palette` | object | цвета (набор зависит от типа) |

Вычисляемые поля (`documents`, `getEvents`, `getOffers`, `countDocs`, `widgetData` и т.п.) на вход
**НЕ отправляются** — они только на чтение.

---

## 4. Типы виджетов (union `Widget`, 11 шт.) и поля для создания

Ниже — поля каждого типа, которые принимаются во входном `widgets` (формат `editConfig`).
В объект виджета **не** кладётся `__typename` — тип определяется по набору полей.

### 4.1 ListFromDocWidget — список из документа-папки
Поля: `widgetUuid, position, isSpecial, header, description, height, counter, limit, list{ limit, doc, holder, form, sort }, palette{ colorWidget, colorText, colorLink }`
```json
{ "widgetUuid": "", "position": "left", "isSpecial": false, "header": "Трест",
  "counter": 3, "description": "",
  "list": { "doc": 816800052, "form": 0, "holder": 0, "limit": 20, "sort": "" } }
```

### 4.2 LinksWidget — блок ссылок (в т.ч. навигация между кабинетами)
Поля: `widgetUuid, position, isSpecial, header, description, height, isTitleLink, titleLink{ href, about }, isButtonCreateForm, links[ { href, title, about, notForChange } ], palette{ colorWidget, colorText, colorLink }`
```json
{ "widgetUuid": "", "position": "left", "isSpecial": false, "header": "Производство мясо",
  "description": "", "isTitleLink": true,
  "titleLink": { "href": "kodeks://link/d?nd=606025000&infoboard=P000C", "about": "" },
  "isButtonCreateForm": false,
  "links": [ { "href": "https://.../docs/?nd=606025000&infoboard=P000A",
               "title": "Заведующий лабораторией", "about": "", "notForChange": true } ] }
```
> Навигация «родитель → под-кабинет» делается через `titleLink.href` вида
> `kodeks://link/d?...&infoboard=PXXXX` (id целевого кабинета).

### 4.3 PluginDocListWidget — список документов из сервиса (умные подборки)
Поля: `widgetUuid, position, isSpecial, header, description, height, counter, limit, listId{ limit, serviceId, listId }, palette{ colorWidget, colorText, colorLink }`
```json
{ "widgetUuid": "", "position": "left", "isSpecial": false,
  "header": "Законодательные требования СМК", "description": "", "counter": 3, "limit": 3,
  "listId": { "limit": null, "serviceId": "circulation", "listId": "infoboards/c8f111b8-6ff0-46b8-84d7-a011518efd04" } }
```

### 4.4 ListFromDocWidget vs DocListWidget
**DocListWidget** — простой список документов: `widgetUuid, position, isSpecial, header, description, height, counter, limit, palette{ colorWidget, colorText, colorLink }`.

### 4.5 ClassifierWidget — виджет классификатора
Поля: `widgetUuid, isSpecial, position, header, description, height, typeClassifier{ typeName, id, attrTab }, palette{ colorWidget, colorText, colorLink }`

### 4.6 DocAreaWidget — документы по области/условиям
Поля: `widgetUuid, position, isSpecial, header, description, height, counter, area{ limit, area, conditions[ { attr, values, mode } ] }, palette{ colorWidget, colorText, colorLink }`

### 4.7 ControlWidget — контроль (КОНД)
Поля: `widgetUuid, isSpecial, position, header, description, isShowDiagram, isShowLink, palette{ colorWidget, colorCells, colorText, colorLink, colorPie{ colorTotal, colorCheck } }`
```json
{ "widgetUuid": "", "isSpecial": true, "position": "right",
  "header": "Документы на контроле", "description": "", "isShowDiagram": true, "isShowLink": true }
```

### 4.8 KndCountersWidget — счётчики КНД (разработка/обсуждение/утверждение/публикация)
Поля: `widgetUuid, isSpecial, position, header, description, height, isDevelop, isDiscuss, isApprove, isPublish, isShowDiagram, isButtonNewProject, palette{ colorWidget, colorText, colorLink, colorCells, colorButton, colorPie{ colorLogo, colorDevelop, colorDiscuss, colorApprove, colorAprrove, colorPublish, colorTotal } }`

### 4.9 OndWidget — обсуждения/экспертиза НД
Поля: `widgetUuid, isSpecial, position, header, height, description, isShowDiagram, isCreatedByMe, isForMyReview, isForMyExpertise, isExpertiseRequired, isExpertiseCarriedOut, isButtonCreateDiscussion, isOndWidget, palette{ colorCells, colorWidget, colorText, colorButton, colorPie{ colorCreatedByMe, colorForMyReview, colorForMyExpertise, colorExpertiseRequired, colorExpertiseCarriedOut, colorTotal, colorLogo } }`

### 4.10 OffersWidget — предложения
Поля: `widgetUuid, position, isSpecial, header, description, height, isALL, isCREATED, isONTIME, isINDISCUSSION, isDECLINED, isShowDiagram, isButtonCreateOffer, isOffersWidget, palette{ colorCells, colorWidget, colorText, colorButton, colorPie{ colorALL, colorCREATED, colorONTIME, colorINDISCUSSION, colorDECLINED, colorLogo } }`

### 4.11 EventsWidget — события/проекты
Поля: `widgetUuid, isSpecial, position, header, height, description, isShowDiagram, isALL, isAPPROVED, isCLOSEDWITHSTAGEVIOLATIONS, isCLOSEDOVERDUE, isVIOLATION, isCOMPLETED, isOVERDUE, isCREATED, isPLANNED, isONTIME, isButtonNewProject, isEventsWidget, isCreated, isPlanned, isOnTime, isOverdue, isCompleted, isViolation, isClosedOverdue, isClosedWithStageViolations, isTotal, palette{ colorWidget, colorText, colorButton }`

> Точный актуальный набор полей можно в любой момент перегенерировать из живой схемы:
> `python scripts/gen_widget_query.py --base-url https://<host>/ --insecure` (он же чистит вычисляемые поля).

---

## 5. Полный пример: создать виджет на борде

1. **Логин** (раздел 1.2) → cookies.
2. **Прочитать борд** `P004H` (раздел 2.1) → получить `author`, `title`, `searchString`, текущий список `widgets`.
3. **Собрать новый список**: к прочитанным виджетам (с их `widgetUuid`) добавить новый объект с `widgetUuid:""`.
4. **Отправить** `editConfig`:
```json
{
  "ConfigInput": {
    "id": "P004H", "author": "kodeks", "title": "Эколог Мои внешние нормативные документы",
    "searchString": { "header": "", "palette": { "colorWidget": "#D3223C", "colorText": "#FFFFFF",
        "colorIcon": "#FFFFFF", "colorInput": "#FFFFFF", "colorButton": "#EDEDED", "colorButtonText": "#333333" } },
    "widgets": "[{\"widgetUuid\":\"019e...\",\"position\":\"left\",\"isSpecial\":false,\"header\":\"Старый\",\"description\":\"\"},{\"widgetUuid\":\"\",\"position\":\"right\",\"isSpecial\":false,\"header\":\"Новый блок\",\"description\":\"\",\"isTitleLink\":false,\"isButtonCreateForm\":false,\"links\":[]}]"
  }
}
```
5. Ответ `editConfig { id title }` — успех. Перечитать борд для проверки.

---

## 6. Реализация в этом репозитории (готовые кирпичи)

- `services/infoboards_service/cabinet_edit.py`:
  - `parse_infoboard_id(link)` — id борда из ссылки;
  - `fetch_board(...)` — чтение (`QueryBoardOne`);
  - `widget_to_input(widget)` — срез вычисляемых полей → объект для `widgets`;
  - `set_widgets(...)` — отправка `editConfig` (сериализует `widgets` в строку);
  - `correct_cabinet(...)` — уточнение по опроснику (удаление невыбранных).
- Аутентификация: `services/auth_service/service.py` (`AuthService.login`), `reg_services` через `RegResolver`.
- `scripts/cabinet_edit_probe.py` — ручной прогон (`--list`, `--list-boards`, `--correct`, `--self-test`, `--dry-run`).
- `scripts/gen_widget_query.py` — перегенерация запроса чтения из живой схемы.

Сервис создания виджетов/кабинетов может переиспользовать `fetch_board` + `set_widgets` (логика
«прочитал → добавил объект → отправил весь список»), а для нового борда — уточнить мутацию создания
(см. 2.4, `id:"virtual"`).
