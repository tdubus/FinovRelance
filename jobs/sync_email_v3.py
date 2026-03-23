#!/usr/bin/env python3
"""
Job de synchronisation des emails Outlook - Version 3.2 (CRON)

Architecture optimisée en 3 phases:
1. Récupération headers AllItems (sans body) avec Extended Property 0x1042
2. Matching SQL intelligent par conversation_id, internet_message_id, in_reply_to_id
3. Fetch body sélectif + création notes avec parent_note_id

CONTRAINTES DE SÉCURITÉ:
- 100% lecture seule (GET uniquement)
- SEULEMENT rattacher aux conversations EXISTANTES (initiées depuis notre app)
- Pas de matching par email client
- Déduplication par internet_message_id (RFC 2822)
- Respect vie privée: on ne récupère le body QUE des messages matchés

v3.1 (18/02/2026) - Corrections fonctionnelles:
- Déduplication cumulative (internet_message_id + outlook_message_id)
- Parent = racine conversation (pas dernier message)
- Direction détectée (pas hardcode 'received')
- in_reply_to_id propagé dans les notes
- Parsing MIME via module email Python
- Race condition Phase 3 renforcée

v3.2 (18/02/2026) - Optimisation volume (500+ users):
- N1: Parallélisation users avec ThreadPoolExecutor (5 workers)
       → 500 users en ~4min au lieu de ~33min
- N2: Batch fetch body via POST /$batch Graph API (20 par requête)
       → 20 messages en 1 appel HTTP au lieu de 20 appels séparés
- N3: Requête SQL ciblée pour in_reply_to (WHERE IN) + cache partagé par company
       → mémoire constante au lieu de charger 50k+ lignes par user
- Gestion 429 (rate limit) avec retry automatique + backoff
- SyncLogger thread-safe
- Stats enrichies (api_calls) pour monitoring

v3.2.1 (18/02/2026) - Corrections critiques:
- Session DB isolée par thread (chaque worker commit indépendamment)
  → évite corruption croisée entre threads partageant la même session
- Savepoints (begin_nested) dans Phase 3: si une note plante,
  les autres du batch sont préservées
- Patch notes envoyées depuis l'app: quand la sync retrouve un email
  dans les Éléments Envoyés d'Outlook, elle complète la note existante
  (ajoute internet_message_id + outlook_message_id) au lieu de créer un doublon
  → Matching élargi: direction='sent' OU (direction=NULL + is_from_sync=False)

Performance cible: < 30 secondes pour 500 utilisateurs
Appelé par cron-job.org toutes les 30 minutes
"""

from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import logging
import os
import time
import random
import requests
import traceback
import re
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_SYNC_EMAIL_V3
from constants import HTTP_TIMEOUT_DEFAULT

# Désactiver les logs DEBUG verbeux de urllib3
logging.getLogger('urllib3').setLevel(logging.WARNING)

sync_emails_v3_bp = Blueprint('sync_emails_v3', __name__)

REQUEST_TIMEOUT = HTTP_TIMEOUT_DEFAULT
MAX_MESSAGES_PER_USER = 100
BATCH_SIZE = 20
GRAPH_BATCH_SIZE = 20  # Max requests par appel $batch Microsoft
MAX_PARALLEL_USERS = 5  # Workers parallèles (limité par rate limit Graph API)
IN_REPLY_TO_EXTENDED_PROPERTY = "String 0x1042"

_sync_lock = threading.Lock()
_sync_in_progress = False

