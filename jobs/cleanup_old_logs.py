"""
CRON JOB - Purge automatique des tables de logs
Execute periodiquement pour supprimer les anciennes entrees de logs.
Securite: Header X-Job-Token doit correspondre a CRON_SECRET.
"""

from flask import Blueprint, request, jsonify, current_app
import os
import logging
from datetime import datetime, timedelta
from sqlalchemy import text
from constants import LOG_CLEANUP_DAYS
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_CLEANUP_OLD_LOGS

# Blueprint pour le nettoyage des logs
cleanup_bp = Blueprint("cleanup", __name__)

# Logger
logger = logging.getLogger(__name__)


@cleanup_bp.route("/jobs/cleanup_old_logs", methods=["POST"])
@advisory_lock(LOCK_CLEANUP_OLD_LOGS)
def cleanup_old_logs():
    """
    Endpoint HTTP securise pour purger les anciennes entrees de logs.
    Securite: Header X-Job-Token doit correspondre a CRON_SECRET.
    """

    # Verification du token de securite
    job_token = request.headers.get('X-Job-Token')
    expected_token = os.environ.get('CRON_SECRET')

    if not expected_token:
        logger.error("CRON_SECRET non configure")
        return jsonify({
            'success': False,
            'error': 'CRON_SECRET not configured'
        }), 500

    if job_token != expected_token:
        logger.warning("Unauthorized cleanup job attempt")
        return jsonify({
            'success': False,
            'error': 'Unauthorized'
        }), 401

    with CronJobLogger('cleanup_old_logs', details={'retention_days': LOG_CLEANUP_DAYS}) as cron_logger:
        try:
            from app import db

            cutoff_date = datetime.utcnow() - timedelta(days=LOG_CLEANUP_DAYS)
            results = {}
            total_deleted = 0
            batch_size = 1000

            # Tables a purger avec leurs colonnes de date
            # NOTE: audit_logs est volontairement exclu de cette liste.
            # Retention de 12 mois minimum requise pour conformite SOC 2.
            # Voir AUDIT_LOG_RETENTION_DAYS dans constants.py.
            tables_to_clean = [
                ('cron_job_logs', 'created_at', None),
                ('webhook_logs', 'received_at', None),
                ('notifications', 'created_at', "is_read = true"),
                ('sync_logs', 'started_at', None),
            ]

            for table_name, date_column, extra_condition in tables_to_clean:
                try:
                    deleted_for_table = 0

                    # Boucle de suppression par batch pour eviter de bloquer la DB
                    while True:
                        where_clause = f"{date_column} < :cutoff_date"
                        if extra_condition:
                            where_clause += f" AND {extra_condition}"

                        # Utilise id + ORDER BY date_column pour exploiter l'index existant
                        query = text(f"""
                            DELETE FROM {table_name}
                            WHERE id IN (
                                SELECT id FROM {table_name}
                                WHERE {where_clause}
                                ORDER BY {date_column} ASC
                                LIMIT :batch_size
                            )
                        """)

                        result = db.session.execute(
                            query,
                            {'cutoff_date': cutoff_date, 'batch_size': batch_size}
                        )
                        db.session.commit()

                        rows_deleted = result.rowcount
                        deleted_for_table += rows_deleted

                        # Log de progression pour les gros nettoyages
                        if deleted_for_table > 0 and deleted_for_table % 10000 == 0:
                            logger.info(f"  {table_name}: {deleted_for_table} rows supprimees...")

                        if rows_deleted < batch_size:
                            break

                    results[table_name] = deleted_for_table
                    total_deleted += deleted_for_table
                    logger.info(f"Cleanup {table_name}: {deleted_for_table} lignes supprimees")

                    # VACUUM ANALYZE apres nettoyage significatif (libere l'espace disque)
                    if deleted_for_table > 1000:
                        try:
                            with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as vacuum_conn:
                                vacuum_conn.execute(text(f"VACUUM ANALYZE {table_name}"))
                            logger.info(f"VACUUM ANALYZE {table_name} completed")
                        except Exception as vacuum_err:
                            logger.warning(f"VACUUM {table_name} skipped: {vacuum_err}")

                except Exception as table_error:
                    # La table n'existe peut-etre pas encore, continuer
                    db.session.rollback()
                    results[table_name] = f"erreur: {str(table_error)}"
                    logger.warning(f"Cleanup {table_name} echoue: {str(table_error)}")

            cron_logger.items_processed = total_deleted
            logger.info(f"Cleanup termine: {total_deleted} lignes supprimees au total")

            return jsonify({
                'success': True,
                'message': f'{total_deleted} lignes supprimees',
                'details': results,
                'cutoff_date': cutoff_date.isoformat(),
                'retention_days': LOG_CLEANUP_DAYS
            }), 200

        except Exception as e:
            logger.error(f"Erreur cleanup_old_logs: {str(e)}")
            cron_logger.items_failed = 1
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
