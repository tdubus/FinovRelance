"""
Email system using Microsoft Graph API exclusively
No SMTP - Graph API only for all email communications

⚠️ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔒 ATTENTION - CODE EN PRODUCTION COMMERCIALE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ INTERDICTION ABSOLUE DE MODIFIER CE FICHIER SANS AUTORISATION EXPLICITE

FinovRelance est un produit commercialisé avec des clients payants.
Ce système d'envoi d'emails gère les communications critiques (2FA, notifications,
licences) via Microsoft Graph API pour des clients réels.

📋 RÈGLES STRICTES:
- AUCUNE modification de la logique Microsoft Graph API
- AUCUNE modification du système de rafraîchissement OAuth
- AUCUNE modification des templates d'emails (2FA, licences, etc.)
- Toute demande doit être documentée et approuvée formellement
- En cas de bug: proposer diagnostic SANS toucher au code

🚨 CONSÉQUENCES D'UNE MODIFICATION NON AUTORISÉE:
- Interruption des communications client (2FA, sécurité)
- Blocage de l'authentification utilisateur
- Violation de sécurité et conformité
- Perte de confiance client
- Responsabilité légale et financière

✅ ACTIONS AUTORISÉES: Lecture, diagnostic, analyse, documentation uniquement
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
from flask import current_app
from datetime import datetime, timedelta
from constants import HTTP_TIMEOUT_DEFAULT


def send_email_via_system_config(to_email, subject, html_content):
    """Envoyer un email via Microsoft Graph API (support@finov-relance.com)"""
    return send_2fa_code_via_smtp(to_email, subject, html_content, config_name='password_reset')

def send_2fa_code_via_smtp(to_email, user_name_or_subject, code_or_content, ip_address=None, config_name='password_reset'):
    """Envoyer email via Microsoft Graph API - SMTP supprime"""

    current_app.logger.info(f"Attempting to send 2FA code to {to_email} via Microsoft Graph API")

    max_retries = 2  # Défini en amont pour éviter UnboundLocalError dans le except

    try:
        # Get system email configuration
        from models import SystemEmailConfiguration
        system_config = SystemEmailConfiguration.query.filter_by(config_name=config_name).first()

        if not system_config:
            raise Exception(f"System email configuration '{config_name}' not found")

        # Check if token needs refresh (5 minutes before expiry)
        if system_config.needs_token_refresh():
            refresh_system_oauth_token(system_config)

        # Check if we have a valid access token
        if not system_config.outlook_oauth_access_token:
            raise Exception("No access token available for system email")

        # Adapter selon si c'est un 2FA ou email personnalisé
        if config_name == 'password_reset' and len(str(code_or_content)) > 6:
            # Email personnalisé (licence, etc.)
            subject = user_name_or_subject
            html_body = code_or_content
        else:
            # Code 2FA
            user_name = user_name_or_subject
            code = code_or_content
            subject = "FinovRelance - Code de vérification"
            location_info = f"Adresse IP: {ip_address}" if ip_address else "Adresse IP non disponible"

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px; text-align: center;">
                    <h2 style="color: #8475EC; margin-bottom: 20px;">Code de vérification</h2>

                    <p style="font-size: 16px; color: #333; margin-bottom: 25px;">
                        Bonjour {user_name},
                    </p>

                    <p style="font-size: 14px; color: #666; margin-bottom: 25px;">
                        Voici votre code de vérification pour accéder à votre compte FinovRelance:
                    </p>

                <div style="background-color: #ffffff; padding: 20px; border-radius: 8px; margin: 25px 0; border: 2px solid #8475EC;">
                    <h1 style="color: #8475EC; font-size: 32px; margin: 0; letter-spacing: 5px;">{code}</h1>
                </div>

                <div style="background-color: #fff3cd; padding: 15px; border-radius: 8px; margin: 20px 0; border-left: 4px solid #ffc107;">
                    <p style="font-size: 12px; color: #856404; margin: 0;">
                        <strong>Informations de sécurité:</strong><br>
                        {location_info}<br>
                        Si vous n'avez pas demandé ce code, ignorez cet email.
                    </p>
                </div>

                <p style="font-size: 12px; color: #666; margin-top: 30px;">
                    Équipe FinovRelance<br>
                    <a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}" style="color: #8475EC;">{os.environ.get('APP_URL', 'https://app.finov-relance.com').replace('https://', '')}</a>
                </p>
            </div>
        </body>
        </html>
        """

        # Send via Microsoft Graph API
        # ROBUSTESSE: Retry automatique si erreur 401 (token expiré)
        # Correctif pour le problème: cron s'arrête le soir, tokens expirés le matin
        import requests

        last_error = None

        for attempt in range(max_retries):
            try:
                current_app.logger.info(f"System email send attempt {attempt + 1}/{max_retries} to {to_email}")

                headers = {
                    'Authorization': f'Bearer {system_config.outlook_oauth_access_token}',
                    'Content-Type': 'application/json'
                }

                email_data = {
                    'message': {
                        'subject': subject,
                        'body': {
                            'contentType': 'HTML',
                            'content': html_body
                        },
                        'toRecipients': [
                            {
                                'emailAddress': {
                                    'address': to_email
                                }
                            }
                        ]
                    }
                }

                response = requests.post(
                    'https://graph.microsoft.com/v1.0/me/sendMail',
                    headers=headers,
                    json=email_data,
                    timeout=HTTP_TIMEOUT_DEFAULT
                )

                if response.status_code == 202:
                    current_app.logger.info(f"2FA code sent successfully via Graph API to {to_email}")
                    return True
                else:
                    error_msg = f"Graph API failed with status {response.status_code}: {response.text}"
                    current_app.logger.error(error_msg)

                    # Check if error is 401 (unauthorized/token expired)
                    if response.status_code == 401 and attempt < max_retries - 1:
                        try:
                            refresh_system_oauth_token(system_config)
                            # Continue to next iteration to retry
                        except Exception as refresh_error:
                            current_app.logger.error(f"System token refresh failed: {refresh_error}")
                            raise Exception(f"Échec du refresh du token système: {refresh_error}")
                    else:
                        # Not a 401, or no retries left
                        raise Exception(error_msg)

            except requests.exceptions.RequestException as req_error:
                last_error = req_error
                current_app.logger.warning(f"Request error (attempt {attempt + 1}/{max_retries}): {req_error}")
                # Ne pas réessayer sur erreur réseau: le serveur Microsoft a peut-être
                # déjà traité la requête et envoyé l'email. Un retry causerait un double envoi.
                raise

    except Exception as e:
        current_app.logger.error(f"Microsoft Graph API send failed after {max_retries} attempts: {str(e)}")
        raise e