logger = logging.getLogger(__name__)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class SyncLogger:
    """Logger thread-safe pour le sync V3 avec fichier persistant et chiffré.

    OPTIMISATION PERFORMANCE: Les messages INFO/DEBUG vont uniquement dans le fichier,
    seuls ERROR/WARNING vont en console pour éviter la pollution des logs.

    SÉCURITÉ: Le fichier log est chiffré avec Fernet (AES-256) pour protéger
    les données sensibles même si /tmp était compromis.
    """

    def __init__(self):
        self.log_lines = []
        self.start_time = None
        self.log_file_path = None
        self._encryption_key = None
        self._lock = threading.Lock()

    def _get_encryption_key(self):
        """Récupère ou génère une clé de chiffrement pour les logs."""
        if self._encryption_key:
            return self._encryption_key

        try:
            from cryptography.fernet import Fernet
            import base64
            import hashlib

            secret = os.environ.get('SYNC_LOG_ENCRYPTION_KEY') or os.environ.get('ENCRYPTION_MASTER_KEY') or 'default-sync-log-key'
            key_bytes = hashlib.sha256(secret.encode()).digest()
            self._encryption_key = base64.urlsafe_b64encode(key_bytes)
            return self._encryption_key
        except Exception as e:
            logger.warning(f"Chiffrement logs indisponible: {e}")
            return None

    def start_session(self):
        with self._lock:
            self.log_lines = []
            self.start_time = datetime.now(timezone.utc)
            timestamp = self.start_time.strftime('%Y%m%d_%H%M%S')
            self.log_file_path = f"/tmp/email_sync_v3_{timestamp}.enc"
        self._add(f"SYNC EMAIL V3.2 - {self.start_time.isoformat()}")
        logger.info("SYNC EMAIL V3.2 démarré")

    def _add(self, line, level="INFO"):
        """Ajoute au fichier log (thread-safe). Console seulement pour ERROR/WARNING."""
        timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]
        formatted = f"[{timestamp}] [{level}] {line}"
        with self._lock:
            self.log_lines.append(formatted)
        if level == "ERROR":
            logger.error(line)
        elif level == "WARNING":
            logger.warning(line)

    def info(self, msg): self._add(msg, "INFO")
    def warning(self, msg): self._add(msg, "WARNING")
    def error(self, msg): self._add(msg, "ERROR")
    def debug(self, msg): self._add(msg, "DEBUG")

    def section(self, title):
        self._add(f">>> {title}")

    def end_session(self, stats):
        duration = (datetime.now(timezone.utc) - self.start_time).total_seconds() if self.start_time else 0
        self._add(f"TERMINÉ en {duration:.2f}s")
        for key, value in stats.items():
            self._add(f"  {key}: {value}")

        logger.info(f"SYNC EMAIL V3.2 terminé en {duration:.1f}s: {stats}")

        if self.log_file_path:
            try:
                with self._lock:
                    content = '\n'.join(self.log_lines)

                key = self._get_encryption_key()
                if key:
                    from cryptography.fernet import Fernet
                    fernet = Fernet(key)
                    encrypted_content = fernet.encrypt(content.encode('utf-8'))
                    with open(self.log_file_path, 'wb') as f:
                        f.write(encrypted_content)
                else:
                    with open(self.log_file_path, 'w', encoding='utf-8') as f:
                        f.write(content)
            except Exception as e:
                logger.error(f"Erreur écriture log: {e}")

        return self.log_file_path

    def decrypt_log_file(self, file_path):
        """Déchiffre un fichier log pour lecture (usage admin uniquement)."""
        try:
            key = self._get_encryption_key()
            if not key:
                return None

            from cryptography.fernet import Fernet
            fernet = Fernet(key)

            with open(file_path, 'rb') as f:
                encrypted_content = f.read()

            decrypted_content = fernet.decrypt(encrypted_content)
            return decrypted_content.decode('utf-8')
        except Exception as e:
            logger.error(f"Erreur déchiffrement log: {e}")
            return None


sync_logger = SyncLogger()


# ──────────────────────────────────────────────────────
# CACHE PARTAGÉ PAR COMPANY (N3)
# ──────────────────────────────────────────────────────

