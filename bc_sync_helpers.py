"""
Business Central Sync Helpers
Fonctions utilitaires pour améliorer la synchronisation Business Central
Implémentation du plan de refonte V2
"""

import logging
import time
import json
import random
import functools
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from models import SyncLog
from app import db
from sqlalchemy.exc import OperationalError, DisconnectionError
from constants import DEFAULT_PAGE_SIZE

logger = logging.getLogger(__name__)


def db_retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    """
    Décorateur pour retry automatique des opérations de base de données avec backoff exponentiel
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            had_connection_error = False

            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    if had_connection_error and attempt > 0:
                        logger.info(f"✅ Auto-reconnect successful after {attempt} retry(ies)")
                    return result
                except (OperationalError, DisconnectionError, Exception) as e:
                    last_exception = e
                    error_str = str(e).lower()

                    # Vérifier si c'est une erreur de connexion PostgreSQL
                    is_connection_error = any(keyword in error_str for keyword in [
                        'ssl connection has been closed',
                        'connection closed',
                        'invalid transaction',
                        'connection already closed',
                        'server closed the connection',
                        'lost synchronization with server',
                        'could not receive data from server'
                    ])

                    if not is_connection_error and attempt == 0:
                        # Pour les erreurs non-connexion, essayer une seule fois de plus
                        continue

                    if attempt < max_retries:
                        # Pour les erreurs de connexion SSL, utiliser INFO car ce sont des reconnexions automatiques normales
                        # Pour les autres erreurs, utiliser WARNING
                        if is_connection_error:
                            had_connection_error = True
                            logger.info(f"🔌 Auto-reconnect (attempt {attempt + 1}/{max_retries + 1}): SSL connection reset detected")
                        else:
                            logger.warning(f"🔄 DB operation failed (attempt {attempt + 1}/{max_retries + 1}): {str(e)[:100]}")

                        # CRITIQUE: Disposer le pool de connexions IMMÉDIATEMENT sur erreur SSL
                        # Cela force SQLAlchemy à créer une nouvelle connexion physique au prochain appel
                        if is_connection_error:
                            logger.debug("Disposing connection pool to force fresh connection")
                            try:
                                db.engine.dispose()  # Force new physical connection
                                db.session.remove()  # Clean up scoped session
                            except Exception:
                                pass  # Ignorer les erreurs de nettoyage
                        else:
                            try:
                                db.session.rollback()
                            except Exception:
                                pass

                        # Délai réduit pour erreurs de connexion (reconnection rapide)
                        if is_connection_error:
                            delay = 0.5  # Reconnection rapide après dispose du pool
                        else:
                            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)

                        logger.debug(f"⏳ Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                    else:
                        logger.error(f"❌ DB operation failed after {max_retries + 1} attempts: {e}")
                        raise last_exception

            return None  # Ne devrait jamais arriver
        return wrapper
    return decorator


class SyncMetrics:
    """Classe pour suivre les métriques de synchronisation en temps réel"""

    def __init__(self, sync_log_id: int):
        self.sync_log_id = sync_log_id
        self.start_time = datetime.utcnow()
        self.pages_processed = 0
        self.items_processed = 0
        self.errors_count = 0
        self.last_page_time = None
        self.last_heartbeat = datetime.utcnow()
        self.last_db_update = datetime.utcnow()
        self.update_interval = 60  # Mise à jour DB toutes les 60 secondes max

    def page_completed(self, items_count: int, processing_time: float):
        """Enregistrer la completion d'une page"""
        self.pages_processed += 1
        self.items_processed += items_count
        self.last_page_time = processing_time

        # Calculer les statistiques
        elapsed = (datetime.utcnow() - self.start_time).total_seconds()
        rate = self.items_processed / max(1, elapsed)

        # Mise à jour en DB seulement si nécessaire (throttling)
        now = datetime.utcnow()
        should_update_db = (
            (now - self.last_db_update).total_seconds() > self.update_interval or
            self.pages_processed % DEFAULT_PAGE_SIZE == 0  # Force update every DEFAULT_PAGE_SIZE pages
        )

        if should_update_db:
            self._update_metrics_with_retry(rate, elapsed, now)

    def _estimate_completion(self, rate: float, sync_log: SyncLog) -> Optional[datetime]:
        """Estimer l'heure de completion basée sur l'historique"""
        if rate > 0 and sync_log.estimated_total:
            remaining = max(0, sync_log.estimated_total - self.items_processed)
            seconds_remaining = remaining / rate
            return datetime.utcnow() + timedelta(seconds=seconds_remaining)
        return None

    @db_retry_with_backoff(max_retries=2, base_delay=0.5)
    def _update_metrics_with_retry(self, rate: float, elapsed: float, now: datetime):
        """Mettre à jour les métriques avec retry automatique"""
        sync_log = SyncLog.query.get(self.sync_log_id)
        if sync_log:
            sync_log.pages_processed = self.pages_processed
            sync_log.items_processed = self.items_processed
            sync_log.processing_rate = rate
            sync_log.avg_page_time = elapsed / max(1, self.pages_processed)
            sync_log.last_activity_at = now
            sync_log.estimated_completion = self._estimate_completion(rate, sync_log)
            db.session.commit()
            self.last_db_update = now

            # Log périodique
            if self.pages_processed % DEFAULT_PAGE_SIZE == 0:
                logger.info(f"📊 Progression: {self.items_processed} items, "
                           f"{rate:.1f} items/s, page {self.pages_processed}")

    def heartbeat(self):
        """Envoyer un heartbeat pour montrer que la sync est active"""
        now = datetime.utcnow()
        # Heartbeat moins fréquent pour réduire la charge DB
        if (now - self.last_heartbeat).total_seconds() > 120:  # Heartbeat toutes les 2 minutes
            self._heartbeat_with_retry(now)

    @db_retry_with_backoff(max_retries=2, base_delay=0.5)
    def _heartbeat_with_retry(self, now: datetime):
        """Heartbeat avec retry automatique"""
        sync_log = SyncLog.query.get(self.sync_log_id)
        if sync_log:
            sync_log.last_activity_at = now
            db.session.commit()
            self.last_heartbeat = now
            logger.debug(f"💓 Heartbeat sync {self.sync_log_id}")


