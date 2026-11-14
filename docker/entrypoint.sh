#!/bin/bash
# Container start: back up the database if migrations are pending, migrate,
# then serve the ASGI app with Daphne.
#
# DB_USER/DB_PASSWORD is the login the running site uses. If
# DB_MIGRATE_USER/DB_MIGRATE_PASSWORD is set, the backup and migrations run as
# that login instead (the table owner), and it is removed from the environment
# before Daphne starts, so the site can only read and write rows.
set -uo pipefail

cd /app
source /app/.venv/bin/activate

BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-10}"
MIGRATE_USER="${DB_MIGRATE_USER:-${DB_USER:-postgres}}"
MIGRATE_PASSWORD="${DB_MIGRATE_PASSWORD:-${DB_PASSWORD:-}}"
unset DB_MIGRATE_USER DB_MIGRATE_PASSWORD

log() { echo "[entrypoint] $*"; }
as_migrator() { DB_USER="$MIGRATE_USER" DB_PASSWORD="$MIGRATE_PASSWORD" "$@"; }

# Wait for the database (the container may start before Postgres is reachable).
for attempt in $(seq 1 30); do
    if python manage.py shell -c "from django.db import connection; connection.ensure_connection()" >/dev/null 2>&1; then
        break
    fi
    log "database not reachable yet (attempt $attempt/30)"
    sleep 2
done

if as_migrator python manage.py migrate --check >/dev/null 2>&1; then
    log "no pending migrations"
else
    log "pending migrations; backing up database first"
    mkdir -p "$BACKUP_DIR"
    backup="$BACKUP_DIR/${DB_NAME:-db}-$(date +%Y%m%d-%H%M%S).sql.gz"
    if PGPASSWORD="$MIGRATE_PASSWORD" pg_dump -h "${DB_HOST:-localhost}" -U "$MIGRATE_USER" "${DB_NAME:-postgres}" | gzip > "$backup"; then
        log "backup written to $backup"
        ls -1t "$BACKUP_DIR"/*.sql.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f
    else
        log "ERROR: database backup failed; not migrating"
        rm -f "$backup"
        exit 1
    fi
    as_migrator python manage.py migrate --noinput || { log "ERROR: migrate failed"; exit 1; }
fi

unset MIGRATE_USER MIGRATE_PASSWORD
# One daphne, several listeners (broker/proxy.py routes by port):
#   PORT (8000)        the hub, on every interface
#   BROKER_PORT (8099) the broker RPC for app containers; bind it to the docker
#                      bridge (BROKER_BIND=172.17.0.1), never to the LAN
#   APP_PORT_BASE..+APP_PORTS-1 (9100..9123) each container app's UI, proxied
endpoints=(-e "tcp:port=${PORT:-8000}:interface=0.0.0.0")
endpoints+=(-e "tcp:port=${BROKER_PORT:-8099}:interface=${BROKER_BIND:-127.0.0.1}")
app_base="${APP_PORT_BASE:-9100}"
for ((i = 0; i < ${APP_PORTS:-24}; i++)); do
    endpoints+=(-e "tcp:port=$((app_base + i)):interface=0.0.0.0")
done
exec daphne --proxy-headers "${endpoints[@]}" BlackDiamondHub.asgi:application