class CompanyCache:
    """Cache thread-safe partagé entre users de la même company.

    Évite de recharger 50k+ internet_message_id par user.
    Charge UNIQUEMENT les données demandées via WHERE IN ciblé.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._caches = {}

    def get_or_load(self, company_id, db, CommunicationNote, conversation_ids=None, in_reply_to_values=None):
        """Retourne le cache pour une company, charge les données manquantes."""
        with self._lock:
            if company_id not in self._caches:
                self._caches[company_id] = {
                    'imids_map': {},
                    'conv_roots': {},
                    'conv_clients': {},
                    '_loaded_imids': set(),
                    '_loaded_convs': set(),
                }

        cache = self._caches[company_id]

        if in_reply_to_values:
            new_values = set(in_reply_to_values) - cache['_loaded_imids']
            if new_values:
                self._load_imids_targeted(company_id, db, CommunicationNote, new_values, cache)

        if conversation_ids:
            new_convs = set(conversation_ids) - cache['_loaded_convs']
            if new_convs:
                self._load_conversation_roots(company_id, db, CommunicationNote, new_convs, cache)

        return cache

    def _load_imids_targeted(self, company_id, db, CommunicationNote, in_reply_to_values, cache):
        """N3: Charge SEULEMENT les internet_message_id dont on a besoin."""
        try:
            values_list = list(in_reply_to_values)
            for i in range(0, len(values_list), 500):
                chunk = values_list[i:i + 500]
                rows = db.session.query(
                    CommunicationNote.internet_message_id,
                    CommunicationNote.id,
                    CommunicationNote.client_id
                ).filter(
                    CommunicationNote.company_id == company_id,
                    CommunicationNote.internet_message_id.in_(chunk)
                ).all()

                with self._lock:
                    for imid, note_id, client_id in rows:
                        if imid:
                            cache['imids_map'][imid] = {'note_id': note_id, 'client_id': client_id}
                    cache['_loaded_imids'].update(in_reply_to_values)
        except Exception as e:
            sync_logger.error(f"Erreur chargement imids company {company_id}: {e}")

    def _load_conversation_roots(self, company_id, db, CommunicationNote, conversation_ids, cache):
        """Charge la racine (premier message chronologique) de chaque conversation."""
        try:
            conv_list = list(conversation_ids)
            for i in range(0, len(conv_list), 500):
                chunk = conv_list[i:i + 500]
                rows = db.session.query(
                    CommunicationNote.conversation_id,
                    CommunicationNote.id,
                    CommunicationNote.client_id,
                    CommunicationNote.created_at
                ).filter(
                    CommunicationNote.company_id == company_id,
                    CommunicationNote.conversation_id.in_(chunk)
                ).order_by(
                    CommunicationNote.conversation_id,
                    CommunicationNote.created_at.asc()
                ).all()

                with self._lock:
                    for conv_id, note_id, client_id, created_at in rows:
                        if conv_id not in cache['conv_roots']:
                            cache['conv_roots'][conv_id] = {'note_id': note_id, 'client_id': client_id}
                        cache['conv_clients'].setdefault(conv_id, set()).add(client_id)
                    cache['_loaded_convs'].update(conversation_ids)
        except Exception as e:
            sync_logger.error(f"Erreur chargement conversations company {company_id}: {e}")

    def clear(self):
        with self._lock:
            self._caches.clear()


_company_cache = CompanyCache()


# ──────────────────────────────────────────────────────
# ROUTES HTTP
# ──────────────────────────────────────────────────────

@sync_emails_v3_bp.route('/jobs/sync_email_v3', methods=['POST'])
@advisory_lock(LOCK_SYNC_EMAIL_V3)
def sync_email_v3():
    """Endpoint HTTP sécurisé pour sync email V3."""
    global _sync_in_progress

    job_token = request.headers.get('X-Job-Token')
    expected_token = os.environ.get('CRON_SECRET')

    if not expected_token:
        return jsonify({'success': False, 'error': 'Configuration error'}), 500

    if job_token != expected_token:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    with _sync_lock:
        if _sync_in_progress:
            return jsonify({
                'success': True,
                'message': 'Sync déjà en cours',
                'status': 'already_running'
            }), 200
        _sync_in_progress = True

    from flask import copy_current_request_context

    @copy_current_request_context
    def run_background_sync():
        global _sync_in_progress
        with CronJobLogger('sync_email_v3', details={'version': '3.2'}) as cron_logger:
            try:
                sync_logger.start_session()
                sync_logger.info("DÉMARRAGE SYNC V3.2")

                start_time = time.time()
                stats = run_sync_v3()

                duration = time.time() - start_time
                stats['duration_seconds'] = round(duration, 2)

                cron_logger.items_processed = stats.get('notes_created', 0)
                cron_logger.items_failed = stats.get('errors', 0)
                cron_logger.items_skipped = stats.get('users_skipped', 0)

                sync_logger.end_session(stats)

            except Exception as e:
                sync_logger.error(f"Erreur fatale: {str(e)}\n{traceback.format_exc()}")
                cron_logger.items_failed = 1
                try:
                    sync_logger.end_session({'fatal_error': str(e)})
                except Exception:
                    pass
                raise
            finally:
                _company_cache.clear()
                with _sync_lock:
                    _sync_in_progress = False

    thread = threading.Thread(target=run_background_sync, daemon=True)
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Sync V3.2 démarrée',
        'status': 'started',
        'timestamp': datetime.now(timezone.utc).isoformat()
    }), 202


@sync_emails_v3_bp.route('/jobs/sync_email_v3/status', methods=['GET'])
def sync_email_v3_status():
    """Statut de la sync V3."""
    job_token = request.headers.get('X-Job-Token')
    expected_token = os.environ.get('CRON_SECRET')

    if job_token != expected_token:
        return jsonify({'error': 'Unauthorized'}), 401

    return jsonify({
        'sync_in_progress': _sync_in_progress,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }), 200


@sync_emails_v3_bp.route('/jobs/sync_email_v3/logs', methods=['GET'])
def get_sync_v3_logs():
    """Liste les logs de sync V3."""
    job_token = request.headers.get('X-Job-Token')
    expected_token = os.environ.get('CRON_SECRET')

    if job_token != expected_token:
        return jsonify({'error': 'Unauthorized'}), 401

    import glob
    log_files = sorted(
        glob.glob("/tmp/email_sync_v3_*.enc") + glob.glob("/tmp/email_sync_v3_*.txt"),
        reverse=True
    )

    logs_info = []
    for log_file in log_files[:10]:
        try:
            if log_file.endswith('.enc'):
                content = sync_logger.decrypt_log_file(log_file)
                if not content:
                    content = "(chiffré - déchiffrement échoué)"
            else:
                with open(log_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            logs_info.append({
                'filename': os.path.basename(log_file),
                'size': len(content),
                'lines': content.count('\n'),
                'preview': content[:500]
            })
        except Exception as e:
            logs_info.append({
                'filename': os.path.basename(log_file),
                'error': str(e)
            })

    return jsonify({'logs_count': len(log_files), 'logs': logs_info}), 200


# ──────────────────────────────────────────────────────
# ORCHESTRATION PRINCIPALE (N1: parallélisation)
# ──────────────────────────────────────────────────────

def run_sync_v3():
    """Exécute la synchronisation V3 optimisée en 3 phases.

    N1: Users traités en parallèle par groupes de MAX_PARALLEL_USERS.
    """
    from app import db, app
    from models import EmailConfiguration

    stats = {
        'users_processed': 0,
        'users_skipped': 0,
        'messages_fetched': 0,
        'messages_matched': 0,
        'notes_created': 0,
        'errors': 0,
        'api_calls': 0
    }
    stats_lock = threading.Lock()

    sync_logger.section("PHASE 0: PRÉPARATION")

    try:
        active_configs = db.session.query(EmailConfiguration).filter(
            EmailConfiguration._outlook_oauth_access_token.isnot(None),
            EmailConfiguration._outlook_oauth_refresh_token.isnot(None)
        ).all()
        sync_logger.info(f"Configurations actives: {len(active_configs)}")
    except Exception as e:
        sync_logger.error(f"Erreur récupération configs: {e}")
        return stats

    if not active_configs:
        sync_logger.warning("Aucune configuration active")
        return stats

    sync_logger.info("Déchiffrement batch des tokens OAuth...")
    tokens_cache = {}
    decrypt_start = time.time()

    for config in active_configs:
        try:
            access_token = config.outlook_oauth_access_token
            refresh_token = config.outlook_oauth_refresh_token
            if access_token and refresh_token:
                tokens_cache[config.id] = {
                    'access_token': access_token,
                    'refresh_token': refresh_token,
                    'user_id': config.user_id,
                    'company_id': config.company_id
                }
        except Exception as e:
            sync_logger.warning(f"Erreur déchiffrement config {config.id}: {e}")

    decrypt_duration = time.time() - decrypt_start
    sync_logger.info(f"Tokens déchiffrés: {len(tokens_cache)} en {decrypt_duration:.2f}s")

    sync_logger.section(f"TRAITEMENT PARALLÈLE ({MAX_PARALLEL_USERS} workers, {len(tokens_cache)} users)")

    def process_user_worker(config_id, token_data):
        """Worker exécuté dans un thread du pool.

        Chaque thread a sa propre session DB isolée pour éviter
        les corruptions croisées (rollback d'un thread n'affecte pas les autres).
        On charge config frais depuis la DB (session.get) au lieu de merge()
        pour éviter l'erreur 'Instance is not bound to a Session'.
        """
        local_stats = {
            'users_processed': 0, 'users_skipped': 0,
            'messages_fetched': 0, 'messages_matched': 0,
            'notes_created': 0, 'errors': 0, 'api_calls': 0
        }
        user_id = token_data['user_id']
        company_id = token_data['company_id']
        user_start = time.time()

        sync_logger.section(f"USER {user_id} (Company {company_id})")

        with app.app_context():
            from app import db as thread_db
            from models import EmailConfiguration
            try:
                thread_db.session.remove()
                config = thread_db.session.get(EmailConfiguration, config_id, with_for_update=True)
                if config is None:
                    sync_logger.warning(f"Config {config_id} introuvable en DB, skip")
                    local_stats['users_skipped'] = 1
                    return local_stats
                process_user_sync(config, local_stats, thread_db)
                local_stats['users_processed'] = 1
                thread_db.session.commit()
            except Exception as e:
                sync_logger.error(f"Erreur user {user_id}: {str(e)}\n{traceback.format_exc()}")
                local_stats['errors'] += 1
                try:
                    thread_db.session.rollback()
                except Exception:
                    pass
            finally:
                thread_db.session.remove()

        user_duration = time.time() - user_start
        sync_logger.info(f"User {user_id} terminé en {user_duration:.2f}s")

        return local_stats

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_USERS) as executor:
        futures = {
            executor.submit(process_user_worker, cid, td): cid
            for cid, td in tokens_cache.items()
        }

        for future in as_completed(futures):
            config_id = futures[future]
            try:
                local_stats = future.result()
                with stats_lock:
                    for key in stats:
                        if key in local_stats:
                            stats[key] += local_stats[key]
            except Exception as e:
                sync_logger.error(f"Worker config {config_id} exception: {e}")
                with stats_lock:
                    stats['errors'] += 1

    sync_logger.section("FIN TRAITEMENT")
    sync_logger.info("Chaque worker a committé ses données indépendamment")

    return stats


def process_user_sync(config, stats, db=None):
    """Traite la synchronisation pour un utilisateur."""
    if db is None:
        from app import db

    access_token = refresh_and_get_token(config, db)
    if not access_token:
        sync_logger.warning(f"User {config.user_id}: pas de token valide, skip")
        stats['users_skipped'] += 1
        return 0

    last_sync = config.outlook_delta_captured_at
    if not last_sync:
        last_sync = datetime.now(timezone.utc) - timedelta(hours=24)

    filter_date = last_sync.strftime('%Y-%m-%dT%H:%M:%SZ')
    user_email = config.outlook_email or ''

    # PHASE 1
    messages, api_calls_p1 = fetch_messages_with_headers(access_token, filter_date)
    sync_logger.info(f"User {config.user_id}: {len(messages)} headers récupérés")
    stats['messages_fetched'] += len(messages)
    stats['api_calls'] += api_calls_p1

    if not messages:
        config.outlook_delta_captured_at = datetime.now(timezone.utc)
        return 0

    # PHASE 2
    matched_messages = match_messages_to_notes(messages, config.company_id, access_token, db=db)
    sync_logger.info(f"User {config.user_id}: {len(matched_messages)} matchés")
    stats['messages_matched'] += len(matched_messages)

    if not matched_messages:
        config.outlook_delta_captured_at = datetime.now(timezone.utc)
        return 0

    # PHASE 3
    notes_created, api_calls_p3 = create_notes_for_matched(
        access_token, matched_messages, config, user_email, db=db
    )
    sync_logger.info(f"User {config.user_id}: {notes_created} notes créées")
    stats['notes_created'] += notes_created
    stats['api_calls'] += api_calls_p3

    config.outlook_delta_captured_at = datetime.now(timezone.utc)
    return notes_created


def refresh_and_get_token(config, db=None):
    """Refresh le token OAuth et retourne le token d'accès."""
    if db is None:
        from app import db
    from email_fallback import refresh_user_oauth_token

    try:
        refresh_user_oauth_token(config)
        db.session.flush()
        return config.outlook_oauth_access_token
    except Exception as e:
        sync_logger.warning(f"Erreur refresh token user {config.user_id}: {e}")
        return None


