"""
Job HTTP endpoint pour refresh automatique des tokens OAuth comptables (QuickBooks, Xero, Business Central)

CONTEXTE:
Les tokens OAuth QuickBooks, Xero et Business Central expirent après 1 heure.
En environnement Replit Autoscale, les threads Python ne tournent pas de manière fiable.
Cette solution utilise un endpoint HTTP appelé par un cron externe (cron-job.org).

ARCHITECTURE:
- Endpoint: POST /jobs/refresh_accounting_tokens
- Authentification: Header X-Job-Token (CRON_SECRET)
- Fréquence recommandée: Toutes les 30 minutes
- Fenêtre de refresh: 30 minutes avant expiration (2 tentatives possibles)

SÉCURITÉ:
- Tokens chiffrés AES-256 dans la base de données
- Isolation complète entre entreprises
- Authentication par token secret partagé

NOTE XERO:
- Xero utilise des rotating refresh tokens (nouveau token à chaque refresh)
- Le système sauvegarde automatiquement le nouveau refresh token après chaque refresh

NOTE ODOO:
- ⚠️ Odoo N'EST PAS inclus dans ce job de refresh
- Raison: Odoo utilise des API Keys qui n'expirent pas automatiquement (pas d'OAuth)
- Les API Keys Odoo sont permanentes jusqu'à révocation manuelle par l'utilisateur
- Voir odoo_connector.py pour plus de détails sur l'architecture Odoo
"""

from flask import Blueprint, request, jsonify, current_app
from datetime import datetime
import os
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_REFRESH_ACCOUNTING_TOKENS

refresh_accounting_bp = Blueprint('refresh_accounting_tokens', __name__)


def verify_job_token():
    """Verify that the request contains a valid job authentication token"""
    job_token = request.headers.get('X-Job-Token')
    expected_token = os.environ.get('CRON_SECRET')

    if not expected_token:
        current_app.logger.error("CRON_SECRET not configured")
        return False

    if not job_token:
        current_app.logger.warning("Missing X-Job-Token header")
        return False

    if job_token != expected_token:
        current_app.logger.warning("Invalid job token")
        return False

    return True


@refresh_accounting_bp.route('/jobs/refresh_accounting_tokens', methods=['POST'])
@advisory_lock(LOCK_REFRESH_ACCOUNTING_TOKENS)
def refresh_accounting_tokens():
    """
    HTTP endpoint pour refresh automatique des tokens OAuth comptables

    Appelé par cron externe (cron-job.org) toutes les 30 minutes
    Refresh tous les tokens proches de l'expiration (< 30 min)

    Returns:
        JSON avec statistiques de refresh (refreshed, failed, skipped)
    """
    # Vérification de l'authentification
    if not verify_job_token():
        return jsonify({
            'success': False,
            'error': 'Unauthorized'
        }), 401

    with CronJobLogger('refresh_accounting_tokens') as job_log:
        try:
            # Refresh tous les connecteurs comptables
            stats = _refresh_accounting_connections()

            # Statistiques globales
            total_refreshed = stats['refreshed']
            total_failed = stats['failed']
            total_skipped = stats['skipped']

            job_log.set_counts(
                processed=total_refreshed,
                failed=total_failed,
                skipped=total_skipped
            )

            current_app.logger.info(
                f"=== REFRESH TERMINÉ: {total_refreshed} refreshed, "
                f"{total_failed} failed, {total_skipped} skipped ==="
            )

            return jsonify({
                'success': True,
                'timestamp': datetime.utcnow().isoformat(),
                'total_refreshed': total_refreshed,
                'total_failed': total_failed,
                'total_skipped': total_skipped,
                'details': {
                    'quickbooks_refreshed': stats.get('quickbooks_refreshed', 0),
                    'quickbooks_failed': stats.get('quickbooks_failed', 0),
                    'quickbooks_skipped': stats.get('quickbooks_skipped', 0),
                    'xero_refreshed': stats.get('xero_refreshed', 0),
                    'xero_failed': stats.get('xero_failed', 0),
                    'xero_skipped': stats.get('xero_skipped', 0),
                    'business_central_refreshed': stats.get('business_central_refreshed', 0),
                    'business_central_failed': stats.get('business_central_failed', 0),
                    'business_central_skipped': stats.get('business_central_skipped', 0),
                    'errors': stats['errors']
                }
            })

        except Exception as e:
            current_app.logger.error(f"Erreur critique lors du refresh des tokens comptables: {e}")
            raise


