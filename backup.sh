#!/usr/bin/env bash
# backup.sh — резервная копия БД + .session-файлов. ARCHITECTURE.md §18.1.
#
# Запускать на VPS из любого места (скрипт сам перейдёт в корень проекта).
# Cron, ежедневно в 03:00:
#   0 3 * * * /opt/telegram-outreach/backup.sh >> /var/log/to-backup.log 2>&1
#
# Параметры (можно переопределить переменными окружения):
#   BACKUP_DIR      — каталог для копий (по умолчанию /var/backups/telegram-outreach)
#   RETENTION_DAYS  — сколько дней хранить (по умолчанию 14)
# Имя БД/пользователя берётся из .env (POSTGRES_DB / POSTGRES_USER).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Подхватываем POSTGRES_* из .env, не хардкодя имена.
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
PG_USER="${POSTGRES_USER:-outreach}"
PG_DB="${POSTGRES_DB:-outreach}"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/telegram-outreach}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TS="$(date +%Y%m%d_%H%M)"
mkdir -p "$BACKUP_DIR"

echo "[backup] $(date -Is) → $BACKUP_DIR (db=$PG_DB user=$PG_USER)"

# 1. Дамп БД через контейнер db, сжатие gzip.
docker compose exec -T db pg_dump -U "$PG_USER" "$PG_DB" \
    | gzip > "$BACKUP_DIR/db_${TS}.sql.gz"

# 2. Архив .session-файлов Telethon.
tar czf "$BACKUP_DIR/sessions_${TS}.tar.gz" data/sessions

# 3. Ротация: удалить копии старше RETENTION_DAYS.
find "$BACKUP_DIR" -type f -mtime "+${RETENTION_DAYS}" -delete

echo "[backup] done: db_${TS}.sql.gz, sessions_${TS}.tar.gz"