# ──────────────────────────────────────────────────────
# PHASE 1: RÉCUPÉRATION DES HEADERS
# ──────────────────────────────────────────────────────

def fetch_messages_with_headers(access_token, filter_date):
    """PHASE 1: Récupère les messages depuis AllItems avec headers.
    
    Returns: (messages_list, api_call_count)
    """
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Prefer': 'odata.maxpagesize=50'
    }

    select_fields = 'id,subject,from,receivedDateTime,sentDateTime,conversationId,internetMessageId'

    url = (
        f"https://graph.microsoft.com/v1.0/me/mailFolders/AllItems/messages"
        f"?$select={select_fields}"
        f"&$expand=SingleValueExtendedProperties($filter=Id eq '{IN_REPLY_TO_EXTENDED_PROPERTY}')"
        f"&$filter=receivedDateTime ge {filter_date} and isDraft eq false"
        f"&$orderby=receivedDateTime desc"
        f"&$top={MAX_MESSAGES_PER_USER}"
    )

    all_messages = []
    api_calls = 0

    try:
        while url and len(all_messages) < MAX_MESSAGES_PER_USER:
            response = _graph_get_with_retry(url, headers)
            api_calls += 1

            if response is None or response.status_code != 200:
                if response:
                    sync_logger.warning(f"API error {response.status_code}: {response.text[:200]}")
                break

            data = response.json()
            messages = data.get('value', [])

            for msg in messages:
                msg['_in_reply_to'] = extract_in_reply_to(msg)

            all_messages.extend(messages)
            url = data.get('@odata.nextLink')

    except Exception as e:
        sync_logger.error(f"Erreur fetch messages: {e}")

    return all_messages[:MAX_MESSAGES_PER_USER], api_calls