def _refresh_accounting_connections():
    """Refresh accounting connection tokens (parallelise avec ThreadPoolExecutor)"""
    from models import AccountingConnection
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app import app, db
    import threading

    stats = {
        'refreshed': 0,
        'failed': 0,
        'skipped': 0,
        'quickbooks_refreshed': 0,
        'quickbooks_failed': 0,
        'quickbooks_skipped': 0,
        'xero_refreshed': 0,
        'xero_failed': 0,
        'xero_skipped': 0,
        'business_central_refreshed': 0,
        'business_central_failed': 0,
        'business_central_skipped': 0,
        'errors': []
    }

    # Get all active accounting connections with OAuth tokens
    connections = AccountingConnection.query.filter(
        AccountingConnection.is_active == True,
        AccountingConnection._refresh_token.isnot(None)
    ).all()

    current_app.logger.info(f"Vérification de {len(connections)} connexions comptables actives")

    # Separer: connexions qui ont besoin de refresh vs skipped
    conns_to_refresh = []
    for conn in connections:
        if conn.needs_token_refresh():
            conns_to_refresh.append((conn.id, conn.system_type, conn.company_id))
        else:
            stats['skipped'] += 1
            stats[f'{conn.system_type}_skipped'] = stats.get(f'{conn.system_type}_skipped', 0) + 1

    if not conns_to_refresh:
        current_app.logger.info("Aucun token comptable a rafraichir")
        return stats

    current_app.logger.info(f"{len(conns_to_refresh)} tokens comptables a rafraichir en parallele (max 3 threads)")

    stats_lock = threading.Lock()

    def refresh_single_connection(conn_tuple):
        conn_id, system_type, company_id = conn_tuple
        with app.app_context():
            try:
                db.session.remove()
                conn = db.session.get(AccountingConnection, conn_id)
                if not conn:
                    return ('failed', system_type, company_id, 'Connection not found')

                success = _refresh_connection_token(conn)
                if success:
                    db.session.commit()
                    return ('refreshed', system_type, company_id, None)
                else:
                    return ('failed', system_type, company_id, 'Refresh returned False')
            except Exception as e:
                db.session.rollback()
                return ('failed', system_type, company_id, str(e))
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(refresh_single_connection, ct): ct for ct in conns_to_refresh}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = ('failed', 'unknown', 0, str(exc))
            status, system_type, company_id, error = result

            with stats_lock:
                stats[status] += 1
                stats[f'{system_type}_{status}'] = stats.get(f'{system_type}_{status}', 0) + 1
                if status == 'failed' and error:
                    error_msg = f"{system_type} company {company_id}: {error}"
                    stats['errors'].append(error_msg)
                    current_app.logger.error(f"Echec refresh {system_type} company {company_id}: {error}")
                elif status == 'refreshed':
                    current_app.logger.info(
                        f"Token {system_type} renouvele avec succes pour company {company_id}"
                    )

    return stats


def _refresh_connection_token(connection):
    """Refresh token for a specific accounting connection

    Args:
        connection: AccountingConnection instance

    Returns:
        bool: True if refresh successful, False otherwise
    """
    try:
        if connection.system_type == 'quickbooks':
            from quickbooks_connector import QuickBooksConnector
            connector = QuickBooksConnector(connection_id=connection.id, company_id=connection.company_id)
            return connector.refresh_access_token()

        elif connection.system_type == 'xero':
            from xero_connector import XeroConnector
            connector = XeroConnector(connection_id=connection.id, company_id=connection.company_id)
            return connector.refresh_access_token()

        elif connection.system_type == 'business_central':
            from business_central_connector import BusinessCentralConnector
            connector = BusinessCentralConnector(connection_id=connection.id)
            return connector.refresh_access_token()

        else:
            current_app.logger.warning(f"Unsupported system type for refresh: {connection.system_type}")
            return False

    except Exception as e:
        current_app.logger.error(f"Error refreshing {connection.system_type} token: {e}")
        return False
