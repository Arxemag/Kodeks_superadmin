# Деплой Kodeks superadmin (авто с ветки release)

Поток: **push в `release` → GitHub Actions заходит по SSH на сервер → `git pull` + `docker compose up -d --build`.**
`.env` живёт **на сервере** (секреты, в репозиторий не коммитятся).

Сервер: `45.10.41.70` (hostname `platforma`). Repo: `github.com/Arxemag/Kodeks_superadmin`, ветка `release`.

---

## 0. Важно перед первым деплоем
- **`.env` убран из git** (теперь в `.gitignore`). Эту правку нужно **закоммитить и запушить в `release`**, иначе `git reset --hard` на сервере затрёт прод-`.env`.
- **Порт 8000** на сервере свободен — на нём поднимется `auth-api`.
- `chromium` наружу не публикуется (нужен только воркеру по сети compose; на хосте 3000 занят).

---

## 1. Разовая подготовка сервера (под root: `su -`, пароль известен)
```bash
# 1.1 Дать пользователю user запускать docker без sudo (нужно для авто-деплоя по SSH)
usermod -aG docker user

# 1.2 Каталог приложения
mkdir -p /home/user/kodeks_superadmin && chown -R user:user /home/user/kodeks_superadmin
```
Дальше — **под `user`** (перелогиниться, чтобы применилась группа docker):
```bash
# 1.3 Клонировать release
git clone -b release https://github.com/Arxemag/Kodeks_superadmin.git /home/user/kodeks_superadmin
#   Если репозиторий ПРИВАТНЫЙ — клонировать с токеном/деплой-ключом:
#   git clone -b release https://<TOKEN>@github.com/Arxemag/Kodeks_superadmin.git /home/user/kodeks_superadmin

# 1.4 Положить прод-.env (см. раздел 3) в /home/user/kodeks_superadmin/.env

# 1.5 Первый запуск + создание таблиц и заливка reg
cd /home/user/kodeks_superadmin
docker compose up -d --build
docker compose exec worker python scripts/init_db.py --seed   # создаст таблицы + 6 регов
```

### SSH-ключ для GitHub Actions
```bash
# на своей машине: сгенерировать пару (без пароля)
ssh-keygen -t ed25519 -f deploy_key -N ""
# публичный ключ -> на сервер в authorized_keys пользователя user:
#   cat deploy_key.pub  >> /home/user/.ssh/authorized_keys   (на сервере)
# приватный ключ deploy_key -> в секрет GitHub DEPLOY_SSH_KEY
```

---

## 2. Секреты в GitHub (Settings → Secrets → Actions)
| Секрет | Значение |
|---|---|
| `DEPLOY_HOST` | `45.10.41.70` |
| `DEPLOY_USER` | `user` |
| `DEPLOY_SSH_KEY` | приватный ключ `deploy_key` (целиком) |

После этого: workflow `.github/workflows/deploy.yml` деплоит при каждом пуше в `release` (и по кнопке «Run workflow»).

---

## 3. Прод-`.env` (положить на сервер; значения подтверждены)
```dotenv
# БД — существующий postgres сервера (база postgres, user platforma)
DB_URL=postgresql+asyncpg://platforma:Deusmodus@host.docker.internal:5432/postgres

# Каталог (админ для правки кабинетов/виджетов)
ADMIN_LOGIN=kodeks
ADMIN_PASSWORD=skedoks

# Kafka сервера (внешний listener брокера kafka-0)
KAFKA_BOOTSTRAP_SERVERS=45.10.41.70:29092
KAFKA_GROUP_ID=superadmin-workers

# Топики уточнения кабинета (на брокере — через ДЕФИС!)
KAFKA_CORRECT_CABINET_TOPIC=correct-cabinet
KAFKA_CABINET_CORRECTED_TOPIC=cabinet-corrected
KAFKA_CORRECT_CABINET_DLQ_TOPIC=correct-cabinet-dlq

# Безопасный первый прогон: план в лог + ответ, но БЕЗ удаления виджетов. Потом -> false
CORRECT_CABINET_DRY_RUN=true

PORT=8000
LOG_LEVEL=INFO
CHROMIUM_WS_ENDPOINT=ws://chromium:3000
```
> Прочие топики users (`create_user`/`update_user` и т.п.) на брокере встречаются с РАЗНЫМ написанием
> (дефис/подчёркивание) — если будете подключать users-поток, сверьте имена с брокером и допишите
> `KAFKA_CREATE_TOPIC`/`KAFKA_UPDATE_TOPIC` в `.env`.

---

## 4. Как деплоить дальше
- **Авто:** `git push origin release` → GitHub Actions сам выкатит на сервер.
- **Вручную** (на сервере под `user`): `bash scripts/deploy.sh` (подтянет release + пересоберёт).

---

## 5. Проверка после деплоя
- API живой: `curl http://45.10.41.70:8000/api/expert/health` → `{"status":"ok"}`.
- Управление таблицей reg: открыть `http://45.10.41.70:8000/admin/reg-services`.
- Логи: `docker compose logs -f worker` (и `auth-api`). Метрики воркера: порт 9100.
- Воркер слушает `correct-cabinet` (см. лог при старте). Первый прогон — с `CORRECT_CABINET_DRY_RUN=true`.

---

## 6. Заметки / риски
- **`host.docker.internal`** в `DB_URL` работает за счёт `extra_hosts: host-gateway` в `docker-compose.yml`. Если на их Docker это не сработает — заменить хост в `DB_URL` на IP сервера (`45.10.41.70:5432`).
- БД `postgres` в схеме `public` сейчас пустая — наши таблицы создаст `init_db.py`, чужого не трогаем.
- Страница `/admin/reg-services` без авторизации — держать за VPN/закрытой сетью.
