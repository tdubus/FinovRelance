"""
OutlookEmailSyncService - Service de synchronisation des emails Outlook

Module pour le BOUTON MANUEL de synchronisation.
Scanne Inbox + SentItems avec déduplication par internet_message_id (RFC 2822).

ARCHITECTURE:
- sync_conversation_to_notes(): Sync messages d'une conversation existante
- sync_replies_with_changed_subject(): Découvre réponses avec sujet modifié
- Déduplication: internet_message_id est la clé unique (pas outlook_message_id)

CONTRAINTES:
- SEULEMENT rattacher aux conversations EXISTANTES provenant de notre app
- Pas de matching par email client
- Pas de création d'orphelins
- Respect de la vie privée: on ne sync QUE les réponses/transferts
  liés à des conversations initiées depuis notre app

CORRECTIONS v2 (18/02/2026):
- Déduplication unifiée au scope (company_id, client_id) — plus de doublons
  entre sync_conversation_to_notes et _sync_discovered_messages
- Résolution du parent: ne suppose plus que la racine est forcément "sent"
  → supporte les conversations initiées par réception
- Matching des notes sent à patcher: ajout critère date (±5min)
- Code de création de note factorisé dans _create_note_from_message()
- Commit par batch au lieu de commit unitaire par message
- Parsing MIME: suppression limite 100 lignes, utilisation lib email
- bare except remplacés par except (ValueError, TypeError)
"""

import logging
import re
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger(__name__)


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
        logger.warning(f"Erreur conversion HTML: {e}")
        return html_content


GRAPH_SELECT_FIELDS = 'id,subject,bodyPreview,body,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,conversationId,internetMessageId,isRead,hasAttachments,internetMessageHeaders'
GRAPH_SELECT_FIELDS_DISCOVERY = 'id,subject,from,receivedDateTime,sentDateTime,conversationId,internetMessageId'
IN_REPLY_TO_EXTENDED_PROPERTY = "String 0x1042"


