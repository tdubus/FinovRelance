"""Module pour envoyer l'email de bienvenue aux nouvelles entreprises"""

import os
from datetime import datetime
from flask import current_app
from email_fallback import send_email_via_system_config


def send_welcome_email(company_name, company_email, plan_name, user_first_name):
    """
    Envoyer un email de bienvenue aux nouvelles entreprises avec plan payant

    Args:
        company_name: Nom de l'entreprise
        company_email: Email de l'entreprise
        plan_name: Nom du plan (display_name)
        user_first_name: Prénom du contact principal
    """
    try:
        current_app.logger.info(f"Envoi email de bienvenue à {company_name} ({company_email})")

        # Lien de réservation pour l'onboarding
        booking_link = os.environ.get('BOOKING_URL', "https://outlook.office.com/book/FinovRelance@finova-solutions.com/s/seWAHOnojUCUarM7F5W7UQ2?ismsaljsauthenabled")

        # Logo URL - utiliser le chemin absolu
        logo_url = f"{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/marketing-static/images/logo.png"

        # Template HTML de l'email
        html_content = f"""
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bienvenue chez FinovRelance</title>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f7fa;">
    <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f5f7fa;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                    <!-- Header avec logo -->
                    <tr>
                        <td style="background: linear-gradient(135deg, #8475EC 0%, #6B5DC0 100%); padding: 40px 20px; text-align: center;">
                            <img src="{logo_url}" alt="FinovRelance" style="height: 70px; width: auto; display: block; margin: 0 auto;">
                        </td>
                    </tr>

                    <!-- Contenu principal -->
                    <tr>
                        <td style="padding: 40px 30px;">
                            <h1 style="color: #1a202c; font-size: 28px; font-weight: 700; margin: 0 0 20px 0; line-height: 1.3;">
                                Bienvenue chez FinovRelance ! 🎉
                            </h1>

                            <p style="color: #4a5568; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">
                                Bonjour {user_first_name},
                            </p>

                            <p style="color: #4a5568; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">
                                Félicitations pour votre inscription au plan <strong>{plan_name}</strong> ! Nous sommes ravis de vous accueillir et de vous accompagner dans l'optimisation de votre gestion des comptes à recevoir.
                            </p>

                            <p style="color: #4a5568; font-size: 16px; line-height: 1.6; margin: 0 0 30px 0;">
                                Pour vous aider à démarrer du bon pied, nous vous offrons un <strong>accompagnement personnalisé</strong> avec un membre de notre équipe. Cette session d'onboarding vous permettra de :
                            </p>

                            <ul style="color: #4a5568; font-size: 16px; line-height: 1.8; margin: 0 0 30px 0; padding-left: 20px;">
                                <li>Configurer votre compte selon vos besoins</li>
                                <li>Découvrir les fonctionnalités clés de la plateforme</li>
                                <li>Importer vos données clients et factures</li>
                                <li>Personnaliser vos modèles de relance</li>
                                <li>Poser toutes vos questions</li>
                            </ul>

                            <p style="color: #4a5568; font-size: 16px; line-height: 1.6; margin: 0 0 30px 0;">
                                Réservez dès maintenant votre session d'onboarding personnalisée :
                            </p>

                            <!-- Bouton CTA -->
                            <table role="presentation" style="margin: 0 auto;">
                                <tr>
                                    <td style="text-align: center;">
                                        <a href="{booking_link}"
                                           style="display: inline-block; background-color: #ffffff; color: #000000 !important;
                                                  font-size: 18px; font-weight: 600; text-decoration: none; padding: 16px 40px;
                                                  border-radius: 8px; border: 2px solid #8475EC; box-shadow: 0 4px 12px rgba(132, 117, 236, 0.2);">
                                            <span style="color: #000000 !important;">📅 Réserver mon rendez-vous</span>
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="color: #718096; font-size: 14px; line-height: 1.6; margin: 30px 0 0 0; text-align: center;">
                                <em>Vous pouvez également copier ce lien :</em><br>
                                <a href="{booking_link}" style="color: #8475EC; word-break: break-all; font-size: 13px;">
                                    {booking_link}
                                </a>
                            </p>
                        </td>
                    </tr>

                    <!-- Section d'information supplémentaire -->
                    <tr>
                        <td style="background-color: #f7fafc; padding: 30px; border-top: 1px solid #e2e8f0;">
                            <h3 style="color: #2d3748; font-size: 18px; font-weight: 600; margin: 0 0 15px 0;">
                                En attendant, voici quelques ressources utiles :
                            </h3>

                            <ul style="color: #4a5568; font-size: 15px; line-height: 1.8; margin: 0; padding-left: 20px;">
                                <li><a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/guide" style="color: #8475EC; text-decoration: none;">Guide d'utilisation complet</a></li>
                                <li><a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/fonctionnalites" style="color: #8475EC; text-decoration: none;">Découvrir toutes les fonctionnalités</a></li>
                                <li><a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/contact" style="color: #8475EC; text-decoration: none;">Nous contacter</a></li>
                            </ul>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px; text-align: center; border-top: 1px solid #e2e8f0;">
                            <p style="color: #718096; font-size: 14px; line-height: 1.6; margin: 0 0 10px 0;">
                                Vous avez des questions ? Notre équipe est là pour vous aider.
                            </p>
                            <p style="color: #8475EC; font-size: 15px; font-weight: 600; margin: 0 0 20px 0;">
                                <a href="mailto:support@finov-relance.com" style="color: #8475EC; text-decoration: none;">
                                    support@finov-relance.com
                                </a>
                            </p>

                            <p style="color: #a0aec0; font-size: 13px; line-height: 1.5; margin: 0;">
                                © {datetime.now().year} FinovRelance - Tous droits réservés<br>
                                Optimisez votre gestion des comptes à recevoir
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

        # Sujet de l'email
        subject = f"Bienvenue chez FinovRelance - Réservez votre session d'onboarding"

        # Envoyer l'email via le système configuré (support@finov-relance.com)
        result = send_email_via_system_config(
            to_email=company_email,
            subject=subject,
            html_content=html_content
        )

        if result:
            current_app.logger.info(f"✅ Email de bienvenue envoyé avec succès à {company_email}")
            return True
        else:
            current_app.logger.error(f"❌ Échec envoi email de bienvenue à {company_email}")
            return False

    except Exception as e:
        current_app.logger.error(f"❌ Erreur envoi email de bienvenue à {company_email}: {str(e)}")
        return False