def cleanup_old_synclogs(days_to_keep: int = 30) -> int:
    """Nettoyer les anciens logs de synchronisation"""
    cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)

    # Garder au moins les 10 derniers par connection
    from models import SyncLog, AccountingConnection

    deleted = 0

    for connection in AccountingConnection.query.all():
        # Récupérer les IDs à garder
        keep_ids = db.session.query(SyncLog.id)\
                            .filter_by(connection_id=connection.id)\
                            .order_by(SyncLog.started_at.desc())\
                            .limit(10)\
                            .subquery()

        # Supprimer les anciens sauf ceux à garder
        result = SyncLog.query.filter(
            SyncLog.connection_id == connection.id,
            SyncLog.started_at < cutoff_date,
            ~SyncLog.id.in_(keep_ids)
        ).delete(synchronize_session=False)

        deleted += result

    db.session.commit()

    if deleted > 0:
        logger.info(f"🗑️ {deleted} anciens logs de sync supprimés")

    return deleted


def build_delta_filter(connection, entity_type: str) -> Optional[str]:
    """Construire un filtre OData pour synchronisation delta"""
    try:
        # Vérifier si delta sync est activé
        if not hasattr(connection, 'delta_enabled') or not connection.delta_enabled:
            return None

        # Récupérer la dernière sync réussie
        last_sync = SyncLog.query.filter_by(
            connection_id=connection.id,
            sync_type=entity_type,
            status='completed'
        ).order_by(SyncLog.completed_at.desc()).first()

        if not last_sync or not last_sync.completed_at:
            logger.info("Première synchronisation ou pas de référence - full sync")
            return None

        # Vérifier si full sync forcée
        days_since = (datetime.utcnow() - last_sync.completed_at).days
        full_sync_interval = getattr(connection, 'full_sync_interval', 7)

        if days_since >= full_sync_interval:
            logger.info(f"Full sync forcée après {days_since} jours")
            return None

        # Construire le filtre delta
        delta_field = getattr(connection, 'delta_field', 'SystemModifiedAt')
        last_sync_iso = last_sync.completed_at.strftime('%Y-%m-%dT%H:%M:%SZ')

        # IMPORTANT: OData v4 pour DateTimeOffset - PAS de guillemets autour de la date
        # Format correct: field ge 2025-10-02T16:44:50Z (sans guillemets)
        delta_filter = f"{delta_field} ge {last_sync_iso}"
        logger.info(f"🔄 Delta sync activé: {delta_filter}")

        return delta_filter

    except Exception as e:
        logger.error(f"Erreur construction filtre delta: {e}")
        return None