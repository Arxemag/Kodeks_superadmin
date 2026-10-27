#!/usr/bin/env bash
# Деплой Kodeks superadmin на сервере: подтянуть ветку release и перезапустить контейнеры.
# Требует: git, docker + docker compose v2, пользователь в группе docker (docker без sudo).
# .env лежит в каталоге приложения и НЕ перезаписывается git (он в .gitignore).
set -euo pipefail

APP_DIR="${APP_DIR:-/home/user/kodeks_superadmin}"
BRANCH="${BRANCH:-release}"
START_WORKER="${START_WORKER:-0}"
DEPLOY_COMMIT="${DEPLOY_COMMIT:-}"

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "[deploy] ОШИБКА: нет $APP_DIR/.env — создайте прод-.env перед деплоем (см. docs/DEPLOY.md)"
  exit 1
fi

echo "[deploy] git fetch origin/$BRANCH"
git fetch --all --prune
branch_commit=$(git rev-parse "origin/$BRANCH^{commit}")
if [ -n "$DEPLOY_COMMIT" ] && [ "$DEPLOY_COMMIT" != "$branch_commit" ]; then
  echo "[deploy] tested commit differs from current origin/$BRANCH; refusing deploy"
  exit 1
fi
git reset --hard "${DEPLOY_COMMIT:-$branch_commit}"

echo "[deploy] stop consumption before changing the application"
docker compose stop worker
docker compose build auth-api worker
docker compose run --rm --no-deps auth-api python -m scripts.init_db
docker compose up -d chromium auth-api

ready=0
for attempt in $(seq 1 30); do
  if docker compose exec -T auth-api python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:"+os.getenv("PORT","8000")+"/api/expert/ready",timeout=5)' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" != "1" ]; then
  echo "[deploy] API readiness failed; worker remains stopped"
  exit 1
fi

if [ "$START_WORKER" = "1" ]; then
  docker compose up -d worker
else
  echo "[deploy] API is ready; worker remains stopped for server preflight and acceptance"
fi

echo "[deploy] статус:"
docker compose ps
echo "[deploy] готово."
