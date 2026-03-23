#!/usr/bin/env python3
"""
Endpoint HTTP pour le refresh automatique des tokens email OAuth
Appelé par cron-job.org toutes les 30 minutes
"""

from flask import Blueprint, request, jsonify, current_app
from datetime import datetime
import os
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_REFRESH_EMAIL_TOKENS

refresh_tokens_bp = Blueprint('refresh_tokens', __name__)

@refresh_tokens_bp.route('/jobs/refresh_email_tokens', methods=['POST'])
@advisory_lock(LOCK_REFRESH_EMAIL_TOKENS)
def refresh_email_tokens():
    """
    Endpoint HTTP securise pour rafraichir les tokens email OAuth.
    Securite: Header X-Job-Token doit correspondre a CRON_SECRET.
    """

    # Vérification du token de sécurité
    job_token = request.headers.get('X-Job-Token')
    expected_token = os.environ.get('CRON_SECRET')

    if not expected_token:
        current_app.logger.error("CRON_SECRET non configuré")
        return jsonify({
            'success': False,
            'error': 'Configuration error'
        }), 500

    if job_token != expected_token:
        current_app.logger.warning(f"Tentative d'accès non autorisée à /jobs/refresh_email_tokens")
        return jsonify({
            'success': False,
            'error': 'Unauthorized'
        }), 401

    # Exécuter le refresh des tokens avec logging
    with CronJobLogger('refresh_email_tokens') as job_log:
        try:
            stats = {
                'system_refreshed': 0,
                'system_failed': 0,
                'system_skipped': 0,
                'user_refreshed': 0,
                'user_failed': 0,
                'user_skipped': 0,
                'errors': []
            }

            # Refresh system configurations
            stats_system = _refresh_system_configurations()
            stats['system_refreshed'] = stats_system['refreshed']
            stats['system_failed'] = stats_system['failed']
            stats['system_skipped'] = stats_system['skipped']
            stats['errors'].extend(stats_system['errors'])

            # Refresh user configurations
            stats_user = _refresh_user_configurations()
            stats['user_refreshed'] = stats_user['refreshed']
            stats['user_failed'] = stats_user['failed']
            stats['user_skipped'] = stats_user['skipped']
            stats['errors'].extend(stats_user['errors'])

            total_refreshed = stats['system_refreshed'] + stats['user_refreshed']
            total_failed = stats['system_failed'] + stats['user_failed']
            total_skipped = stats['system_skipped'] + stats['user_skipped']

            job_log.set_counts(
                processed=total_refreshed,
                failed=total_failed,
                skipped=total_skipped
            )

            current_app.logger.info(
                f"=== REFRESH TERMINÉ: {total_refreshed} refreshed, {total_failed} failed, {total_skipped} skipped ==="
            )

            # PERFORMANCE: Forcer le flush final de toutes les transactions SQLAlchemy
            # Cela évite que Gunicorn attende la confirmation différée de PostgreSQL
            from app import db
            try:
                db.session.commit()
                current_app.logger.debug("Final database commit successful")
            except Exception as commit_error:
                current_app.logger.warning(f"Final commit warning (non-critical): {commit_error}")
                # Non-bloquant - les commits individuels dans les fonctions de refresh ont déjà réussi

            return jsonify({
                'success': True,
                'timestamp': datetime.utcnow().isoformat(),
                'total_refreshed': total_refreshed,
                'total_failed': total_failed,
                'total_skipped': total_skipped,
                'details': stats
            }), 200

        except Exception as e:
            current_app.logger.error(f"Erreur lors du refresh des tokens: {str(e)}")
            raise


