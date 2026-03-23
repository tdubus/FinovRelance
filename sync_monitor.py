"""
Système de monitoring des synchronisations comptables
Détecte et corrige les synchronisations bloquées automatiquement
Version améliorée avec gestion des jobs et métriques en temps réel
Supporte : Business Central, QuickBooks, Xero, Odoo
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any
from app import db, app
from models import SyncLog, Notification, User, UserCompany
from bc_sync_helpers import cleanup_old_synclogs, db_retry_with_backoff
from utils.advisory_lock import try_advisory_lock, release_advisory_lock, LOCK_SYNC_MONITOR
from constants import (
    SYNC_CHECK_INTERVAL, SYNC_STUCK_THRESHOLD, SYNC_MAX_CONSECUTIVE_ERRORS,
    SYNC_AUTO_SHUTDOWN_CYCLES, LOG_CLEANUP_DAYS
)

logger = logging.getLogger(__name__)

class SyncMonitor:
    """Moniteur pour détecter et corriger les synchronisations bloquées"""

    def __init__(self):
        self.is_running = False
        self.check_interval = SYNC_CHECK_INTERVAL
        self.stuck_threshold = SYNC_STUCK_THRESHOLD
        self.monitor_thread = None
        self.last_successful_check = datetime.utcnow()
        self.consecutive_errors = 0
        self.max_consecutive_errors = SYNC_MAX_CONSECUTIVE_ERRORS

        # Auto-shutdown: Arrêter automatiquement après inactivité
        self.auto_shutdown_enabled = True
        self.auto_shutdown_cycles = SYNC_AUTO_SHUTDOWN_CYCLES
        self.no_active_syncs_cycles = 0

        # Métriques de diagnostic
        self.total_checks = 0
        self.total_errors = 0
        self.connection_errors = 0
        self.last_health_check = datetime.utcnow()

    def start_monitoring(self):
        """Démarrer le monitoring en arrière-plan"""
        if self.is_running:
            logger.info("Sync monitor already running")
            return

        self.is_running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("✅ Sync Monitor started - checking every 5 minutes")

    def stop_monitoring(self):
        """Arrêter le monitoring"""
        self.is_running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("Sync Monitor stopped")

    def _monitor_loop(self):
        """Boucle principale de monitoring"""
        # app is already imported directly

        with app.app_context():
            # Try to acquire advisory lock - only one instance should run the monitor
            lock_acquired = try_advisory_lock(db, LOCK_SYNC_MONITOR)
            if not lock_acquired:
                logger.info("Sync monitor already running on another instance, skipping")
                self.is_running = False
                return

            try:
                while self.is_running:
                    try:
                        # Vérifier les syncs bloquées
                        self._check_stuck_syncs()

                        # Nettoyer les anciens logs (1 fois par jour)
                        if datetime.now().hour == 2 and datetime.now().minute < 5:
                            cleanup_old_synclogs(days_to_keep=LOG_CLEANUP_DAYS)
                            self._cleanup_old_logs()

                        # AUTO-SHUTDOWN: Vérifier s'il y a des syncs actives
                        if self.auto_shutdown_enabled:
                            if self._has_active_syncs():
                                # Réinitialiser le compteur car il y a des syncs actives
                                self.no_active_syncs_cycles = 0
                            else:
                                # Incrémenter le compteur d'inactivité
                                self.no_active_syncs_cycles += 1
                                logger.info(f"⏸️ Aucune sync active détectée ({self.no_active_syncs_cycles}/{self.auto_shutdown_cycles} cycles)")

                                # Arrêter si inactivité prolongée
                                if self.no_active_syncs_cycles >= self.auto_shutdown_cycles:
                                    logger.info("🛑 Arrêt automatique du monitoring (aucune sync active depuis 10 minutes)")
                                    self.is_running = False
                                    break

                        # Marquer le cycle comme réussi
                        self._reset_error_counter()

                        # Mettre à jour les métriques
                        self.total_checks += 1

                        # Log de santé périodique (toutes les heures)
                        if (datetime.utcnow() - self.last_health_check).total_seconds() > 3600:
                            self._log_health_metrics()

                    except Exception as e:
                        self._handle_monitor_error(e)

                    # Attendre avant la prochaine vérification
                    for _ in range(self.check_interval):
                        if not self.is_running:
                            break
                        time.sleep(1)
            finally:
                # Always release the advisory lock when monitor stops
                release_advisory_lock(db, LOCK_SYNC_MONITOR)

    @db_retry_with_backoff(max_retries=2, base_delay=1.0)
    def _has_active_syncs(self) -> bool:
        """Vérifier s'il existe des synchronisations actives (SyncLog running)"""
        try:
            # Vérifier s'il y a des SyncLog avec status='running'
            running_syncs = SyncLog.query.filter(SyncLog.status == 'running').count()

            if running_syncs > 0:
                logger.debug(f"✅ Syncs actives: {running_syncs} running")

            return running_syncs > 0

        except Exception as e:
            logger.error(f"Erreur lors de la vérification des syncs actives: {e}")
            # En cas d'erreur, on considère qu'il y a des syncs actives pour éviter arrêt intempestif
            return True

    @db_retry_with_backoff(max_retries=2, base_delay=1.0)
    def _check_stuck_syncs(self):
        """Détecter et corriger les synchronisations bloquées"""
        cutoff_time = datetime.utcnow() - timedelta(seconds=self.stuck_threshold)
        grace_period = datetime.utcnow() - timedelta(seconds=300)  # 5 minutes de grâce pour l'initialisation

        # Trouver les syncs en cours depuis trop longtemps sans activité
        # IMPORTANT: Exclure les syncs qui viennent juste de démarrer (< 5 min) pour éviter les faux positifs
        stuck_syncs = SyncLog.query.filter(
            SyncLog.status == 'running',
            SyncLog.started_at < grace_period,  # Démarrée il y a plus de 5 minutes
            db.or_(
                SyncLog.last_activity_at < cutoff_time,
                SyncLog.last_activity_at.is_(None)
            )
        ).all()

        if not stuck_syncs:
            return

        logger.warning(f"🚨 Found {len(stuck_syncs)} stuck synchronizations")

        for sync_log in stuck_syncs:
            try:
                self._handle_stuck_sync(sync_log)
            except Exception as e:
                logger.error(f"Error handling stuck sync {sync_log.id}: {e}")

    def _handle_stuck_sync(self, sync_log: SyncLog):
        """Gérer une synchronisation bloquée"""
        duration = datetime.utcnow() - sync_log.started_at
        minutes = int(duration.total_seconds() / 60)

        logger.warning(f"🔧 Fixing stuck sync {sync_log.id} (running for {minutes} minutes)")

        # Marquer comme failed avec détails
        sync_log.status = 'failed'
        sync_log.completed_at = datetime.utcnow()
        sync_log.error_message = f'Synchronisation bloquée après {minutes} minutes - arrêtée automatiquement par le système de monitoring'

        try:
            db.session.commit()

            # Envoyer notification d'alerte aux admins
            self._send_stuck_sync_notification(sync_log, minutes)

            logger.info(f"✅ Stuck sync {sync_log.id} marked as failed and users notified")

        except Exception as e:
            logger.error(f"Error updating stuck sync {sync_log.id}: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass

    def _send_stuck_sync_notification(self, sync_log: SyncLog, minutes: int):
        """Envoyer notification pour une synchronisation bloquée"""
        try:
            from models import AccountingConnection

            connection = AccountingConnection.query.get(sync_log.connection_id)
            if not connection:
                return

            # Trouver les admins de l'entreprise
            admin_users = User.query.join(UserCompany).filter(
                UserCompany.company_id == connection.company_id,
                UserCompany.role.in_(['super_admin', 'admin']),
                UserCompany.is_active == True
            ).all()

            for admin_user in admin_users:
                # Message d'alerte simple et clair
                # Déterminer le nom du système
                system_name = {
                    'business_central': 'Business Central',
                    'quickbooks': 'QuickBooks',
                    'xero': 'Xero',
                    'odoo': 'Odoo'
                }.get(connection.system_type, connection.system_type.title())

                message = (f"🚨 Synchronisation {system_name} bloquée et corrigée automatiquement. "
                          f"Durée: {minutes} minutes. "
                          f"Données synchronisées: {sync_log.clients_synced or 0} clients, {sync_log.invoices_synced or 0} factures. "
                          f"Vous pouvez relancer une nouvelle synchronisation.")

                Notification.create_notification(
                    user_id=admin_user.id,
                    company_id=connection.company_id,
                    type=f'{connection.system_type}_sync_alert',
                    title='Synchronisation bloquée détectée',
                    message=message
                )

        except Exception as e:
            logger.error(f"Error sending stuck sync notification: {e}")

    def _cleanup_old_logs(self):
        """Nettoyer les anciens logs de synchronisation"""
        try:
            # Garder seulement les 100 derniers logs par connexion
            cutoff_date = datetime.utcnow() - timedelta(days=30)

            old_logs = SyncLog.query.filter(
                SyncLog.completed_at < cutoff_date,
                SyncLog.status.in_(['completed', 'failed', 'partial'])
            ).count()

            if old_logs > 0:
                # Supprimer par batch pour éviter la surcharge
                SyncLog.query.filter(
                    SyncLog.completed_at < cutoff_date,
                    SyncLog.status.in_(['completed', 'failed', 'partial'])
                ).limit(50).delete(synchronize_session=False)

                db.session.commit()
                logger.debug(f"Cleaned up {old_logs} old sync logs")

        except Exception as e:
            logger.error(f"Error cleaning up sync logs: {e}")
            db.session.rollback()

    def force_check_now(self) -> Dict[str, Any]:
        """Forcer une vérification immédiate (pour les tests)"""
        # app is already imported directly
        results = {'stuck_syncs_found': 0, 'stuck_syncs_fixed': 0, 'errors': []}

        with app.app_context():
            try:
                cutoff_time = datetime.utcnow() - timedelta(seconds=self.stuck_threshold)
                grace_period = datetime.utcnow() - timedelta(seconds=300)  # 5 minutes de grâce

                stuck_syncs = SyncLog.query.filter(
                    SyncLog.status == 'running',
                    SyncLog.started_at < grace_period,  # Démarrée il y a plus de 5 minutes
                    db.or_(
                        SyncLog.last_activity_at < cutoff_time,
                        SyncLog.last_activity_at.is_(None)
                    )
                ).all()

                results['stuck_syncs_found'] = len(stuck_syncs)

                for sync_log in stuck_syncs:
                    try:
                        self._handle_stuck_sync(sync_log)
                        results['stuck_syncs_fixed'] += 1
                    except Exception as e:
                        results['errors'].append(f"Sync {sync_log.id}: {str(e)}")

            except Exception as e:
                results['errors'].append(f"Monitor error: {str(e)}")

        return results

    def _handle_monitor_error(self, error: Exception):
        """Gérer les erreurs de monitoring avec isolation et récupération"""
        self.consecutive_errors += 1
        self.total_errors += 1
        logger.error(f"Error in sync monitor (#{self.consecutive_errors}): {error}")

        # CORRECTION RENFORCÉE: Gestion spécifique des erreurs PostgreSQL/SSL
        connection_error = False
        error_str = str(error).lower()

        # Détecter les erreurs de connexion PostgreSQL
        if any(keyword in error_str for keyword in [
            'ssl connection has been closed',
            'connection closed',
            'invalid transaction',
            'connection already closed',
            'server closed the connection',
            'lost synchronization with server',
            'could not receive data from server'
        ]):
            connection_error = True
            self.connection_errors += 1
            logger.warning("🔌 Détection d'erreur de connexion PostgreSQL - récupération renforcée")

        # Nettoyage session avec gestion spéciale pour erreurs de connexion
        try:
            if connection_error:
                # Pour les erreurs de connexion, fermer directement
                db.session.close()
                # Recréer une session propre
                db.session.remove()
            else:
                # Pour autres erreurs, essayer rollback normal
                db.session.rollback()
        except Exception as cleanup_error:
            logger.error(f"Error during session cleanup: {cleanup_error}")
            # Dernier recours: fermer brutalement
            try:
                db.session.close()
                db.session.remove()
            except Exception:
                pass  # Ignorer si même ça échoue

        # Gestion progressive des pauses selon les erreurs consécutives
        if self.consecutive_errors >= self.max_consecutive_errors:
            pause_time = 300  # 5 minutes si trop d'erreurs consécutives
            logger.critical(f"🚨 Trop d'erreurs consécutives ({self.consecutive_errors}), pause longue de {pause_time}s")
        elif connection_error:
            pause_time = min(30 + (self.consecutive_errors * 10), 120)  # 30s à 120s progressif
            logger.info(f"⏳ Pause de récupération ({pause_time}s) après erreur de connexion")
        else:
            pause_time = min(10 + (self.consecutive_errors * 5), 60)  # 10s à 60s progressif
            logger.info(f"⏳ Pause de récupération ({pause_time}s) après erreur")

        time.sleep(pause_time)

    def _reset_error_counter(self):
        """Reset le compteur d'erreurs après un cycle réussi"""
        if self.consecutive_errors > 0:
            logger.info(f"✅ Récupération réussie après {self.consecutive_errors} erreurs")
            self.consecutive_errors = 0
            self.last_successful_check = datetime.utcnow()

    def _log_health_metrics(self):
        """Logger les métriques de santé du système"""
        try:
            uptime = datetime.utcnow() - self.last_health_check
            error_rate = (self.total_errors / max(1, self.total_checks)) * 100
            connection_error_rate = (self.connection_errors / max(1, self.total_errors)) * 100 if self.total_errors > 0 else 0

            logger.info(f"🏥 Santé Sync Monitor - Uptime: {uptime}, "
                       f"Vérifications: {self.total_checks}, "
                       f"Erreurs: {self.total_errors} ({error_rate:.1f}%), "
                       f"Erreurs connexion: {self.connection_errors} ({connection_error_rate:.1f}% des erreurs)")

            # Tester la connexion DB
            try:
                from sqlalchemy import text
                db.session.execute(text('SELECT 1'))
                db.session.commit()
                logger.info("✅ Test connexion PostgreSQL: OK")
            except Exception as db_test_error:
                logger.error(f"❌ Test connexion PostgreSQL: {db_test_error}")

            self.last_health_check = datetime.utcnow()

        except Exception as e:
            logger.error(f"Erreur lors du log des métriques de santé: {e}")

    def get_health_status(self) -> dict:
        """Retourner le statut de santé actuel"""
        uptime = datetime.utcnow() - self.last_health_check
        return {
            'is_running': self.is_running,
            'uptime_seconds': uptime.total_seconds(),
            'total_checks': self.total_checks,
            'total_errors': self.total_errors,
            'connection_errors': self.connection_errors,
            'consecutive_errors': self.consecutive_errors,
            'last_successful_check': self.last_successful_check.isoformat(),
            'error_rate': (self.total_errors / max(1, self.total_checks)) * 100
        }

# Instance globale du moniteur
sync_monitor = SyncMonitor()

def start_sync_monitoring():
    """Démarrer le monitoring global"""
    sync_monitor.start_monitoring()

def ensure_monitoring_started():
    """
    Démarrer le monitoring seulement s'il n'est pas déjà en cours
    Cette fonction doit être appelée par chaque route de synchronisation AVANT de lancer le thread
    pour garantir qu'un monitoring actif surveille la synchronisation.

    Thread-safe: Peut être appelée simultanément par plusieurs routes sans créer de doublons.
    """
    if not sync_monitor.is_running:
        logger.info("🚀 Démarrage du monitoring pour nouvelle synchronisation")
        sync_monitor.start_monitoring()
    else:
        logger.debug("✅ Monitoring déjà actif, réinitialisation du compteur d'inactivité")
        # Réinitialiser le compteur pour éviter arrêt prématuré pendant sync active
        sync_monitor.no_active_syncs_cycles = 0

def stop_sync_monitoring():
    """Arrêter le monitoring global"""
    sync_monitor.stop_monitoring()

def check_sync_health():
    """Vérification immédiate de la santé des synchronisations"""
    return sync_monitor.force_check_now()

def get_monitor_health():
    """Obtenir les métriques de santé du moniteur"""
    return sync_monitor.get_health_status()