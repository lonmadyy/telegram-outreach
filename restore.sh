#!/usr/bin/env bash
# restore.sh — восстановление БД и/или .session-файлов из бэкапа. ARCHITECTURE.md §18.2.
#
# Использование:
#   ./restore.sh --db /var/backups/telegram-outreach/db_<ts>.sql.gz
#   ./restore.sh --sessions /var/backups/telegram-outreach/sessions_<ts>.tar.gz
#   ./restore.sh --db <...> --sessions <...>
#
# ВНИМАНИЕ: операция перезаписывает текущие данные. Скрипт запрашивает подтверждение.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
PG_USER="${POSTGRES_USER:-outreach}"
PG_DB="${POSTGRES_DB:-outreach}"

DB_FILE=""
SESS_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db) DB_FILE="${2:-}"; shift 2 ;;
        --sessions) SESS_FILE="${2:-}"; shift 2 ;;
        *) echo "Неизвестный аргумент: $1"; exit 1 ;;
    esac
done

if [[ -z "$DB_FILE" && -z "$SESS_FILE" ]]; then
    echo "Укажите --db <file.sql.gz> и/или --sessions <file.tar.gz>"
    exit 1
fi

echo "Восстановление ПЕРЕЗАПИШЕТ текущие данные:"
[[ -n "$DB_FILE" ]] && echo "  • БД     ← $DB_FILE"
[[ -n "$SESS_FILE" ]] && echo "  • сессии ← $SESS_FILE"
read -r -p "Продолжить? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Отменено."; exit 0; }

if [[ -n "$DB_FILE" ]]; then
    echo "[restore] БД из $DB_FILE"
    gunzip -c "$DB_FILE" | docker compose exec -T db psql -U "$PG_USER" "$PG_DB"
fi

if [[ -n "$SESS_FILE" ]]; then
    echo "[restore] сессии из $SESS_FILE"
    tar xzf "$SESS_FILE"
fi

echo "[restore] готово. Перезапустите приложение: docker compose restart app"
