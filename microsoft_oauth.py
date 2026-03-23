import requests
import secrets
import base64
import os
from urllib.parse import urlencode
from flask import current_app, session, url_for
from constants import HTTP_TIMEOUT_DEFAULT, is_production

class MicrosoftOAuthConnector:
    def __init__(self):
        self.client_id = current_app.config.get('MICROSOFT_CLIENT_ID')
        self.client_secret = current_app.config.get('MICROSOFT_CLIENT_SECRET')
        self.tenant = current_app.config.get('MICROSOFT_TENANT', 'common')  # 'common' pour multitenant

        # Détection automatique de l'environnement et URL de redirection appropriée
        self.redirect_uri = self._get_redirect_uri()

        # URLs Microsoft
        self.auth_url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"
        self.token_url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
        self.graph_api_url = "https://graph.microsoft.com/v1.0"

    def _get_redirect_uri(self):
        """
        Déterminer l'URL de redirection OAuth appropriée selon l'environnement.

        En PRODUCTION (FLASK_ENV=production):
        - Utilise MICROSOFT_REDIRECT_URI configurée (domaine de production)

        En DEVELOPPEMENT (FLASK_ENV=development):
        - Utilise MICROSOFT_REDIRECT_URI_DEV si définie
        - Sinon, utilise MICROSOFT_REDIRECT_URI de la config

        Pour que cela fonctionne, l'application Azure doit avoir les URLs enregistrées:
        - Production: https://app.finov-relance.com/profile/microsoft/callback
        - Développement: http://localhost:5000/profile/microsoft/callback
        """
        if is_production():
            # En production, utiliser l'URL configurée ou générer dynamiquement
            configured_uri = current_app.config.get('MICROSOFT_REDIRECT_URI')
            if configured_uri:
                return configured_uri
            return url_for('oauth_callback.microsoft_callback', _external=True)
        else:
            # En développement, vérifier si une URL spécifique est configurée
            dev_uri = os.environ.get('MICROSOFT_REDIRECT_URI_DEV')
            if dev_uri:
                return dev_uri

            # Utiliser l'URL configurée (localhost par défaut en dev)
            configured_uri = current_app.config.get('MICROSOFT_REDIRECT_URI')
            if configured_uri:
                return configured_uri

            # Fallback: utiliser url_for
            return url_for('oauth_callback.microsoft_callback', _external=True)

    def get_current_environment_info(self):
        """Retourne des informations sur l'environnement actuel pour le débogage"""
        return {
            'is_production': is_production(),
            'redirect_uri': self.redirect_uri,
            'configured_prod_uri': current_app.config.get('MICROSOFT_REDIRECT_URI'),
            'configured_dev_uri': os.environ.get('MICROSOFT_REDIRECT_URI_DEV')
        }

    def get_authorization_url(self):
        """Générer l'URL d'autorisation OAuth2"""
        state = secrets.token_urlsafe(32)
        session['oauth_state'] = state

        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'scope': 'Mail.Send Mail.ReadWrite User.Read offline_access',
            'state': state,
            'response_mode': 'query',
            'prompt': 'select_account',  # Permet de réutiliser le consentement admin
            'domain_hint': 'organizations'  # Force le tenant organisationnel
        }

        return f"{self.auth_url}?{urlencode(params)}"

    def exchange_code_for_tokens(self, auth_code, state):
        """Échanger le code d'autorisation contre des tokens"""
        # Vérifier le state pour la sécurité CSRF
        stored_state = session.get('oauth_state')
        if stored_state is None:
            raise ValueError("État OAuth absent de la session - possible attaque CSRF")
        if state != stored_state:
            raise ValueError("État OAuth invalide")

        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': auth_code,
            'grant_type': 'authorization_code',
            'redirect_uri': self.redirect_uri,
            'scope': 'Mail.Send Mail.ReadWrite User.Read offline_access'
        }

        # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
        from utils.http_client import create_microsoft_session

        session_http = create_microsoft_session()
        response = session_http.post(self.token_url, data=data)

        return response.json()

    def refresh_access_token(self, refresh_token, max_retries=3):
        """ROBUSTESSE : Rafraîchir le token d'accès avec retry automatique"""
        import time
        from flask import current_app

        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
            'scope': 'Mail.Send Mail.ReadWrite User.Read offline_access'
        }

        # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
        from utils.http_client import create_microsoft_session

        last_error = None
        for attempt in range(max_retries):
            try:
                session_http = create_microsoft_session()
                response = session_http.post(self.token_url, data=data, timeout=HTTP_TIMEOUT_DEFAULT)

                if response.status_code == 200:
                    return response.json()
                elif response.status_code in [429, 503, 502, 504]:
                    # Erreurs temporaires - retry avec backoff exponentiel
                    wait_time = (2 ** attempt) + 1
                    current_app.logger.warning(f"Token refresh temporairement échoué (tentative {attempt + 1}/{max_retries}), retry dans {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    # Erreur permanente
                    current_app.logger.error(f"Token refresh échoué définitivement: {response.status_code} - {response.text}")
                    return response.json()

            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + 1
                    current_app.logger.warning(f"Erreur refresh token (tentative {attempt + 1}/{max_retries}): {str(e)}, retry dans {wait_time}s")
                    time.sleep(wait_time)
                else:
                    current_app.logger.error(f"Token refresh définitivement échoué après {max_retries} tentatives: {str(e)}")

        # Toutes les tentatives ont échoué
        raise Exception(f"Impossible de rafraîchir le token après {max_retries} tentatives. Dernière erreur: {str(last_error)}")

    def get_user_info(self, access_token):
        """Récupérer les informations utilisateur depuis Microsoft Graph API"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
        from utils.http_client import create_microsoft_session

        session_http = create_microsoft_session()
        response = session_http.get(f'{self.graph_api_url}/me', headers=headers)

        return response.json()

    def send_email(self, access_token, to_emails, subject, body, from_name=None, cc_list=None, attachments=None, return_message_ids=False, high_importance=False, read_receipt=False, delivery_receipt=False):
        """
        Envoyer un email via Microsoft Graph API with optional attachments

        Args:
            return_message_ids: Si True, retourne un dict avec success, message_id et conversation_id au lieu de juste True/False
            high_importance: Si True, marque l'email comme haute importance
            read_receipt: Si True, demande un accusé de lecture
            delivery_receipt: Si True, demande un accusé de remise

        Returns:
            Si return_message_ids=True: dict avec {'success': bool, 'message_id': str, 'conversation_id': str}
            Si return_message_ids=False: bool (compatibilité avec code existant)
        """
        from flask import current_app
        import re

        # SÉCURITÉ ÉTAPE 9 : Logging sécurisé avec masquage des emails
        from utils.secure_logging import sanitize_email_for_logs, create_secure_log_message

        safe_emails = [sanitize_email_for_logs(email) for email in (to_emails if isinstance(to_emails, list) else [to_emails])]
        secure_message = create_secure_log_message(
            "Microsoft send_email called",
            to_emails=safe_emails,
            subject_preview=subject[:50] if subject else "no_subject"
        )
        current_app.logger.info(secure_message)

        if not access_token:
            raise Exception("Token d'accès Microsoft manquant")

        # Normaliser le HTML pour un affichage cohérent (interligne simple style Outlook)
        if body:
            # Ajouter des styles inline aux paragraphes pour un rendu uniforme
            body = re.sub(
                r'<p(?![^>]*style=)([^>]*)>',
                r'<p style="margin: 0; line-height: 1.5;"\1>',
                body
            )
            # Gérer les paragraphes qui ont déjà un style mais pas de margin
            body = re.sub(
                r'<p([^>]*style="[^"]*")([^>]*)>',
                lambda m: m.group(0) if 'margin' in m.group(1) else f'<p{m.group(1)[:-1]}; margin: 0; line-height: 1.5;"{m.group(2)}>',
                body
            )
            current_app.logger.info(f"Email body size: {len(body)} bytes")

        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # Assurer que to_emails est une liste
        if isinstance(to_emails, str):
            to_emails = [to_emails]

        # Construire les destinataires principaux
        to_recipients = []
        for email in to_emails:
            to_recipients.append({
                "emailAddress": {
                    "address": email.strip()
                }
            })

        # Construire les destinataires en copie
        cc_recipients = []
        if cc_list:
            for email in cc_list:
                cc_recipients.append({
                    "emailAddress": {
                        "address": email.strip()
                    }
                })

        # Construire le message
        message = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": body
                },
                "toRecipients": to_recipients
            }
        }

        # Ajouter les destinataires en copie si présents
        if cc_recipients:
            message["message"]["ccRecipients"] = cc_recipients

        # Ajouter les options d'email
        if high_importance:
            message["message"]["importance"] = "high"

        if read_receipt:
            message["message"]["isReadReceiptRequested"] = True

        if delivery_receipt:
            message["message"]["isDeliveryReceiptRequested"] = True

        # Add attachments if provided
        if attachments:
            import base64
            message["message"]["attachments"] = []

            for attachment in attachments:
                # Encoder le contenu en base64 - SIMPLE AND WORKING VERSION
                content = attachment['content']

                # Gérer différents types de contenu (version simple qui fonctionnait)
                if hasattr(content, 'getvalue'):
                    # Si c'est un BytesIO, récupérer le contenu
                    content_bytes = content.getvalue()
                elif isinstance(content, bytes):
                    content_bytes = content
                elif isinstance(content, str):
                    content_bytes = content.encode('utf-8')
                else:
                    # Fallback pour d'autres types
                    content_bytes = bytes(content)

                encoded_content = base64.b64encode(content_bytes).decode('utf-8')

                attachment_data = {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment['filename'],
                    "contentType": attachment['content_type'],
                    "contentBytes": encoded_content
                }

                message["message"]["attachments"].append(attachment_data)

        try:
            # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
            from utils.http_client import create_microsoft_session
            session_http = create_microsoft_session()

            # Calculer la taille approximative du message
            import json
            message_size = len(json.dumps(message).encode('utf-8'))
            current_app.logger.info(f"Total message size: {message_size} bytes ({message_size/1024:.1f} KB)")

            # Si on a besoin des IDs, utiliser l'approche brouillon + envoi
            if return_message_ids:
                current_app.logger.info("Using draft + send approach to get message_id and conversation_id")

                # Étape 1 : Créer le brouillon
                draft_response = session_http.post(
                    f"{self.graph_api_url}/me/messages",
                    headers=headers,
                    json=message["message"]
                )

                if draft_response.status_code not in [200, 201]:
                    current_app.logger.error(f"Draft creation failed: {draft_response.text}")
                    if draft_response.status_code == 413:
                        raise Exception("La taille de l'email dépasse la limite autorisée.")
                    raise Exception(f"Erreur création brouillon: {draft_response.status_code}")

                draft_data = draft_response.json()
                message_id = draft_data.get('id')
                conversation_id = draft_data.get('conversationId')

                current_app.logger.info(f"Draft created with ID: {message_id}, ConversationID: {conversation_id}")

                # Étape 2 : Envoyer le brouillon
                send_response = session_http.post(
                    f"{self.graph_api_url}/me/messages/{message_id}/send",
                    headers=headers
                )

                if send_response.status_code != 202:
                    current_app.logger.error(f"Send draft failed: {send_response.text}")
                    raise Exception(f"Erreur envoi brouillon: {send_response.status_code}")

                current_app.logger.info("Email sent successfully via draft approach")
                return {
                    'success': True,
                    'message_id': message_id,
                    'conversation_id': conversation_id
                }

            # Sinon, utiliser l'approche classique (compatibilité avec code existant)
            else:
                current_app.logger.info(f"Sending email via Microsoft Graph API to: {self.graph_api_url}/me/sendMail")
                current_app.logger.info(f"Message structure: {len(to_recipients)} recipients, attachments: {len(attachments) if attachments else 0}")

                response = session_http.post(
                    f"{self.graph_api_url}/me/sendMail",
                    headers=headers,
                    json=message
                )

                if response.status_code != 202:
                    current_app.logger.error(f"Microsoft Graph error response: {response.text}")
                    if response.status_code == 413:  # Request Entity Too Large
                        raise Exception("La taille de l'email (signature + contenu + pièces jointes) dépasse la limite autorisée. Réduisez la taille de votre signature ou retirez des pièces jointes.")
                    raise Exception(f"Erreur envoi email: {response.status_code} - {response.text}")

                current_app.logger.info("Email sent successfully via Microsoft Graph")
                return True
        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Network error sending email: {str(e)}")
            if "413" in str(e) or "entity too large" in str(e).lower():
                raise Exception("La taille de l'email dépasse la limite autorisée. Réduisez la taille de votre signature ou retirez des pièces jointes.")
            raise Exception(f"Erreur réseau lors de l'envoi: {str(e)}")
        except Exception as e:
            current_app.logger.error(f"General error sending email: {str(e)}")
            if "413" in str(e) or "entity too large" in str(e).lower():
                raise Exception("La taille de l'email dépasse la limite autorisée. Réduisez la taille de votre signature ou retirez des pièces jointes.")
            raise

    def test_connection(self, access_token):
        """Tester la connexion avec Microsoft Graph"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
        from utils.http_client import create_microsoft_session

        session_http = create_microsoft_session()
        response = session_http.get(f"{self.graph_api_url}/me", headers=headers)
        return response.status_code == 200