def _refresh_system_configurations():
    """Refresh system email configuration tokens"""
    from models import SystemEmailConfiguration
    from email_fallback import refresh_system_oauth_token
    import time

    stats = {
        'refreshed': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }

    start_time = time.time()

    # Get all active system email configurations
    configs = SystemEmailConfiguration.query.filter_by(is_active=True).all()

    current_app.logger.info(f"Processing {len(configs)} system configurations...")

    for config in configs:
        config_start_time = time.time()

        if not config.outlook_oauth_access_token or not config.outlook_oauth_refresh_token:
            stats['skipped'] += 1
            continue

        try:
            if config.needs_token_refresh():
                current_app.logger.info(
                    f"Token système proche de l'expiration pour {config.config_name}, renouvellement..."
                )
                refresh_system_oauth_token(config)
                stats['refreshed'] += 1
                elapsed = time.time() - config_start_time
                current_app.logger.info(
                    f"Token système renouvelé avec succès pour {config.config_name} (temps: {elapsed:.2f}s)"
                )
            else:
                stats['skipped'] += 1

        except Exception as e:
            stats['failed'] += 1
            elapsed = time.time() - config_start_time
            error_msg = f"Système {config.config_name}: {str(e)} (temps: {elapsed:.2f}s)"
            stats['errors'].append(error_msg)
            current_app.logger.error(f"Échec renouvellement système {config.config_name}: {e}")
            # ISOLATION: Continue avec les autres configs même si celle-ci échoue
            continue

    total_time = time.time() - start_time
    current_app.logger.info(
        f"System configs processing completed in {total_time:.2f}s: "
        f"{stats['refreshed']} refreshed, {stats['failed']} failed, {stats['skipped']} skipped"
    )

    return stats


def _refresh_user_configurations():
    """Refresh user email configuration tokens (parallelise avec ThreadPoolExecutor)"""
    from models import EmailConfiguration
    from token_refresh_scheduler import scheduler
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app import app, db
    import time
    import threading

    stats = {
        'refreshed': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }

    start_time = time.time()

    # Filtrer en SQL: seulement les configs avec tokens OAuth
    user_configs = EmailConfiguration.query.filter(
        EmailConfiguration._outlook_oauth_access_token.isnot(None),
        EmailConfiguration._outlook_oauth_refresh_token.isnot(None)
    ).all()

    current_app.logger.info(f"Processing {len(user_configs)} user configurations...")

    # Separer: configs qui ont besoin de refresh vs skipped
    configs_to_refresh = []
    for config in user_configs:
        if config.needs_token_refresh():
            configs_to_refresh.append((config.id, config.user_id, config.company_id))
        else:
            stats['skipped'] += 1

    if not configs_to_refresh:
        total_time = time.time() - start_time
        current_app.logger.info(
            f"User configs processing completed in {total_time:.2f}s: "
            f"0 refreshed, 0 failed, {stats['skipped']} skipped (aucun refresh necessaire)"
        )
        return stats

    current_app.logger.info(f"{len(configs_to_refresh)} tokens a rafraichir en parallele (max 5 threads)")

    # Stats thread-safe
    stats_lock = threading.Lock()

    def refresh_single_config(config_tuple):
        config_id, user_id, company_id = config_tuple
        with app.app_context():
            try:
                db.session.remove()
                config = db.session.get(EmailConfiguration, config_id)
                if not config:
                    return ('failed', config_id, 'Config not found')

                scheduler._refresh_user_oauth_token(config)
                db.session.commit()
                current_app.logger.info(
                    f"Token utilisateur renouvele pour user {user_id}, company {company_id}"
                )
                return ('refreshed', config_id, None)
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Echec renouvellement utilisateur {user_id}: {e}")
                return ('failed', config_id, str(e))
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(refresh_single_config, ct): ct for ct in configs_to_refresh}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = ('failed', 0, str(exc))

            status = result[0]
            config_id = result[1]
            error = result[2] if len(result) > 2 else None

            with stats_lock:
                stats[status] += 1
                if status == 'failed' and error:
                    stats['errors'].append(f"Config {config_id}: {error}")

    total_time = time.time() - start_time
    current_app.logger.info(
        f"User configs processing completed in {total_time:.2f}s: "
        f"{stats['refreshed']} refreshed, {stats['failed']} failed, {stats['skipped']} skipped"
    )

    return stats
