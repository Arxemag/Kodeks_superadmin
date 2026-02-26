# Топики Kafka и форматы payload

Единый воркер (`main_unified_worker.py`) подписан на все топики ниже. Сообщение может быть в обёртке `{ "event_id", "event_type", "payload" }` — тогда маршрутизация по `event_type` (или по `record.topic`).

---

## 1. Создание пользователя

- **topic:** `create-user`
- **payload:**

| Поле      | Тип   | Обязательное | Примечание        |
|-----------|--------|--------------|-------------------|
| reg       | string | да          | Рег. номер        |
| uid       | string | да          | Логин             |
| psw       | string | да          | Пароль            |
| mail      | string | нет         |                   |
| name      | string | нет         |                   |
| org       | string | нет         | Часто пустой      |
| pos       | string | нет         | Часто пустой      |
| telephon  | string | нет         |                   |
| end       | string | нет         | Часто пустой      |
| grp       | list[int] | нет      | Опционально       |

DTO: `CreateUserDTO` (services/users_service/dto.py).

---

## 2. Обновление пользователя

- **topic:** `update-user`
- **payload:** как в create-user, плюс:

| Поле | Тип   | Обязательное |
|------|--------|--------------|
| id   | string | нет         | Идентификатор пользователя для обновления |

DTO: `UpdateUserDTO` (наследует CreateUserDTO, добавляет `id`).

---

## 3. Включение рега

- **topic:** `enable_reg_company`
- **payload:**

| Поле        | Тип   | Обязательное |
|-------------|--------|--------------|
| reg         | string | да          |
| oldReg      | string | да          |
| companyName | string | да          |

DTO: `EnableRegCompanyDTO` (services/reg_company_service/dto.py). Пока только приём и лог; реализация API — позже.

---

## 4. Выключение рега

- **topic:** `disable_reg_company`
- **payload:**

| Поле        | Тип   | Обязательное |
|-------------|--------|--------------|
| reg         | string | да          |
| companyName | string | да          |

DTO: `DisableRegCompanyDTO`. Пока только приём и лог.

---

## 5. Обновление отделов у пользователя

- **topic:** `update-user-departments`
- **payload:**

| Поле        | Тип   | Обязательное |
|-------------|--------|--------------|
| reg         | string | да          |
| id          | string | да          |
| departments | array  | да, не пустой |

Элемент `departments`: `{ "id": string, "title": string }`.

DTO: `UpdateUserDepartmentsDTO`, `DepartmentDTO` (id, title).

---

## 6. Обновление отделов и кабинетов у компании (init_company / sync_departments)

Один и тот же обработчик для двух топиков.

- **topic:** `init_company` или `sync_departments`
- **payload:**

| Поле        | Тип   | Обязательное | Примечание |
|-------------|--------|--------------|------------|
| id          | number | да          | ID компании (client_id) |
| reg         | string | да          | Рег. номер |
| companyName | string | нет         | Для `sync_departments` часто не передаётся (по умолчанию "") |
| departments | array  | нет (по умолчанию []) | Список отделов |

Элемент `departments`: `{ "id": string (UUID), "title": string, "modules": [string, ...] }`.  
`title` — название группы в каталоге; `modules` — названия кабинетов (инфобордов).

DTO: `InitCompanyDTO`, `DepartmentPayload` (services/infoboards_service/dto.py).

---

## Конфиг (common/config.py)

- `KAFKA_CREATE_TOPIC` = `create-user`
- `KAFKA_UPDATE_TOPIC` = `update-user`
- `KAFKA_UPDATE_DEPARTMENTS_TOPIC` = `update-user-departments`
- `KAFKA_INIT_COMPANY_TOPIC` = `init_company`
- `KAFKA_SYNC_DEPARTMENTS_TOPIC` = `sync_departments`
- `KAFKA_ENABLE_REG_COMPANY_TOPIC` = `enable_reg_company`
- `KAFKA_DISABLE_REG_COMPANY_TOPIC` = `disable_reg_company`

---

## Проверка цепочки: JSON → разбор → маршрутизация

1. **Приём сообщения**  
   Consumer получает байты → `value_deserializer` в `common.kafka` делает `json.loads(raw.decode("utf-8"))` → в обработчик попадает уже dict (или list).

2. **Разбор тела (unwrap)**  
   Во всех воркерах используется `unwrap_payload(record.value)` из `common.kafka`:
   - если в сообщении есть ключ **`payload`** — в обработку идёт именно он (если payload был строкой JSON — он декодируется);
   - иначе в обработку идёт всё тело сообщения (плоский формат).

3. **Маршрутизация в едином воркере**  
   - Эффективный топик: `event_type_from_message(record.value)` из `common.kafka` (сначала верхний уровень, при отсутствии — внутри `payload`).
   - Если `event_type` нет — используется `record.topic`.
   - Для users и reg_company в обработчик передаётся виртуальный record с `topic = effective_topic`, чтобы при одном топике Kafka маршрут совпадал с `event_type`.

4. **init_company / sync_departments**  
   После `unwrap_payload` применяется: обёртка **`data`** (если есть и нет `reg` на верхнем уровне); подстановка **`reg`** из ключей `registration`, `reg_number`, `regNumber`, `companyReg`. Та же логика в воркере и в скрипте `init_company_probe.py`.

5. **Валидация**  
   Из полученного payload вызывается `DTO.model_validate(payload)` для соответствующего топика (CreateUserDTO, UpdateUserDTO, InitCompanyDTO, EnableRegCompanyDTO, DisableRegCompanyDTO, UpdateUserDepartmentsDTO).
