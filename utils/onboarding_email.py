import os
from flask import current_app


def send_password_setup_email(user_email, first_name, company_name, setup_url):
    from email_fallback import send_email_via_system_config

    subject = "FinovRelance — Configurez votre mot de passe"
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px; text-align: center;">
            <h2 style="color: #8475EC; margin-bottom: 20px;">Bienvenue chez FinovRelance</h2>

            <p style="font-size: 16px; color: #333; margin-bottom: 25px;">
                Bonjour {first_name},
            </p>

            <p style="font-size: 14px; color: #666; margin-bottom: 15px;">
                Votre compte a été créé avec succès pour l'entreprise <strong>{company_name}</strong>.
            </p>

            <p style="font-size: 14px; color: #666; margin-bottom: 25px;">
                Votre période d'essai gratuite de <strong>14 jours</strong> est maintenant active.
                Pour accéder à votre tableau de bord, veuillez configurer votre mot de passe :
            </p>

            <div style="background-color: #ffffff; padding: 20px; border-radius: 8px; margin: 25px 0; border: 2px solid #8475EC;">
                <a href="{setup_url}" style="background-color: #8475EC; color: #ffffff; padding: 14px 32px; text-decoration: none; border-radius: 8px; font-size: 16px; font-weight: 600; display: inline-block;">
                    Configurer mon mot de passe
                </a>
            </div>

            <div style="background-color: #fff3cd; padding: 15px; border-radius: 8px; margin: 20px 0; border-left: 4px solid #ffc107;">
                <p style="font-size: 12px; color: #856404; margin: 0;">
                    <strong>Information de sécurité :</strong><br>
                    Ce lien est valide pendant <strong>48 heures</strong> et ne peut être utilisé qu'une seule fois.<br>
                    Si vous n'avez pas créé de compte, vous pouvez ignorer cet email.
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

    try:
        result = send_email_via_system_config(user_email, subject, html_content)
        current_app.logger.info(f"Email de configuration mot de passe envoyé à {user_email}")
        return result
    except Exception as e:
        current_app.logger.error(f"Erreur envoi email setup password à {user_email}: {e}")
        return False


def send_new_company_confirmation_email(user_email, first_name, company_name, login_url=None, has_trial=False):
    from email_fallback import send_email_via_system_config

    if not login_url:
        login_url = f"{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/auth/login"

    if has_trial:
        subscription_info = """
            <p style="font-size: 14px; color: #666; margin-bottom: 25px;">
                Votre période d'essai gratuite de <strong>14 jours</strong> est maintenant active pour cette entreprise.
                Connectez-vous à votre tableau de bord pour commencer la configuration :
            </p>"""
    else:
        subscription_info = """
            <p style="font-size: 14px; color: #666; margin-bottom: 25px;">
                Votre abonnement est maintenant actif pour cette entreprise.
                Connectez-vous à votre tableau de bord pour commencer la configuration :
            </p>"""

    subject = f"FinovRelance — Nouvelle entreprise ajoutée : {company_name}"
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px; text-align: center;">
            <h2 style="color: #8475EC; margin-bottom: 20px;">Nouvelle entreprise ajoutée</h2>

            <p style="font-size: 16px; color: #333; margin-bottom: 25px;">
                Bonjour {first_name},
            </p>

            <p style="font-size: 14px; color: #666; margin-bottom: 15px;">
                L'entreprise <strong>{company_name}</strong> a été ajoutée avec succès à votre compte FinovRelance.
            </p>

            {subscription_info}

            <div style="background-color: #ffffff; padding: 20px; border-radius: 8px; margin: 25px 0; border: 2px solid #8475EC;">
                <a href="{login_url}" style="background-color: #8475EC; color: #ffffff; padding: 14px 32px; text-decoration: none; border-radius: 8px; font-size: 16px; font-weight: 600; display: inline-block;">
                    Accéder à mon tableau de bord
                </a>
            </div>

            <p style="font-size: 12px; color: #666; margin-top: 30px;">
                Équipe FinovRelance<br>
                <a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}" style="color: #8475EC;">{os.environ.get('APP_URL', 'https://app.finov-relance.com').replace('https://', '')}</a>
            </p>
        </div>
    </body>
    </html>
    """

    try:
        result = send_email_via_system_config(user_email, subject, html_content)
        current_app.logger.info(f"Email de confirmation nouvelle entreprise envoyé à {user_email}")
        return result
    except Exception as e:
        current_app.logger.error(f"Erreur envoi email confirmation à {user_email}: {e}")
        return False
