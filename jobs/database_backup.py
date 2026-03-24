"""
CRON JOB - Backup automatique de la base de donnees vers DB dediee
Execute 2x/jour pour sauvegarder la base de donnees de production
Chaque backup = un schema PostgreSQL nomme backup_YYYYMMDD_HHMMSS
Retention : 7 jours (schemas plus anciens supprimes automatiquement)
"""

from flask import Blueprint, request, jsonify, current_app
import os
import logging
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_DATABASE_BACKUP

backup_bp = Blueprint("backup", __name__)

RETENTION_DAYS = 7
SCHEMA_PREFIX = "backup_"

logger = logging.getLogger(__name__)

# Thread-safe state for concurrent access
_backup_lock = threading.Lock()
_last_backup = {
    "status": "idle",
    "started_at": None,
    "completed_at": None,
    "schema": None,
    "error": None,
    "size_mb": None,
    "schemas_purged": 0,
}


def _verify_token():
    """Verify cron job authentication token at call time."""
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        logger.error("CRON_SECRET not configured - backup endpoint disabled")
        return False
    token = request.headers.get("X-Job-Token")
    if token != secret:
        logger.warning("Unauthorized backup job attempt")
        return False
    return True


def _parse_db_url(url):
    """Parse a PostgreSQL URL into connection components."""
    # Normalize postgres:// to postgresql:// for consistent parsing
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/"),
        "sslmode": dict(p.split("=") for p in parsed.query.split("&") if "=" in p).get("sslmode", "prefer"),
    }


def _pg_env(password):
    """Return a subprocess env dict with PGPASSWORD set (avoids credentials in ps)."""
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    return env


def _pg_conn_args(parts):
    """Return common psql/pg_dump connection flags."""
    return [
        f"--host={parts['host']}",
        f"--port={parts['port']}",
        f"--username={parts['user']}",
        f"--dbname={parts['dbname']}",
    ]


def _sanitize_pg_error(stderr):
    """Strip credentials from PostgreSQL error messages."""
    import re
    first_line = stderr.strip().splitlines()[0] if stderr.strip() else ""
    return re.sub(r'postgresql://[^\s]+', '[redacted]', first_line)[:200]


def _update_state(**kwargs):
    """Thread-safe update of _last_backup."""
    with _backup_lock:
        _last_backup.update(kwargs)


def _get_state():
    """Thread-safe read of _last_backup."""
    with _backup_lock:
        return dict(_last_backup)