def _refresh_microsoft_oauth_token(email_config, max_retries=3, user_id=None):
    """
    Refresh Microsoft OAuth token for system or user email configuration.
    ROBUSTESSE: Retry automatique avec backoff exponentiel (3s, 7s, 15s).

    Args:
        email_config: SystemEmailConfiguration or EmailConfiguration with OAuth tokens
        max_retries: Number of retry attempts
        user_id: If provided, logs include user context (for user tokens)
    """
    import time

    context = f"user {user_id}" if user_id else "system"

    if not hasattr(email_config, 'outlook_oauth_refresh_token') or not email_config.outlook_oauth_refresh_token:
        raise Exception(f"No refresh token available for {context} email configuration")

    # Microsoft OAuth token endpoint
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    # Get client credentials
    client_id = current_app.config.get('MICROSOFT_CLIENT_ID')
    client_secret = current_app.config.get('MICROSOFT_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise Exception("Microsoft OAuth credentials not configured")

    # Token refresh data
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': email_config.outlook_oauth_refresh_token,
        'grant_type': 'refresh_token',
        'scope': 'Mail.Send Mail.ReadWrite User.Read offline_access'
    }

    # ROBUSTESSE: Retry avec backoff exponentiel
    last_error = None
    for attempt in range(max_retries):
        try:
            from utils.http_client import create_microsoft_session
            session_http = create_microsoft_session()
            response = session_http.post(token_url, data=data, timeout=HTTP_TIMEOUT_DEFAULT)

            if response.status_code == 200:
                token_data = response.json()

                # Update config with new tokens
                email_config.outlook_oauth_access_token = token_data.get('access_token')
                if 'refresh_token' in token_data:
                    email_config.outlook_oauth_refresh_token = token_data['refresh_token']

                # Update token expiry if we have the field
                if hasattr(email_config, 'outlook_oauth_token_expires'):
                    expires_in = token_data.get('expires_in', 3600)
                    email_config.outlook_oauth_token_expires = datetime.utcnow() + timedelta(seconds=expires_in)

                # Save to database
                from app import db
                db.session.commit()

                if user_id:
                    current_app.logger.info(f"User OAuth token refreshed successfully for user {user_id}")
                return True

            elif response.status_code in [429, 503, 502, 504]:
                # Erreurs temporaires - retry avec backoff exponentiel
                wait_time = (2 ** attempt) + 1
                current_app.logger.warning(
                    f"{context.capitalize()} token refresh temporairement echoue "
                    f"(tentative {attempt + 1}/{max_retries}), retry dans {wait_time}s"
                )
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                continue
            else:
                # Erreur permanente (401, 403, etc.)
                error_msg = f"{context.capitalize()} token refresh failed: {response.status_code} - {response.text}"
                current_app.logger.error(error_msg)
                raise Exception(error_msg)

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + 1
                current_app.logger.warning(
                    f"Erreur refresh {context} token "
                    f"(tentative {attempt + 1}/{max_retries}): {str(e)}, retry dans {wait_time}s"
                )
                time.sleep(wait_time)
            else:
                # Derniere tentative echouee
                current_app.logger.error(
                    f"{context.capitalize()} token refresh error apres {max_retries} tentatives: {str(e)}"
                )

    # Toutes les tentatives ont echoue
    raise Exception(
        f"Impossible de rafraichir le token {context} apres {max_retries} tentatives. "
        f"Derniere erreur: {str(last_error)}"
    )


