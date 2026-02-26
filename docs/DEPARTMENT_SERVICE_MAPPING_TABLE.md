# Таблица department_service_mapping

Таблица заполняется из сообщений **init_company** / **sync_departments** в методе `InitCompanyService._sync_mapping_table`.

## Схема (миграции 001, 002)

| Колонка             | Тип                    | Описание |
|---------------------|------------------------|----------|
| id                  | BIGSERIAL PRIMARY KEY  | Автоинкремент |
| department_id       | UUID NOT NULL          | ID отдела из payload `departments[].id` |
| service_group_name  | VARCHAR(255) NOT NULL  | Название группы в каталоге = `departments[].title` |
| reg                 | VARCHAR(50) NOT NULL   | Рег. номер = `dto.reg` |
| client_id           | VARCHAR(100) NOT NULL  | **ID компании (клиента)** = `dto.id` из payload |
| client_name         | VARCHAR(255) NOT NULL  | Название компании = `dto.companyName` |
| mapping_status      | mapping_status_enum    | `'active'` или `'archived'` |

Ограничение: уникальный индекс по `(reg, department_id)` — для одного рега один и тот же `department_id` не повторяется.

## Как заполняется из Kafka

| Колонка в БД   | Откуда в payload |
|----------------|-------------------|
| id             | не из payload (BIGSERIAL) |
| department_id  | `departments[].id` (UUID) |
| service_group_name | `departments[].title` |
| reg            | `reg` |
| client_id      | `id` (ID компании/клиента) |
| client_name    | `companyName` |
| mapping_status | `'active'` при insert/update; `'archived'` для отделов, которые убрали из нового payload по этому reg |

## Пример

Payload:

```json
{
  "id": 67,
  "reg": "123456",
  "companyName": "ООО Ромашка",
  "departments": [
    { "id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "title": "Отдел продаж", "modules": ["Кабинет СМК"] }
  ]
}
```

В таблице появится (или обновится) строка:

| id | department_id | service_group_name | reg    | client_id | client_name   | mapping_status |
|----|---------------|--------------------|--------|-----------|---------------|----------------|
| 1  | a0eebc99-...  | Отдел продаж       | 123456 | 67        | ООО Ромашка   | active         |

Один и тот же `department_id` (UUID) может встречаться в разных компаниях (разные `reg` и `client_id`) — уникальность по паре `(reg, department_id)`.

Архивация: если в следующем сообщении для того же `reg` в `departments` нет отдела с данным `department_id`, у существующей строки выставляется `mapping_status = 'archived'`.
