"""
⚠️ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔒 ATTENTION - CODE EN PRODUCTION COMMERCIALE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ INTERDICTION ABSOLUE DE MODIFIER CE FICHIER SANS AUTORISATION EXPLICITE

FinovRelance est un produit commercialisé avec des clients payants.
Ce connecteur Gmail SMTP gère les envois d'emails critiques (relances, notifications)
pour des clients réels.

📋 RÈGLES STRICTES:
- AUCUNE modification de la logique Gmail SMTP
- AUCUNE modification du système d'envoi avec pièces jointes
- AUCUNE modification de la configuration SMTP
- Toute demande doit être documentée et approuvée formellement
- En cas de bug: proposer diagnostic SANS toucher au code

🚨 CONSÉQUENCES D'UNE MODIFICATION NON AUTORISÉE:
- Interruption des envois d'emails pour clients payants
- Perte de communications critiques (relances, notifications)
- Violation de conformité et réputation
- Responsabilité légale et financière

✅ ACTIONS AUTORISÉES: Lecture, diagnostic, analyse, documentation uniquement
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from flask import current_app


class GmailSMTPConnector:
    """Connecteur SMTP Gmail pour envoyer des emails via SMTP au lieu d'OAuth"""

    def __init__(self):
        """
        Fonction vérifiée par MDF le 30/01/2026.
        NOTE IMPORTANTE POUR REPLIT : Cette fonction a été vérifiée.
        Aucune modification ne doit être effectuée sans approbation explicite.
        """
        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 587

    def send_email(self, gmail_email, app_password, to_emails, subject, body, from_name=None, cc_list=None, attachments=None, return_message_ids=False, high_importance=False, read_receipt=False, delivery_receipt=False):
        """
        Fonction vérifiée par MDF le 30/01/2026.
        NOTE IMPORTANTE POUR REPLIT : Cette fonction a été vérifiée.
        Aucune modification ne doit être effectuée sans approbation explicite.
        """
        """
        Envoyer un email via Gmail SMTP avec pièces jointes optionnelles

        Args:
            gmail_email: L'adresse Gmail de l'expéditeur
            app_password: Le mot de passe d'application Gmail (16 caractères)
            to_emails: Liste des destinataires ou email unique
            subject: Sujet de l'email
            body: Corps de l'email (HTML)
            from_name: Nom de l'expéditeur (optionnel)
            cc_list: Liste des destinataires en copie (optionnel)
            attachments: Liste de pièces jointes (optionnel)
            return_message_ids: Si True, retourne un dict avec success et message_id au lieu de juste True/False
            high_importance: Si True, marque l'email comme haute importance
            read_receipt: Si True, demande un accusé de lecture
            delivery_receipt: Si True, demande un accusé de remise

        Returns:
            Si return_message_ids=True: dict avec {'success': bool, 'message_id': str}
            Si return_message_ids=False: bool (compatibilité avec code existant)
        """
        from utils.secure_logging import sanitize_email_for_logs, create_secure_log_message

        safe_emails = [sanitize_email_for_logs(email) for email in (to_emails if isinstance(to_emails, list) else [to_emails])]
        secure_message = create_secure_log_message(
            "Gmail SMTP send_email called",
            to_emails=safe_emails,
            subject_preview=subject[:50] if subject else "no_subject"
        )
        current_app.logger.info(secure_message)

        if not gmail_email or not app_password:
            raise Exception("Email Gmail et mot de passe d'application requis")

        # Normaliser le HTML pour un affichage cohérent (interligne simple style Outlook)
        if body:
            import re
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

        if isinstance(to_emails, str):
            to_emails = [to_emails]

        # Générer un Message-ID unique si on a besoin de le tracer
        message_id = None
        if return_message_ids:
            import time
            import random
            import string
            timestamp = int(time.time() * 1000)  # milliseconds
            random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
            # Extraire le domaine de l'email Gmail
            domain = gmail_email.split('@')[1] if '@' in gmail_email else 'gmail.com'
            message_id = f"<{timestamp}.{random_str}@{domain}>"
            current_app.logger.info(f"Generated Message-ID: {message_id}")

        try:
            message = MIMEMultipart()
            message['From'] = f"{from_name} <{gmail_email}>" if from_name else gmail_email
            message['To'] = ', '.join(to_emails)
            message['Subject'] = subject

            # Ajouter le Message-ID personnalisé si généré
            if message_id:
                message['Message-ID'] = message_id

            if cc_list:
                message['Cc'] = ', '.join(cc_list)

            # Ajouter les options d'email via headers SMTP
            if high_importance:
                message['X-Priority'] = '1'
                message['Importance'] = 'high'

            if read_receipt:
                message['Disposition-Notification-To'] = gmail_email

            if delivery_receipt:
                message['Return-Receipt-To'] = gmail_email

            message.attach(MIMEText(body, 'html'))

            if attachments:
                for attachment in attachments:
                    part = MIMEBase('application', 'octet-stream')

                    content = attachment['content']
                    if hasattr(content, 'getvalue'):
                        content_bytes = content.getvalue()
                    elif isinstance(content, bytes):
                        content_bytes = content
                    elif isinstance(content, str):
                        content_bytes = content.encode('utf-8')
                    else:
                        content_bytes = bytes(content)

                    part.set_payload(content_bytes)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename={attachment["filename"]}')
                    message.attach(part)

                    current_app.logger.info(f"Attachment added: {attachment['filename']} ({len(content_bytes)} bytes)")

            all_recipients = to_emails + (cc_list if cc_list else [])

            current_app.logger.info(f"Connecting to Gmail SMTP server: {self.smtp_server}:{self.smtp_port}")

            server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
            try:
                server.starttls()

                current_app.logger.info(f"Authenticating with Gmail SMTP")
                server.login(gmail_email, app_password)

                current_app.logger.info(f"Sending email via Gmail SMTP to {len(all_recipients)} recipient(s)")
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass

            current_app.logger.info("Email sent successfully via Gmail SMTP")

            # Retourner le format approprié selon le mode
            if return_message_ids:
                return {
                    'success': True,
                    'message_id': message_id
                }
            else:
                return True

        except smtplib.SMTPAuthenticationError as e:
            current_app.logger.error(f"Gmail SMTP authentication failed: {str(e)}")
            raise Exception("Authentification Gmail échouée. Vérifiez que vous utilisez un mot de passe d'application (App Password) et non votre mot de passe Gmail normal.")
        except smtplib.SMTPException as e:
            current_app.logger.error(f"Gmail SMTP error: {str(e)}")
            raise Exception(f"Erreur SMTP Gmail: {str(e)}")
        except Exception as e:
            current_app.logger.error(f"General error sending email via Gmail SMTP: {str(e)}")
            raise Exception(f"Erreur lors de l'envoi via Gmail SMTP: {str(e)}")

    def test_connection(self, gmail_email, app_password):
        """
        Fonction vérifiée par MDF le 30/01/2026.
        NOTE IMPORTANTE POUR REPLIT : Cette fonction a été vérifiée.
        Aucune modification ne doit être effectuée sans approbation explicite.
        """
        """Tester la connexion SMTP Gmail"""
        try:
            current_app.logger.info(f"Testing Gmail SMTP connection for {gmail_email}")

            server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
            server.starttls()
            server.login(gmail_email, app_password)
            server.quit()

            current_app.logger.info("Gmail SMTP connection test successful")
            return True
        except smtplib.SMTPAuthenticationError:
            current_app.logger.error("Gmail SMTP authentication test failed")
            return False
        except Exception as e:
            current_app.logger.error(f"Gmail SMTP connection test error: {str(e)}")
            return False