def _run_backup(app, schema_name):
    """Execute backup in background thread (holds _backup_lock to prevent concurrence)."""
    import traceback as _tb

    logger.info(f"Backup thread started for {schema_name}")

    try:
        # Create dump file with restrictive permissions (mode 600)
        fd, dump_path = tempfile.mkstemp(prefix=f"{schema_name}_", suffix=".dump", dir="/tmp")
        os.close(fd)
        os.chmod(dump_path, 0o600)
        logger.info("Dump file created")

        _update_state(
            status="running",
            started_at=datetime.utcnow().isoformat(),
            completed_at=None,
            schema=schema_name,
            error=None,
            size_mb=None,
            schemas_purged=0,
        )

        database_url = os.environ.get("DATABASE_URL")
        backup_url = os.environ.get("BACKUP_DATABASE_URL")
        logger.info(f"URLs loaded, parsing...")
        src = _parse_db_url(database_url)
        dst = _parse_db_url(backup_url)
        logger.info(f"Parsed OK: src={src['host']}, dst={dst['host']}")
    except Exception as e:
        logger.error(f"Backup init failed: {_tb.format_exc()}")
        print(f"BACKUP INIT FAILED: {_tb.format_exc()}", flush=True)
        _update_state(status="failed", error=str(e)[:200], completed_at=datetime.utcnow().isoformat())
        return

    try:
        with app.app_context():
            with CronJobLogger("database_backup_v2") as cron_log:
                # Step 1: pg_dump production database
                logger.info(f"Backup started: {schema_name}")

                result = subprocess.run(
                    ["pg_dump", *_pg_conn_args(src),
                     "--format=custom", "--no-owner", "--no-privileges",
                     f"--file={dump_path}"],
                    env=_pg_env(src["password"]),
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode != 0:
                    raise RuntimeError(f"pg_dump failed: {_sanitize_pg_error(result.stderr)}")

                dump_size = os.path.getsize(dump_path)
                size_mb = round(dump_size / (1024 * 1024), 1)
                _update_state(size_mb=size_mb)
                logger.info(f"1/4 - Dump termine ({size_mb} MB)")

                # Step 2+3: Restore into public, then rename to target schema
                # pg_restore 16 does not have --schema-mapping, so we:
                #   a) Drop+recreate public on backup DB
                #   b) pg_restore into public
                #   c) Rename public -> backup_YYYYMMDD_HHMMSS
                logger.info(f"2/4 - Restauration dans {schema_name}...")

                # 3a. Drop public on backup DB (clean slate for this restore)
                subprocess.run(
                    ["psql", *_pg_conn_args(dst), "-c",
                     "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"],
                    env=_pg_env(dst["password"]),
                    capture_output=True, text=True, timeout=30
                )

                # 3b. Restore dump into public schema
                result = subprocess.run(
                    ["pg_restore", "--no-owner", "--no-privileges",
                     *_pg_conn_args(dst), dump_path],
                    env=_pg_env(dst["password"]),
                    capture_output=True, text=True, timeout=600
                )
                # pg_restore returns 1 for warnings (like transaction_timeout), only fail on 2+
                if result.returncode >= 2:
                    raise RuntimeError(f"pg_restore failed: {_sanitize_pg_error(result.stderr)}")

                if result.returncode == 1:
                    logger.info(f"pg_restore completed with warnings")

                # 3c. Drop target schema if exists (re-run safety), then rename public
                result = subprocess.run(
                    ["psql", *_pg_conn_args(dst), "-c",
                     f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE; ALTER SCHEMA public RENAME TO "{schema_name}"; CREATE SCHEMA public;'],
                    env=_pg_env(dst["password"]),
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Schema rename failed: {_sanitize_pg_error(result.stderr)}")

                # Cleanup dump file
                os.remove(dump_path)
                dump_path = None

                # Step 4: Purge old schemas beyond retention
                logger.info(f"4/4 - Purge des schemas > {RETENTION_DAYS} jours...")
                purged = _purge_old_schemas(dst)

                _update_state(
                    status="success",
                    completed_at=datetime.utcnow().isoformat(),
                    schemas_purged=purged,
                )

                cron_log.set_counts(processed=1, failed=0, skipped=0)
                logger.info(f"Backup completed: {schema_name} ({size_mb}MB, {purged} purged)")

    except Exception as e:
        error_msg = _sanitize_pg_error(str(e))
        logger.error(f"Backup failed: {error_msg}")
        _update_state(
            status="failed",
            completed_at=datetime.utcnow().isoformat(),
            error=error_msg,
        )
        # CronJobLogger __exit__ handles the failed status automatically

    finally:
        # Cleanup dump file on failure
        if dump_path and os.path.exists(dump_path):
            os.remove(dump_path)


def _purge_old_schemas(dst):
    """Delete backup schemas older than RETENTION_DAYS."""
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y%m%d_%H%M%S")

    result = subprocess.run(
        ["psql", *_pg_conn_args(dst), "-t", "-A", "-c",
         f"SELECT schema_name FROM information_schema.schemata "
         f"WHERE schema_name LIKE '{SCHEMA_PREFIX}%' ORDER BY schema_name;"],
        env=_pg_env(dst["password"]),
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        logger.error(f"Failed to list schemas for purge")
        return 0

    purged = 0
    for schema in result.stdout.strip().split("\n"):
        schema = schema.strip()
        if not schema or not schema.startswith(SCHEMA_PREFIX):
            continue

        # Lexicographic comparison works because format is YYYYMMDD_HHMMSS (always UTC)
        timestamp_part = schema[len(SCHEMA_PREFIX):]
        if timestamp_part < cutoff_str:
            drop_result = subprocess.run(
                ["psql", *_pg_conn_args(dst), "-c",
                 f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;'],
                env=_pg_env(dst["password"]),
                capture_output=True, text=True, timeout=60
            )
            if drop_result.returncode == 0:
                purged += 1
                logger.info(f"Purged old backup schema: {schema}")
            else:
                logger.error(f"Failed to purge {schema}")

    return purged


@backup_bp.route("/jobs/database_backup", methods=["POST"])
@advisory_lock(LOCK_DATABASE_BACKUP)
def database_backup():
    """Endpoint cron pour executer le backup de la base de donnees."""
    if not _verify_token():
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    backup_url = os.environ.get("BACKUP_DATABASE_URL")
    database_url = os.environ.get("DATABASE_URL")

    if not backup_url:
        return jsonify({"status": "error", "message": "BACKUP_DATABASE_URL non configuree"}), 500

    if not database_url:
        return jsonify({"status": "error", "message": "DATABASE_URL non configuree"}), 500

    # Prevent concurrent backups (advisory lock is released when HTTP returns)
    if not _backup_lock.acquire(blocking=False):
        return jsonify({"status": "skipped", "message": "Backup already in progress"}), 200

    schema_name = SCHEMA_PREFIX + datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    def _run_and_release(app, name):
        try:
            _run_backup(app, name)
        except Exception as e:
            logger.error(f"Backup thread crashed: {e}")
        finally:
            _backup_lock.release()

    app = current_app._get_current_object()
    t = threading.Thread(target=_run_and_release, args=(app, schema_name), daemon=True)
    t.start()

    return jsonify({
        "status": "accepted",
        "message": "Backup started in background",
        "schema": schema_name,
        "timestamp": datetime.utcnow().isoformat()
    }), 202


@backup_bp.route("/jobs/backup_status", methods=["GET"])
def backup_status():
    """Endpoint pour verifier le statut du systeme de backup."""
    if not _verify_token():
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    backup_url = os.environ.get("BACKUP_DATABASE_URL")
    configured = backup_url is not None

    schema_count = None
    if configured:
        try:
            dst = _parse_db_url(backup_url)
            result = subprocess.run(
                ["psql", *_pg_conn_args(dst), "-t", "-A", "-c",
                 f"SELECT count(*) FROM information_schema.schemata "
                 f"WHERE schema_name LIKE '{SCHEMA_PREFIX}%';"],
                env=_pg_env(dst["password"]),
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                schema_count = int(result.stdout.strip())
        except Exception:
            pass

    return jsonify({
        "status": "configured" if configured else "not_configured",
        "backup_database_configured": configured,
        "retention_days": RETENTION_DAYS,
        "existing_snapshots": schema_count,
        "last_backup": _get_state(),
    }), 200


@backup_bp.route("/jobs/backup_logs", methods=["GET"])
def backup_logs():
    """Endpoint pour consulter les schemas de backup existants."""
    if not _verify_token():
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    backup_url = os.environ.get("BACKUP_DATABASE_URL")
    if not backup_url:
        return jsonify({"status": "error", "message": "BACKUP_DATABASE_URL non configuree"}), 500

    try:
        dst = _parse_db_url(backup_url)
        result = subprocess.run(
            ["psql", *_pg_conn_args(dst), "-t", "-A", "-c",
             f"SELECT schema_name FROM information_schema.schemata "
             f"WHERE schema_name LIKE '{SCHEMA_PREFIX}%' ORDER BY schema_name DESC;"],
            env=_pg_env(dst["password"]),
            capture_output=True, text=True, timeout=10
        )

        if result.returncode != 0:
            return jsonify({"status": "error", "message": "Failed to list backup schemas"}), 500

        schemas = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]

        return jsonify({
            "status": "success",
            "count": len(schemas),
            "schemas": schemas,
            "retention_days": RETENTION_DAYS,
            "last_backup": _get_state(),
        }), 200

    except Exception:
        return jsonify({"status": "error", "message": "Internal error listing schemas"}), 500