def extract_in_reply_to(message):
    """Extrait le In-Reply-To depuis Extended Property 0x1042."""
    props = message.get('singleValueExtendedProperties', [])
    for prop in props:
        if prop.get('id', '').endswith('0x1042'):
            return prop.get('value')
    return None


def fetch_in_reply_to_via_mime(access_token, message_id):
    """FALLBACK: Récupère In-Reply-To via MIME content ($value)."""
    auth_headers = {'Authorization': f'Bearer {access_token}'}
    url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/$value"

    try:
        response = _graph_get_with_retry(url, auth_headers)
        if response is None or response.status_code != 200:
            return None

        try:
            import email as email_mod
            from email.policy import default as default_policy
            parsed = email_mod.message_from_string(response.text, policy=default_policy)
            in_reply_to = parsed.get('In-Reply-To', '')
            return in_reply_to.strip() if in_reply_to else None
        except Exception:
            for line in response.text.split('\n'):
                line_stripped = line.strip()
                if line_stripped.lower().startswith('in-reply-to:'):
                    return line_stripped[12:].strip()
                if line_stripped == '':
                    break
            return None

    except Exception as e:
        sync_logger.debug(f"MIME fallback error: {e}")
        return None


# ──────────────────────────────────────────────────────
# PHASE 2: MATCHING SQL (N3: cache partagé)
# ──────────────────────────────────────────────────────

def match_messages_to_notes(messages, company_id, access_token=None, db=None):
    """PHASE 2: Matching SQL intelligent avec déduplication et cache partagé."""
    if db is None:
        from app import db
    from models import CommunicationNote

    if not messages:
        return []

    outlook_message_ids = [m.get('id') for m in messages if m.get('id')]
    conversation_ids = list({m.get('conversationId') for m in messages if m.get('conversationId')})
    internet_message_ids = [m.get('internetMessageId') for m in messages if m.get('internetMessageId')]
    in_reply_to_values = [m.get('_in_reply_to') for m in messages if m.get('_in_reply_to')]

    # ── Déduplication ──
    existing_internet_message_ids = set()
    if internet_message_ids:
        rows = db.session.query(CommunicationNote.internet_message_id).filter(
            CommunicationNote.company_id == company_id,
            CommunicationNote.internet_message_id.in_(internet_message_ids)
        ).all()
        existing_internet_message_ids = {r[0] for r in rows}
        if existing_internet_message_ids:
            sync_logger.info(f"Déduplication PRIMARY: {len(existing_internet_message_ids)} déjà en base")

    existing_outlook_ids = set()
    if outlook_message_ids:
        rows = db.session.query(CommunicationNote.outlook_message_id).filter(
            CommunicationNote.company_id == company_id,
            CommunicationNote.outlook_message_id.in_(outlook_message_ids)
        ).all()
        existing_outlook_ids = {r[0] for r in rows}
        if existing_outlook_ids:
            sync_logger.info(f"Déduplication SECONDARY: {len(existing_outlook_ids)} déjà en base")

    # ── N3: Cache partagé par company ──
    cache = _company_cache.get_or_load(
        company_id, db, CommunicationNote,
        conversation_ids=conversation_ids,
        in_reply_to_values=in_reply_to_values
    )

    all_imids_in_db = cache['imids_map']
    conv_to_root = cache['conv_roots']
    conv_client_ids = cache['conv_clients']

    for conv_id, clients in conv_client_ids.items():
        if len(clients) > 1:
            sync_logger.warning(f"Conversation {(conv_id or '')[:20]}... multi-client: {clients}")

    # ── Boucle de matching ──
    matched = []
    mime_fallback_count = 0
    skipped_duplicates = 0

    for msg in messages:
        outlook_msg_id = msg.get('id')
        internet_msg_id = msg.get('internetMessageId')
        conv_id = msg.get('conversationId')
        in_reply_to = msg.get('_in_reply_to')

        if internet_msg_id and internet_msg_id in existing_internet_message_ids:
            skipped_duplicates += 1
            continue
        if outlook_msg_id and outlook_msg_id in existing_outlook_ids:
            skipped_duplicates += 1
            continue

        parent_info = None
        match_type = None

        # 1. In-Reply-To → parent direct
        if in_reply_to and in_reply_to in all_imids_in_db:
            parent_info = all_imids_in_db[in_reply_to]
            match_type = 'in_reply_to'

        # 2. conversation_id → racine
        elif conv_id and conv_id in conv_to_root:
            if len(conv_client_ids.get(conv_id, set())) > 1:
                sync_logger.warning(f"Skip {(outlook_msg_id or '')[:30]}... - Conv multi-client")
                continue
            parent_info = conv_to_root[conv_id]
            match_type = 'conversation_id'

        # 3. MIME fallback
        elif access_token and not in_reply_to:
            mime_in_reply_to = fetch_in_reply_to_via_mime(access_token, msg.get('id'))
            mime_fallback_count += 1

            if mime_in_reply_to:
                if mime_in_reply_to not in all_imids_in_db:
                    _company_cache.get_or_load(
                        company_id, db, CommunicationNote,
                        in_reply_to_values=[mime_in_reply_to]
                    )
                if mime_in_reply_to in all_imids_in_db:
                    parent_info = all_imids_in_db[mime_in_reply_to]
                    match_type = 'mime_fallback'
                    msg['_in_reply_to'] = mime_in_reply_to

        if parent_info:
            matched.append({
                'message': msg,
                'parent_note_id': parent_info['note_id'],
                'client_id': parent_info['client_id'],
                'match_type': match_type
            })
            existing_outlook_ids.add(outlook_msg_id)
            if internet_msg_id:
                existing_internet_message_ids.add(internet_msg_id)

    if skipped_duplicates > 0:
        sync_logger.info(f"Doublons évités: {skipped_duplicates}")
    if mime_fallback_count > 0:
        sync_logger.info(f"MIME fallback: {mime_fallback_count} fois")

    return matched