class OutlookEmailSyncService:
    """Service pour synchroniser les emails depuis Outlook via Microsoft Graph API."""

    def __init__(self, access_token):
        self.access_token = access_token
        self.graph_api_url = "https://graph.microsoft.com/v1.0"

    def _get_headers(self):
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }

    # ──────────────────────────────────────────────
    # GRAPH API — Récupération de messages
    # ──────────────────────────────────────────────

    def get_message_by_id(self, message_id):
        """Récupère un message complet par son ID."""
        import requests as req

        try:
            url = f"{self.graph_api_url}/me/messages/{message_id}"
            params = {
                '$select': GRAPH_SELECT_FIELDS,
                '$expand': 'attachments($select=id,name,size,contentType)'
            }

            response = req.get(url,
                               headers=self._get_headers(),
                               params=params,
                               timeout=15)

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(
                    f"Erreur récupération message {message_id[:20]}...: {response.status_code}"
                )
                return None

        except Exception as e:
            logger.error(f"Exception récupération message: {str(e)}")
            return None

    def get_messages_by_conversation_id(self, conversation_id, limit=50):
        """Récupère tous les messages d'une conversation Outlook."""
        import requests as req

        debug_info = {
            'method_used': 'odata_filter',
            'graph_url': None,
            'error_detail': None
        }

        try:
            escaped_conv_id = conversation_id.replace("'", "''")

            url = f"{self.graph_api_url}/me/messages"
            params = {
                '$filter': f"conversationId eq '{escaped_conv_id}'",
                '$top': limit,
                '$select': GRAPH_SELECT_FIELDS,
                '$expand': 'attachments($select=id,name,size,contentType)'
            }

            debug_info['graph_url'] = f"{url}?$filter=conversationId eq '...'"

            response = req.get(url,
                               headers=self._get_headers(),
                               params=params,
                               timeout=15)
            debug_info['http_status'] = response.status_code

            if response.status_code == 200:
                data = response.json()
                messages = data.get('value', [])
                messages.sort(key=lambda m: m.get('receivedDateTime', ''),
                              reverse=True)
                return messages, debug_info
            elif response.status_code == 400:
                return self._get_messages_fallback(conversation_id, limit,
                                                   debug_info)
            else:
                debug_info['error_detail'] = f"{response.status_code}"
                return [], debug_info

        except Exception as e:
            debug_info['error_detail'] = str(e)
            return self._get_messages_fallback(conversation_id, limit,
                                               debug_info)

    def _get_messages_fallback(self, conversation_id, limit, debug_info):
        """Fallback: filtre local si OData échoue. Pagine pour couvrir plus de messages."""
        import requests as req

        debug_info['method_used'] = 'fallback_local_filter'

        try:
            all_matched = []
            url = f"{self.graph_api_url}/me/messages"
            params = {
                '$top': 200,
                '$orderby': 'receivedDateTime desc',
                '$select': GRAPH_SELECT_FIELDS,
                '$expand': 'attachments($select=id,name,size,contentType)'
            }

            # Paginer jusqu'à 3 pages (600 messages max) au lieu de charger 500 d'un coup
            for _page in range(3):
                response = req.get(url,
                                   headers=self._get_headers(),
                                   params=params,
                                   timeout=20)

                if response.status_code != 200:
                    break

                data = response.json()
                page_messages = data.get('value', [])
                all_matched.extend(
                    m for m in page_messages
                    if m.get('conversationId') == conversation_id)

                # Arrêter si on a assez ou s'il n'y a plus de pages
                if len(all_matched) >= limit:
                    break
                next_link = data.get('@odata.nextLink')
                if not next_link:
                    break
                url = next_link
                params = None  # nextLink contient déjà les params

            return all_matched[:limit], debug_info

        except Exception as e:
            debug_info['fallback_exception'] = str(e)
            return [], debug_info

    # ──────────────────────────────────────────────
    # EXTRACTION HEADERS RFC 2822
    # ──────────────────────────────────────────────

    def extract_in_reply_to_from_extended_property(self, message):
        """Extrait In-Reply-To depuis Extended Property 0x1042."""
        extended_props = message.get('singleValueExtendedProperties', [])
        if not extended_props:
            return None

        for prop in extended_props:
            prop_id = prop.get('id', '')
            if '0x1042' in prop_id.lower():
                return prop.get('value')
        return None

    def extract_rfc2822_headers(self, message, use_mime_fallback=False):
        """Extrait Message-ID et In-Reply-To d'un message."""
        result = {'message_id': None, 'in_reply_to': None}

        result['message_id'] = message.get('internetMessageId')

        in_reply_to = self.extract_in_reply_to_from_extended_property(message)
        if in_reply_to:
            result['in_reply_to'] = in_reply_to
        else:
            headers = message.get('internetMessageHeaders', [])
            for header in headers:
                if header.get('name', '').lower() == 'in-reply-to':
                    result['in_reply_to'] = header.get('value', '')
                    break

            if not result['in_reply_to'] and use_mime_fallback:
                message_id = message.get('id')
                if message_id:
                    mime_headers = self.get_message_mime_headers(message_id)
                    if mime_headers.get('in_reply_to'):
                        result['in_reply_to'] = mime_headers['in_reply_to']

        return result

    def get_message_mime_headers(self, message_id):
        """Récupère In-Reply-To via MIME content ($value endpoint).

        Utilise le module email de Python pour un parsing fiable
        au lieu de scanner manuellement les N premières lignes.
        """
        import requests as req

        headers = {'Authorization': f'Bearer {self.access_token}'}
        url = f"{self.graph_api_url}/me/messages/{message_id}/$value"

        try:
            response = req.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return {}

            result = {}

            try:
                import email
                from email.policy import default as default_policy
                msg = email.message_from_string(response.text,
                                                policy=default_policy)

                in_reply_to = msg.get('In-Reply-To', '')
                if in_reply_to:
                    result['in_reply_to'] = in_reply_to.strip()

                msg_id = msg.get('Message-ID', '')
                if msg_id:
                    result['message_id'] = msg_id.strip()
            except Exception as e_email:
                # Fallback: parsing manuel si le module email échoue
                logger.debug(
                    f"email parser fallback for {message_id[:20]}: {e_email}")
                for line in response.text.split('\n'):
                    line_stripped = line.strip()
                    lower_line = line_stripped.lower()

                    if lower_line.startswith('in-reply-to:'):
                        result['in_reply_to'] = line_stripped[12:].strip()
                    elif lower_line.startswith('message-id:'):
                        result['message_id'] = line_stripped[11:].strip()

                    # Ligne vide = fin des headers MIME
                    if line_stripped == '':
                        break

            return result

        except Exception as e:
            logger.debug(f"MIME fallback error: {e}")
            return {}

    # ──────────────────────────────────────────────
    # RÉSOLUTION DU PARENT & DIRECTION
    # ──────────────────────────────────────────────

    def find_parent_note_by_in_reply_to(self,
                                        in_reply_to_id,
                                        company_id,
                                        db,
                                        CommunicationNote,
                                        client_id=None):
        """Trouve la note parente via In-Reply-To."""
        if not in_reply_to_id:
            return None

        query = db.session.query(CommunicationNote).filter(
            CommunicationNote.internet_message_id == in_reply_to_id,
            CommunicationNote.company_id == company_id)

        if client_id is not None:
            query = query.filter(CommunicationNote.client_id == client_id)

        return query.first()

    def _resolve_parent_note_id(self,
                                message,
                                conversation_id,
                                client_id,
                                company_id,
                                db,
                                CommunicationNote,
                                rfc_headers=None):
        """Résolution unifiée du parent pour les deux chemins de sync.

        Stratégie en 2 étapes:
        1. In-Reply-To header → parent direct (le plus fiable)
        2. Racine de la conversation par conversation_id
           (premier message chronologique, sent OU received)

        Returns:
            tuple: (parent_note_id, in_reply_to_id)
        """
        if rfc_headers is None:
            rfc_headers = self.extract_rfc2822_headers(message,
                                                       use_mime_fallback=True)

        in_reply_to_id = rfc_headers.get('in_reply_to')

        # Étape 1: In-Reply-To → lien direct vers le parent
        if in_reply_to_id:
            parent = self.find_parent_note_by_in_reply_to(in_reply_to_id,
                                                          company_id,
                                                          db,
                                                          CommunicationNote,
                                                          client_id=client_id)
            if parent:
                return parent.id, in_reply_to_id

        # Étape 2: racine de la conversation (premier message, toute direction)
        root_note = db.session.query(CommunicationNote).filter(
            CommunicationNote.conversation_id == conversation_id,
            CommunicationNote.company_id == company_id,
            CommunicationNote.client_id == client_id,
            CommunicationNote.parent_note_id.is_(None)).order_by(
                CommunicationNote.created_at.asc()).first()

        if root_note:
            return root_note.id, in_reply_to_id

        return None, in_reply_to_id

    def detect_email_direction(self, message, user_email):
        """Détecte la direction d'un email."""
        subject = message.get('subject') or ''
        from_address = message.get('from', {}).get('emailAddress',
                                                   {}).get('address',
                                                           '').lower()
        user_email_lower = user_email.lower() if user_email else ''

        is_from_user = from_address == user_email_lower
        subject_lower = subject.lower()

        is_reply = subject_lower.startswith('re:') or subject_lower.startswith(
            're :')
        is_forward = any(
            subject_lower.startswith(p)
            for p in ['fw:', 'fwd:', 'tr:', 'tr :'])

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

    # ──────────────────────────────────────────────
    # DÉDUPLICATION UNIFIÉE
    # ──────────────────────────────────────────────

    def _load_existing_ids(self,
                           company_id,
                           client_id,
                           db,
                           CommunicationNote,
                           conversation_id=None):
        """Charge les IDs existants pour la déduplication.

        Scope TOUJOURS au niveau (company_id, client_id) pour éviter
        les doublons entre les deux chemins de sync.
        Le conversation_id est optionnel et sert uniquement à réduire la requête.
        """
        base_filter = [
            CommunicationNote.company_id == company_id,
            CommunicationNote.client_id == client_id
        ]

        # internet_message_ids — scope global (company + client)
        imid_query = db.session.query(
            CommunicationNote.internet_message_id).filter(
                *base_filter,
                CommunicationNote.internet_message_id.isnot(None))
        existing_internet_message_ids = {
            r[0]
            for r in imid_query.all() if r[0]
        }

        # outlook_message_ids — scope global (company + client)
        omid_query = db.session.query(
            CommunicationNote.outlook_message_id).filter(
                *base_filter, CommunicationNote.outlook_message_id.isnot(None))
        existing_outlook_ids = {r[0] for r in omid_query.all() if r[0]}

        return existing_internet_message_ids, existing_outlook_ids

    def _is_duplicate(self, internet_msg_id, outlook_msg_id,
                      existing_internet_message_ids, existing_outlook_ids):
        """Vérifie si un message existe déjà. Logique unifiée."""
        if internet_msg_id and internet_msg_id in existing_internet_message_ids:
            return True
        if outlook_msg_id and outlook_msg_id in existing_outlook_ids:
            return True
        return False

    def _try_patch_existing_sent_note(self, conversation_id, client_id,
                                      company_id, subject, internet_message_id,
                                      outlook_msg_id, in_reply_to_id,
                                      msg_datetime, db, CommunicationNote):
        """Tente de patcher une note sent existante sans internet_message_id.

        Matching renforcé: conversation_id + direction + subject + date (±5min)
        pour éviter l'ambiguïté quand il y a plusieurs réponses avec le même sujet.

        Returns:
            True si une note a été patchée, False sinon.
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
            CommunicationNote.internet_message_id.is_(None))

        # Ajout du critère date si disponible pour lever l'ambiguïté
        if msg_datetime:
            window_start = msg_datetime - timedelta(minutes=5)
            window_end = msg_datetime + timedelta(minutes=5)
            query = query.filter(
                CommunicationNote.created_at.between(window_start, window_end))

        existing_sent_note = query.first()

        if existing_sent_note:
            existing_sent_note.internet_message_id = internet_message_id
            existing_sent_note.outlook_message_id = outlook_msg_id
            if in_reply_to_id:
                existing_sent_note.in_reply_to_id = in_reply_to_id
            logger.info(
                f"Mise à jour note #{existing_sent_note.id} avec internet_message_id"
            )
            return True

        return False

    # ──────────────────────────────────────────────
    # CRÉATION DE NOTE — Logique factorisée
    # ──────────────────────────────────────────────

    def _parse_message_datetime(self, message):
        """Parse la date d'un message Graph API. Retourne (msg_datetime, note_date)."""
        sent_dt = message.get('sentDateTime')
        received_dt = message.get('receivedDateTime')

        for dt_str in [sent_dt, received_dt]:
            if dt_str:
                try:
                    msg_datetime = datetime.fromisoformat(
                        dt_str.replace('Z', '+00:00'))
                    return msg_datetime, msg_datetime.date()
                except (ValueError, TypeError):
                    logger.debug(f"Date parsing failed for: {dt_str}")
                    continue

        return datetime.now(timezone.utc), date.today()

    def _extract_email_addresses(self, message):
        """Extrait from_email et to_emails d'un message Graph API.

        Returns:
            tuple: (from_email, to_emails_string) ou (None, None) si incomplet
        """
        from_info = message.get('from', {}).get('emailAddress', {})
        from_email = from_info.get('address', '')

        all_recipients = []
        for field in ['toRecipients', 'ccRecipients', 'bccRecipients']:
            for r in message.get(field, []):
                addr = r.get('emailAddress', {}).get('address', '')
                if addr:
                    all_recipients.append(addr)

        if not from_email or not all_recipients:
            return None, None

        return from_email, ', '.join(all_recipients)

    def _create_note_from_message(self,
                                  message,
                                  client_id,
                                  company_id,
                                  user_id,
                                  user_email,
                                  parent_note_id,
                                  db,
                                  CommunicationNote,
                                  internet_message_id=None,
                                  in_reply_to_id=None):
        """Crée une CommunicationNote à partir d'un message Graph API.

        Logique factorisée utilisée par les deux chemins de sync.
        NE fait PAS de commit (le caller gère le batch).

        Returns:
            CommunicationNote si créée, None si message incomplet.
        """
        outlook_msg_id = message.get('id')
        direction = self.detect_email_direction(message, user_email)

        from_email, to_emails = self._extract_email_addresses(message)
        if not from_email or not to_emails:
            logger.warning(
                f"Message {(outlook_msg_id or '')[:30]} incomplet (from/to vide) - Skipped"
            )
            return None

        msg_datetime, note_date = self._parse_message_datetime(message)

        body = message.get('body', {})
        body_content = body.get('content', '') if body else ''

        subject = message.get('subject') or 'Sans objet'
        direction_labels = {
            'sent': 'Envoyé',
            'received': 'Reçu',
            'reply': 'Réponse',
            'forward': 'Transféré'
        }
        note_text = f"[{direction_labels.get(direction, direction)}] {subject}"

        new_note = CommunicationNote(
            client_id=client_id,
            user_id=user_id,
            company_id=company_id,
            note_text=note_text,
            note_type='email',
            note_date=note_date,
            email_from=from_email,
            email_to=to_emails,
            email_subject=subject,
            email_body=body_content,
            outlook_message_id=outlook_msg_id,
            conversation_id=message.get('conversationId'),
            internet_message_id=internet_message_id,
            in_reply_to_id=in_reply_to_id,
            email_direction=direction,
            parent_note_id=parent_note_id,
            is_from_sync=True,
            created_at=msg_datetime)

        db.session.add(new_note)
        return new_note

    # ──────────────────────────────────────────────
    # DISCOVERY — Réponses avec sujet modifié
    # ──────────────────────────────────────────────

    def discover_replies_with_changed_subject(self,
                                              known_message_ids,
                                              company_id,
                                              limit=100,
                                              days_back=7):
        """
        Découvre les réponses avec sujet modifié.
        Scanne INBOX et SENTITEMS avec déduplication par internet_message_id.
        Pagine pour ne pas rater de messages.
        """
        import requests as req

        if not known_message_ids:
            return []

        discovered_messages = []
        seen_ids = set()

        date_filter = (
            datetime.now(timezone.utc) -
            timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
        expand_value = f"singleValueExtendedProperties($filter=id eq '{IN_REPLY_TO_EXTENDED_PROPERTY}')"

        folders = [('inbox', f"{self.graph_api_url}/me/messages",
                    'receivedDateTime'),
                   ('sentItems',
                    f"{self.graph_api_url}/me/mailFolders/sentItems/messages",
                    'sentDateTime')]

        for folder_name, url, date_field in folders:
            try:
                params = {
                    '$top': limit,
                    '$orderby': f'{date_field} desc',
                    '$select': GRAPH_SELECT_FIELDS_DISCOVERY,
                    '$filter': f"{date_field} ge {date_filter}",
                    '$expand': expand_value
                }

                # Paginer jusqu'à 3 pages par dossier
                for _page in range(3):
                    response = req.get(url,
                                       headers=self._get_headers(),
                                       params=params,
                                       timeout=20)

                    if response.status_code != 200:
                        logger.warning(
                            f"Erreur découverte ({folder_name}): {response.status_code}"
                        )
                        break

                    data = response.json()
                    messages = data.get('value', [])
                    mime_candidates = []

                    for message in messages:
                        msg_id = message.get('id')
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)

                        in_reply_to = self.extract_in_reply_to_from_extended_property(
                            message)

                        if not in_reply_to:
                            headers = self.extract_rfc2822_headers(message)
                            in_reply_to = headers.get('in_reply_to')

                        if not in_reply_to and not message.get(
                                'internetMessageHeaders'):
                            if len(mime_candidates) < 10:
                                mime_candidates.append(message)
                            continue

                        if in_reply_to and in_reply_to in known_message_ids:
                            discovered_messages.append(message)
                            message['_extracted_in_reply_to'] = in_reply_to

                    for message in mime_candidates:
                        mime_headers = self.get_message_mime_headers(
                            message.get('id'))
                        in_reply_to = mime_headers.get('in_reply_to')

                        if in_reply_to and in_reply_to in known_message_ids:
                            discovered_messages.append(message)
                            message['_extracted_in_reply_to'] = in_reply_to

                    # Page suivante?
                    next_link = data.get('@odata.nextLink')
                    if not next_link:
                        break
                    url = next_link
                    params = None  # nextLink contient déjà les params

            except Exception as e:
                logger.error(f"Exception découverte ({folder_name}): {str(e)}")

        logger.info(
            f"Découverte Inbox+SentItems: {len(discovered_messages)} réponses ({days_back}j)"
        )
        return discovered_messages

    # ──────────────────────────────────────────────
    # SYNC PRINCIPAL — Conversation existante
    # ──────────────────────────────────────────────

    def sync_conversation_to_notes(self, conversation_id, client_id,
                                   company_id, user_id, user_email):
        """Synchronise une conversation vers les notes de communication.

        Ne crée des notes QUE pour les messages liés à une conversation
        déjà existante dans notre app (initiée par un envoi depuis l'app).
        """
        from app import db
        from models import CommunicationNote

        stats = {
            'new_notes': 0,
            'skipped': 0,
            'errors': 0,
            'messages_found': 0
        }

        messages, debug_info = self.get_messages_by_conversation_id(
            conversation_id)
        stats['messages_found'] = len(messages)
        stats.update(debug_info)

        if not messages:
            return stats

        # Déduplication — scope global (company + client), PAS par conversation
        existing_internet_message_ids, existing_outlook_ids = self._load_existing_ids(
            company_id, client_id, db, CommunicationNote)

        notes_to_flush = []

        for message in messages:
            outlook_msg_id = message.get('id')
            internet_msg_id = message.get('internetMessageId')

            # Déduplication unifiée
            if self._is_duplicate(internet_msg_id, outlook_msg_id,
                                  existing_internet_message_ids,
                                  existing_outlook_ids):
                stats['skipped'] += 1
                continue

            try:
                with db.session.begin_nested():
                    direction = self.detect_email_direction(message, user_email)
                    rfc_headers = self.extract_rfc2822_headers(
                        message, use_mime_fallback=True)
                    internet_message_id = message.get(
                        'internetMessageId') or rfc_headers.get('message_id')
                    in_reply_to_id = rfc_headers.get('in_reply_to')

                    # Re-check avec l'internet_message_id extrait des headers
                    if internet_message_id and internet_message_id in existing_internet_message_ids:
                        stats['skipped'] += 1
                        continue

                    # Résolution du parent — logique unifiée
                    parent_note_id, in_reply_to_id = self._resolve_parent_note_id(
                        message,
                        conversation_id,
                        client_id,
                        company_id,
                        db,
                        CommunicationNote,
                        rfc_headers=rfc_headers)

                    subject = message.get('subject') or 'Sans objet'
                    msg_datetime, _ = self._parse_message_datetime(message)

                    # Tenter de patcher une note sent existante sans internet_message_id
                    if direction == 'sent':
                        patched = self._try_patch_existing_sent_note(
                            conversation_id, client_id, company_id, subject,
                            internet_message_id, outlook_msg_id, in_reply_to_id,
                            msg_datetime, db, CommunicationNote)
                        if patched:
                            if outlook_msg_id:
                                existing_outlook_ids.add(outlook_msg_id)
                            if internet_message_id:
                                existing_internet_message_ids.add(
                                    internet_message_id)
                            stats['skipped'] += 1
                            continue

                        # Pas de parent → pas de note orpheline pour les sent
                        if not parent_note_id:
                            stats['skipped'] += 1
                            continue

                    # Pas de parent du tout → skip (pas d'orphelins)
                    if not parent_note_id:
                        stats['skipped'] += 1
                        continue

                    new_note = self._create_note_from_message(
                        message,
                        client_id,
                        company_id,
                        user_id,
                        user_email,
                        parent_note_id,
                        db,
                        CommunicationNote,
                        internet_message_id=internet_message_id,
                        in_reply_to_id=in_reply_to_id)

                    if new_note:
                        notes_to_flush.append(new_note)
                        if outlook_msg_id:
                            existing_outlook_ids.add(outlook_msg_id)
                        if internet_message_id:
                            existing_internet_message_ids.add(internet_message_id)
                        stats['new_notes'] += 1
                    else:
                        stats['skipped'] += 1

            except Exception as e:
                logger.error(f"Erreur sync message (savepoint rollback): {str(e)}")
                stats['errors'] += 1

        # Commit par batch — tout ou rien pour cette conversation
        if notes_to_flush:
            try:
                db.session.commit()
                logger.info(
                    f"Batch commit: {len(notes_to_flush)} notes pour conversation {conversation_id[:20]}..."
                )
            except Exception as e:
                db.session.rollback()
                logger.error(f"Erreur batch commit conversation: {str(e)}")
                stats['errors'] += stats['new_notes']
                stats['new_notes'] = 0

        return stats

    # ──────────────────────────────────────────────
    # SYNC DISCOVERY — Réponses sujet modifié
    # ──────────────────────────────────────────────

    def sync_replies_with_changed_subject(self,
                                          client_id,
                                          company_id,
                                          user_id,
                                          user_email,
                                          max_passes=3,
                                          known_ids_cache=None):
        """Synchronise les réponses dont le sujet a été modifié."""
        from app import db
        from models import CommunicationNote

        stats = {'discovered': 0, 'synced': 0, 'skipped': 0, 'errors': 0}
        pass_count = 0

        known_internet_ids = set(
            known_ids_cache.get('internet_message_ids',
                                set())) if known_ids_cache else set()

        while pass_count < max_passes:
            pass_count += 1

            if pass_count > 1 or not known_ids_cache:
                known_ids_query = db.session.query(
                    CommunicationNote.internet_message_id).filter(
                        CommunicationNote.company_id == company_id,
                        CommunicationNote.client_id == client_id,
                        CommunicationNote.internet_message_id.isnot(
                            None)).all()
                known_internet_ids = {
                    row[0]
                    for row in known_ids_query if row[0]
                }

            if not known_internet_ids:
                break

            discovered_messages = self.discover_replies_with_changed_subject(
                known_internet_ids, company_id)

            existing_imids = set()
            all_imids = [
                m.get('internetMessageId') for m in discovered_messages
                if m.get('internetMessageId')
            ]
            if all_imids:
                existing = db.session.query(
                    CommunicationNote.internet_message_id).filter(
                        CommunicationNote.company_id == company_id,
                        CommunicationNote.internet_message_id.in_(
                            all_imids)).all()
                existing_imids = {r[0] for r in existing if r[0]}

            new_messages = [
                m for m in discovered_messages
                if m.get('internetMessageId') not in existing_imids
            ]

            if not new_messages:
                break

            stats['discovered'] += len(new_messages)
            synced = self._sync_discovered_messages(new_messages, client_id,
                                                    company_id, user_id,
                                                    user_email, db,
                                                    CommunicationNote)

            stats['synced'] += synced['synced']
            stats['skipped'] += synced['skipped']
            stats['errors'] += synced['errors']

            if synced['synced'] == 0:
                break

        return stats

    def _sync_discovered_messages(self, messages, client_id, company_id,
                                  user_id, user_email, db, CommunicationNote):
        """Synchronise une liste de messages découverts."""
        stats = {'synced': 0, 'skipped': 0, 'errors': 0}

        # Déduplication — même scope global que sync_conversation_to_notes
        existing_internet_message_ids, existing_outlook_ids = self._load_existing_ids(
            company_id, client_id, db, CommunicationNote)

        notes_to_flush = []

        for discovered_msg in messages:
            try:
                in_reply_to_id = discovered_msg.get('_extracted_in_reply_to')
                internet_message_id = discovered_msg.get('internetMessageId')

                if not in_reply_to_id:
                    rfc_headers = self.extract_rfc2822_headers(
                        discovered_msg, use_mime_fallback=True)
                    internet_message_id = internet_message_id or rfc_headers.get(
                        'message_id')
                    in_reply_to_id = rfc_headers.get('in_reply_to')

                # Résolution du parent via In-Reply-To (logique unifiée)
                parent_note = self.find_parent_note_by_in_reply_to(
                    in_reply_to_id,
                    company_id,
                    db,
                    CommunicationNote,
                    client_id=client_id)

                if not parent_note:
                    stats['skipped'] += 1
                    continue

                # Récupérer le message complet
                message = self.get_message_by_id(discovered_msg.get('id'))
                if not message:
                    stats['errors'] += 1
                    continue

                internet_message_id = message.get(
                    'internetMessageId') or internet_message_id
                if not internet_message_id:
                    rfc_headers = self.extract_rfc2822_headers(
                        message, use_mime_fallback=True)
                    internet_message_id = rfc_headers.get('message_id')

                outlook_msg_id = message.get('id')

                # Déduplication unifiée
                if self._is_duplicate(internet_message_id, outlook_msg_id,
                                      existing_internet_message_ids,
                                      existing_outlook_ids):
                    stats['skipped'] += 1
                    continue

                with db.session.begin_nested():
                    subject = message.get('subject') or 'Sans objet'
                    direction = self.detect_email_direction(message, user_email)
                    msg_datetime, _ = self._parse_message_datetime(message)
                    conversation_id = message.get('conversationId')

                    # Tenter le patch d'une note sent existante
                    if direction == 'sent':
                        patched = self._try_patch_existing_sent_note(
                            conversation_id, client_id, company_id, subject,
                            internet_message_id, outlook_msg_id, in_reply_to_id,
                            msg_datetime, db, CommunicationNote)
                        if patched:
                            if outlook_msg_id:
                                existing_outlook_ids.add(outlook_msg_id)
                            if internet_message_id:
                                existing_internet_message_ids.add(
                                    internet_message_id)
                            stats['skipped'] += 1
                            continue

                    new_note = self._create_note_from_message(
                        message,
                        client_id,
                        company_id,
                        user_id,
                        user_email,
                        parent_note.id,
                        db,
                        CommunicationNote,
                        internet_message_id=internet_message_id,
                        in_reply_to_id=in_reply_to_id)

                    if new_note:
                        notes_to_flush.append(new_note)
                        if outlook_msg_id:
                            existing_outlook_ids.add(outlook_msg_id)
                        if internet_message_id:
                            existing_internet_message_ids.add(internet_message_id)
                        stats['synced'] += 1
                    else:
                        stats['skipped'] += 1

            except Exception as e:
                logger.error(f"Erreur sync discovered (savepoint rollback): {str(e)}")
                stats['errors'] += 1

        # Commit par batch
        if notes_to_flush:
            try:
                db.session.commit()
                logger.info(
                    f"Batch commit discovered: {len(notes_to_flush)} notes")
            except Exception as e:
                db.session.rollback()
                logger.error(f"Erreur batch commit discovered: {str(e)}")
                stats['errors'] += stats['synced']
                stats['synced'] = 0

        return stats


def get_sync_service_for_user(user_id, company_id):
    """Crée un OutlookEmailSyncService pour un utilisateur."""
    from models import EmailConfiguration
    from email_fallback import refresh_user_oauth_token

    email_config = EmailConfiguration.query.filter_by(
        user_id=user_id, company_id=company_id).first()

    if not email_config or not email_config.is_outlook_connected():
        return None, None

    if email_config.needs_token_refresh():
        try:
            refresh_user_oauth_token(email_config)
        except Exception as e:
            logger.error(f"Impossible de rafraîchir le token: {str(e)}")
            return None, None

    access_token = email_config.outlook_oauth_access_token
    if not access_token:
        return None, None

    return OutlookEmailSyncService(access_token), email_config.outlook_email
