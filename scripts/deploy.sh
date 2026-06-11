#!/usr/bin/env bash
# Деплой Kodeks superadmin на сервере: подтянуть ветку release и перезапустить контейнеры.
# Требует: git, docker + docker compose v2, пользователь в группе docker (docker без sudo).
# .env лежит в каталоге приложения и НЕ перезаписывается git (он в .gitignore).
set -euo pipefail

APP_DIR="${APP_DIR:-/home/user/kodeks_superadmin}"
BRANCH="${BRANCH:-release}"

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "[deploy] ОШИБКА: нет $APP_DIR/.env — создайте прод-.env перед деплоем (см. docs/DEPLOY.md)"
  exit 1
fi

echo "[deploy] git fetch origin/$BRANCH"
git fetch --all --prune
git reset --hard "origin/$BRANCH"

echo "[deploy] docker compose up -d --build"
docker compose up -d --build

echo "[deploy] статус:"
docker compose ps
echo "[deploy] готово."