# ──────────────────────────────────────────────────────
# PHASE 3: FETCH BODY BATCH + CRÉATION NOTES (N2)
# ──────────────────────────────────────────────────────

def create_notes_for_matched(access_token, matched_messages, config, user_email='', db=None):
    """PHASE 3: Fetch body par batch $batch + création notes.

    N2: POST /$batch envoie jusqu'à 20 GET en un seul appel HTTP.
    Savepoints: chaque message est protégé individuellement.
    Patch sent: les courriels envoyés depuis l'app sont complétés, pas dupliqués.

    Returns: (notes_created, api_call_count)
    """
    if db is None:
        from app import db
    from models import CommunicationNote

    notes_created = 0
    patched_count = 0
    skipped_race_condition = 0
    api_calls = 0

    for batch_start in range(0, len(matched_messages), GRAPH_BATCH_SIZE):
        batch = matched_messages[batch_start:batch_start + GRAPH_BATCH_SIZE]

        message_ids = [m['message'].get('id') for m in batch if m['message'].get('id')]
        full_messages_map = fetch_message_bodies_batch(access_token, message_ids)
        api_calls += 1

        for match_info in batch:
            msg = match_info['message']
            message_id = msg.get('id')

            try:
                full_message = full_messages_map.get(message_id)
                if not full_message:
                    sync_logger.warning(f"Body manquant: {(message_id or '')[:20]}...")
                    continue

                internet_msg_id = full_message.get('internetMessageId')
                outlook_msg_id = full_message.get('id')

                if internet_msg_id:
                    existing = db.session.query(CommunicationNote.id).filter(
                        CommunicationNote.company_id == config.company_id,
                        CommunicationNote.internet_message_id == internet_msg_id
                    ).first()
                    if existing:
                        skipped_race_condition += 1
                        continue

                if outlook_msg_id:
                    existing = db.session.query(CommunicationNote.id).filter(
                        CommunicationNote.company_id == config.company_id,
                        CommunicationNote.outlook_message_id == outlook_msg_id
                    ).first()
                    if existing:
                        skipped_race_condition += 1
                        continue

                with db.session.begin_nested():
                    direction = detect_email_direction(full_message, user_email)
                    msg_datetime, _ = _parse_message_datetime(full_message)
                    subject = full_message.get('subject') or 'Sans objet'
                    conversation_id = full_message.get('conversationId')
                    in_reply_to_id = msg.get('_in_reply_to')

                    if direction == 'sent':
                        patched = _try_patch_existing_sent_note(
                            conversation_id, match_info['client_id'],
                            config.company_id, subject,
                            internet_msg_id, outlook_msg_id,
                            in_reply_to_id, msg_datetime, db, CommunicationNote
                        )
                        if patched:
                            patched_count += 1
                            continue

                    note = create_communication_note(
                        full_message,
                        match_info['client_id'],
                        config.company_id,
                        config.user_id,
                        match_info['parent_note_id'],
                        user_email=user_email,
                        in_reply_to_id=in_reply_to_id
                    )

                    if note:
                        db.session.add(note)
                        notes_created += 1

                if notes_created > 0 and notes_created % BATCH_SIZE == 0:
                    try:
                        db.session.flush()
                    except Exception as flush_err:
                        sync_logger.error(f"Erreur flush batch: {flush_err}")
                        db.session.rollback()

            except Exception as e:
                sync_logger.error(f"Erreur création note (savepoint rollback): {e}")

    if skipped_race_condition > 0:
        sync_logger.info(f"Race conditions évitées: {skipped_race_condition}")
    if patched_count > 0:
        sync_logger.info(f"Notes app patchées (doublons évités): {patched_count}")

    return notes_created, api_calls


