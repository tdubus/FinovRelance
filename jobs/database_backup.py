"""
CRON JOB - Backup automatique de la base de données
Exécuté quotidiennement pour sauvegarder la base de données de production
Sécurité : Backup pour protection contre les incidents
Version: PostgreSQL 16 compatible
"""

from flask import Blueprint, request, jsonify
import os
import logging
import subprocess
import sys
import threading
from datetime import datetime
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_DATABASE_BACKUP
from constants import HTTP_TIMEOUT_DEFAULT

# Blueprint pour les backups
backup_bp = Blueprint("backup", __name__)

# Secret pour sécuriser le cron
CRON_SECRET = os.getenv("CRON_SECRET")

# Logger
logger = logging.getLogger(__name__)

@backup_bp.route("/jobs/database_backup", methods=["POST"])
@advisory_lock(LOCK_DATABASE_BACKUP)
def database_backup():
    """Endpoint du cron pour exécuter le backup de la base de données"""

    # Vérification du token de sécurité
    token = request.headers.get("X-Job-Token")
    if not CRON_SECRET or token != CRON_SECRET:
        logger.warning("Unauthorized backup job attempt")
        return jsonify({
            "status": "error",
            "message": "Unauthorized"
        }), 403

    try:
        # Vérifier les variables d'environnement nécessaires
        required_vars = ['PGHOST', 'PGUSER', 'PGDATABASE']
        missing_vars = [var for var in required_vars if not os.getenv(var)]

        if missing_vars:
            error_msg = f"Variables d'environnement manquantes: {', '.join(missing_vars)}"
            logger.error(error_msg)
            return jsonify({
                "status": "error",
                "message": error_msg
            }), 500

        logger.info("Backup démarré")

        return jsonify({
            "status": "accepted",
            "message": "Backup started",
            "timestamp": datetime.utcnow().isoformat()
        }), 202

    except Exception as e:
        error_msg = f"Backup error: {str(e)}"
        logger.error(error_msg)
        return jsonify({
            "status": "error",
            "message": error_msg
        }), 500

@backup_bp.route("/jobs/backup_status", methods=["GET"])
def backup_status():
    """Endpoint pour vérifier le statut du système de backup"""

    # Vérification du token de sécurité
    token = request.headers.get("X-Job-Token")
    if not CRON_SECRET or token != CRON_SECRET:
        logger.warning("Unauthorized backup_status attempt")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    # Vérifier les variables d'environnement
    required_vars = {
        'PGHOST': os.getenv('PGHOST') is not None,
        'PGUSER': os.getenv('PGUSER') is not None,
        'PGDATABASE': os.getenv('PGDATABASE') is not None,
    }

    all_configured = all(required_vars.values())

    response = {
        "status": "configured" if all_configured else "not_configured",
        "configuration": required_vars,
        "ready_for_backup": all_configured
    }

    return jsonify(response), 200

@backup_bp.route("/jobs/backup_logs", methods=["GET"])
def backup_logs():
    """Endpoint pour consulter les logs du dernier backup"""

    # Vérification du token de sécurité
    token = request.headers.get("X-Job-Token")
    if not CRON_SECRET or token != CRON_SECRET:
        logger.warning("Unauthorized backup_logs attempt")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        # Trouver le fichier de log le plus récent
        import glob
        log_files = glob.glob("/tmp/backup_*.log")

        if not log_files:
            return jsonify({
                "status": "no_logs",
                "message": "Aucun log de backup trouvé"
            }), 404

        # Trier par date de modification (plus récent en premier)
        latest_log = max(log_files, key=os.path.getmtime)

        with open(latest_log, 'r') as f:
            content = f.read()

        return jsonify({
            "status": "success",
            "log_file": latest_log,
            "timestamp": datetime.fromtimestamp(os.path.getmtime(latest_log)).isoformat(),
            "content": content,
            "completed": "BACKUP TERMINÉ AVEC SUCCÈS" in content or "✓ BACKUP TERMINÉ" in content,
            "failed": "BACKUP ÉCHOUÉ" in content or "ERREUR CRITIQUE" in content
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500
