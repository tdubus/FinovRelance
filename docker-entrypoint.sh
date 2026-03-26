#!/bin/bash
set -e

echo "=== FinovRelance - Demarrage ==="

# Attendre que PostgreSQL soit pret
if [ -n "$DATABASE_URL" ]; then
    echo "Attente de PostgreSQL..."
    # Extraire host et port de DATABASE_URL
    DB_HOST=$(echo "$DATABASE_URL" | sed -n 's|.*@\([^:/]*\).*|\1|p')
    DB_PORT=$(echo "$DATABASE_URL" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')
    DB_PORT=${DB_PORT:-5432}

    for i in $(seq 1 30); do
        if pg_isready -h "$DB_HOST" -p "$DB_PORT" > /dev/null 2>&1; then
            echo "PostgreSQL est pret."
            break
        fi
        echo "  Tentative $i/30 - PostgreSQL pas encore pret..."
        sleep 2
    done
fi

# Seed DB from dump file if SEED_DB=true (runs only once via lock file)
if [ "$SEED_DB" = "true" ] && [ -f "/app/dump.backup" ] && [ ! -f "/tmp/.seed_done" ]; then
    touch /tmp/.seed_done
    echo "SEED_DB=true detecte. Reset et restauration de la base de donnees..."
    echo "1/3 - Drop du schema public..."
    psql "$DATABASE_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    echo "2/3 - Restauration du dump..."
    pg_restore --no-owner --no-privileges -d "$DATABASE_URL" /app/dump.backup
    echo "3/3 - Seed termine. Pensez a remettre SEED_DB=false."
elif [ "$SEED_DB" = "true" ] && [ -f "/tmp/.seed_done" ]; then
    echo "Seed deja effectue (lock /tmp/.seed_done). Skipping."
elif [ "$SEED_DB" = "true" ]; then
    echo "ERREUR: SEED_DB=true mais dump.backup introuvable dans /app/"
fi

# Seed DB from remote URL if SEED_FROM_URL is set (runs only once via lock file)
# Usage: set SEED_FROM_URL=postgresql://user:pass@host/db in Coolify env vars
if [ -n "$SEED_FROM_URL" ] && [ ! -f "/tmp/.seed_url_done" ]; then
    touch /tmp/.seed_url_done
    echo "SEED_FROM_URL detecte. Migration depuis la base distante..."
    echo "1/4 - Dump de la base source..."
    pg_dump "$SEED_FROM_URL" --format=custom --no-owner -f /tmp/remote_dump.backup
    DUMP_SIZE=$(du -h /tmp/remote_dump.backup | cut -f1)
    echo "      Dump termine ($DUMP_SIZE)"
    echo "2/4 - Drop du schema public sur la cible..."
    psql "$DATABASE_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    echo "3/4 - Restauration sur la cible..."
    pg_restore --no-owner --no-privileges -d "$DATABASE_URL" /tmp/remote_dump.backup
    rm -f /tmp/remote_dump.backup
    echo "4/4 - Migration terminee. Retirez SEED_FROM_URL de vos variables d'environnement."
elif [ -n "$SEED_FROM_URL" ] && [ -f "/tmp/.seed_url_done" ]; then
    echo "Seed URL deja effectue (lock /tmp/.seed_url_done). Skipping."
fi

# Migrations automatiques si Flask-Migrate est utilise
if [ -d "migrations" ]; then
    echo "Application des migrations de base de donnees..."
    flask db upgrade
    echo "Migrations appliquees."
fi

# Demarrage Gunicorn (configurable via variables d'environnement)
GUNICORN_WORKERS=${GUNICORN_WORKERS:-4}
GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-120}

echo "Demarrage de Gunicorn (workers=$GUNICORN_WORKERS, timeout=$GUNICORN_TIMEOUT)..."
exec gunicorn --bind 0.0.0.0:5000 \
    --timeout $GUNICORN_TIMEOUT \
    --workers $GUNICORN_WORKERS \
    --threads 4 \
    --access-logfile - \
    --error-logfile - \
    main:app