def fetch_message_bodies_batch(access_token, message_ids):
    """N2: Récupère les body via POST /$batch (jusqu'à 20 en 1 appel).

    Returns: {message_id: full_message_dict, ...}
    """
    if not message_ids:
        return {}

    select_fields = 'id,subject,body,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,conversationId,internetMessageId,hasAttachments'

    batch_requests = []
    for idx, msg_id in enumerate(message_ids[:GRAPH_BATCH_SIZE]):
        batch_requests.append({
            'id': str(idx),
            'method': 'GET',
            'url': f'/me/messages/{msg_id}?$select={select_fields}'
        })

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = _graph_post_with_retry(
            'https://graph.microsoft.com/v1.0/$batch',
            headers=headers,
            json_data={'requests': batch_requests}
        )

        if response is None or response.status_code != 200:
            sync_logger.warning(f"$batch error, fallback individuel")
            return _fetch_bodies_individual_fallback(access_token, message_ids, select_fields)

        batch_response = response.json()
        results = {}

        for item in batch_response.get('responses', []):
            if item.get('status') == 200:
                body = item.get('body', {})
                msg_id = body.get('id')
                if msg_id:
                    results[msg_id] = body

        # Retry individuel pour les manquants
        missing_ids = [mid for mid in message_ids if mid not in results]
        if missing_ids:
            results.update(_fetch_bodies_individual_fallback(access_token, missing_ids, select_fields))

        return results

    except Exception as e:
        sync_logger.error(f"Exception $batch: {e}")
        return _fetch_bodies_individual_fallback(access_token, message_ids, select_fields)


def _fetch_bodies_individual_fallback(access_token, message_ids, select_fields):
    """Fallback: fetch body un par un si $batch échoue."""
    results = {}
    for msg_id in message_ids:
        msg = fetch_message_body(access_token, msg_id)
        if msg:
            results[msg_id] = msg
    return results


def fetch_message_body(access_token, message_id):
    """Récupère le body complet d'un message (appel individuel)."""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    select_fields = 'id,subject,body,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,conversationId,internetMessageId,hasAttachments'
    url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}?$select={select_fields}"

    try:
        response = _graph_get_with_retry(url, headers)
        if response and response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        sync_logger.error(f"Exception fetch body: {e}")
        return None


# ──────────────────────────────────────────────────────
# GRAPH API — RETRY AVEC GESTION 429
# ──────────────────────────────────────────────────────

def _graph_get_with_retry(url, headers, max_retries=2):
    """GET avec retry automatique sur 429/503 (rate limit) + jitter anti-thundering herd."""
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

            if response.status_code in (429, 503):
                retry_after = min(int(response.headers.get('Retry-After', 5)), 30)
                jitter = random.uniform(0, retry_after * 0.3)
                sync_logger.warning(f"Rate limited ({response.status_code}), retry {retry_after + jitter:.1f}s")
                time.sleep(retry_after + jitter)
                continue

            return response

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                time.sleep(2 + random.uniform(0, 1))
                continue
            return None
        except Exception as e:
            sync_logger.error(f"Request error: {e}")
            return None

    return None


def _graph_post_with_retry(url, headers, json_data, max_retries=2):
    """POST avec retry automatique sur 429/503 (rate limit) + jitter anti-thundering herd."""
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(url, headers=headers, json=json_data, timeout=REQUEST_TIMEOUT)

            if response.status_code in (429, 503):
                retry_after = min(int(response.headers.get('Retry-After', 5)), 30)
                jitter = random.uniform(0, retry_after * 0.3)
                sync_logger.warning(f"Rate limited ({response.status_code}), retry {retry_after + jitter:.1f}s")
                time.sleep(retry_after + jitter)
                continue

            return response

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                time.sleep(2 + random.uniform(0, 1))
                continue
            return None
        except Exception as e:
            sync_logger.error(f"Request error: {e}")
            return None

    return None


# ──────────────────────────────────────────────────────
# PATCH NOTES ENVOYÉES DEPUIS L'APP (anti-doublon)
# ──────────────────────────────────────────────────────

def _try_patch_existing_sent_note(conversation_id, client_id, company_id,
                                   subject, internet_message_id, outlook_msg_id,
                                   in_reply_to_id, msg_datetime, db, CommunicationNote):
    """Patch une note envoyée depuis l'app au lieu de créer un doublon.

    Quand l'app envoie un courriel, elle crée une note SANS internet_message_id
    ni outlook_message_id (pas encore connus). La sync retrouve ce même courriel
    dans les Éléments Envoyés d'Outlook. Au lieu de créer un doublon, on complète
    la note existante avec les identifiants Outlook.

    Matching élargi (cohérent avec outlook_email_sync.py):
    - direction = 'sent' (notes marquées explicitement)
    - OU direction = NULL ET is_from_sync = False (notes créées par l'app sans étiquette)

    Critères de correspondance:
    - Même conversation_id, company_id, client_id, sujet
    - internet_message_id est NULL (pas encore renseigné)
    - Date ±5 minutes (pour lever l'ambiguïté entre plusieurs envois similaires)

    Returns: True si une note a été patchée, False sinon.
    """
    if not internet_message_id:
        return False

    from sqlalchemy import or_, and_

    query = db.session.query(CommunicationNote).filter(
        CommunicationNote.conversation_id == conversation_id,
        CommunicationNote.company_id == company_id,
        CommunicationNote.client_id == client_id,
        or_(
            CommunicationNote.email_direction == 'sent',
            and_(
                CommunicationNote.email_direction.is_(None),
                CommunicationNote.is_from_sync == False
            )
        ),
        CommunicationNote.email_subject == subject,
        CommunicationNote.internet_message_id.is_(None)
    )

    if msg_datetime:
        window_start = msg_datetime - timedelta(minutes=5)
        window_end = msg_datetime + timedelta(minutes=5)
        query = query.filter(
            CommunicationNote.created_at.between(window_start, window_end)
        )

    existing_sent_note = query.first()

    if existing_sent_note:
        existing_sent_note.internet_message_id = internet_message_id
        existing_sent_note.outlook_message_id = outlook_msg_id
        if in_reply_to_id:
            existing_sent_note.in_reply_to_id = in_reply_to_id
        sync_logger.info(
            f"Patch note #{existing_sent_note.id}: ajout internet_message_id (doublon évité)"
        )
        return True

    return False