def refresh_system_oauth_token(email_config, max_retries=3):
    """Refresh Microsoft OAuth token for system email configuration."""
    return _refresh_microsoft_oauth_token(email_config, max_retries=max_retries)


def refresh_user_oauth_token(email_config, max_retries=3):
    """Refresh Microsoft OAuth token for user email configuration."""
    user_id = getattr(email_config, 'user_id', None)
    return _refresh_microsoft_oauth_token(email_config, max_retries=max_retries, user_id=user_id)


# Gmail OAuth refresh function removed - Gmail now uses SMTP instead of OAuth
# def refresh_gmail_oauth_token(email_config): ...

def send_email_via_gmail(email_config, to_email, subject, html_content, attachments=None):
    """Send email via Gmail SMTP"""

    current_app.logger.info(f"Attempting to send email to {to_email} via Gmail SMTP")

    try:
        # Check if we have Gmail SMTP credentials
        if not email_config.gmail_email or not email_config.gmail_smtp_app_password:
            raise Exception("Gmail SMTP non configuré. Veuillez configurer votre adresse Gmail et votre mot de passe d'application.")

        # Send via Gmail SMTP
        from gmail_smtp import GmailSMTPConnector
        connector = GmailSMTPConnector()

        success = connector.send_email(
            gmail_email=email_config.gmail_email,
            app_password=email_config.gmail_smtp_app_password,
            to_emails=[to_email],
            subject=subject,
            body=html_content,
            from_name=None,
            attachments=attachments
        )

        if success:
            current_app.logger.info(f"Email sent successfully via Gmail SMTP to {to_email}")
            return True
        else:
            raise Exception("Gmail SMTP send failed")

    except Exception as e:
        current_app.logger.error(f"Gmail SMTP send error: {str(e)}")
        raise e