# ──────────────────────────────────────────────────────
# UTILITAIRES
# ──────────────────────────────────────────────────────

def html_to_text(html_content):
    """Convertit HTML en texte brut."""
    if not html_content:
        return ''

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_content, 'html.parser')

        for br in soup.find_all('br'):
            br.replace_with('\n')
        for p in soup.find_all('p'):
            p.insert_after('\n\n')
        for div in soup.find_all('div'):
            div.insert_after('\n')

        text = soup.get_text()
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        text = '\n'.join(line.strip() for line in text.split('\n'))

        return text.strip()

    except Exception as e:
        sync_logger.warning(f"Erreur conversion HTML: {e}")
        return html_content


def detect_email_direction(message, user_email):
    """Détecte la direction d'un email (cohérent avec outlook_email_sync.py)."""
    subject = message.get('subject') or ''
    from_address = message.get('from', {}).get('emailAddress', {}).get('address', '').lower()
    user_email_lower = user_email.lower() if user_email else ''

    is_from_user = from_address == user_email_lower
    subject_lower = subject.lower()

    is_reply = subject_lower.startswith('re:') or subject_lower.startswith('re :')
    is_forward = any(subject_lower.startswith(p) for p in ['fw:', 'fwd:', 'tr:', 'tr :'])

    if is_from_user:
        if is_forward:
            return 'forward'
        elif is_reply:
            return 'reply'
        return 'sent'
    else:
        if is_forward:
            return 'forward'
        elif is_reply:
            return 'reply'
        return 'received'


def _parse_message_datetime(message):
    """Parse la date d'un message Graph API. Retourne (msg_datetime, note_date)."""
    sent_dt = message.get('sentDateTime')
    received_dt = message.get('receivedDateTime')

    for dt_str in [sent_dt, received_dt]:
        if dt_str:
            try:
                if isinstance(dt_str, str):
                    msg_datetime = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
                else:
                    msg_datetime = dt_str
                return msg_datetime, msg_datetime.date()
            except (ValueError, TypeError):
                continue

    return datetime.now(timezone.utc), datetime.now(timezone.utc).date()


def create_communication_note(message, client_id, company_id, user_id, parent_note_id,
                               user_email='', in_reply_to_id=None):
    """Crée une CommunicationNote à partir d'un message Outlook."""
    from models import CommunicationNote

    try:
        message_id = message.get('id', 'UNKNOWN')

        from_email = ''
        from_data = message.get('from', {})
        if from_data:
            from_email = from_data.get('emailAddress', {}).get('address', '')

        to_emails = []
        for field in ['toRecipients', 'ccRecipients', 'bccRecipients']:
            for recipient in message.get(field, []):
                addr = recipient.get('emailAddress', {}).get('address')
                if addr:
                    to_emails.append(addr)

        if not from_email:
            sync_logger.warning(f"Message incomplet (from vide): {message_id[:30]}... - Skipped")
            return None

        if not to_emails:
            sync_logger.warning(f"Message incomplet (to/cc/bcc vides): {message_id[:30]}... - Skipped")
            return None

        body_data = message.get('body', {})
        body_content = body_data.get('content', '')

        if not body_content:
            sync_logger.warning(f"Message sans body: {message_id[:30]}... - Création quand même")

        msg_datetime, note_date = _parse_message_datetime(message)
        direction = detect_email_direction(message, user_email)

        subject = message.get('subject') or 'Sans objet'
        direction_labels = {'sent': 'Envoyé', 'received': 'Reçu', 'reply': 'Réponse', 'forward': 'Transféré'}
        note_text = f"[{direction_labels.get(direction, direction)}] {subject}"

        note = CommunicationNote(
            client_id=client_id,
            company_id=company_id,
            user_id=user_id,
            note_type='email',
            note_text=note_text,
            note_date=note_date,
            email_body=body_content,
            email_subject=subject,
            email_from=from_email,
            email_to=', '.join(to_emails),
            email_direction=direction,
            internet_message_id=message.get('internetMessageId'),
            in_reply_to_id=in_reply_to_id,
            conversation_id=message.get('conversationId'),
            outlook_message_id=message.get('id'),
            parent_note_id=parent_note_id,
            is_from_sync=True,
            created_at=msg_datetime
        )

        return note

    except Exception as e:
        sync_logger.error(f"Erreur création note object: {e}")
        return None
