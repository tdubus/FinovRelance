"""
PHASE 4 - Système de notifications automatiques complet
Notifications in-app + emails automatiques pour événements abonnements
"""

import os
from flask import current_app
from app import db
from models import Notification, UserCompany
from email_fallback import send_email_via_system_config
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

APP_URL = os.environ.get('APP_URL', 'https://app.finov-relance.com')

def send_notification(user_id, company_id, type, title, message, data=None):
    """
    Create and store a notification in the database
    No real-time broadcasting - will be retrieved via AJAX polling
    """
    try:
        # Create notification in database
        notification = Notification.create_notification(
            user_id=user_id,
            company_id=company_id,
            type=type,
            title=title,
            message=message,
            data=data
        )

        logger.info(f"Notification created for user {user_id}: {title}")
        return notification

    except Exception as e:
        logger.error(f"Failed to create notification: {e}")
        return None

def get_unread_notifications(user_id, company_id):
    """
    Get unread notifications for a specific user and company
    """
    return Notification.query.filter_by(
        user_id=user_id,
        company_id=company_id,
        is_read=False
    ).order_by(Notification.created_at.desc()).all()

def get_recent_notifications(user_id, company_id, limit=10):
    """
    Get recent notifications for a specific user and company
    Only return unread notifications so read ones disappear from the list
    """
    return Notification.query.filter_by(
        user_id=user_id,
        company_id=company_id,
        is_read=False  # Only show unread notifications
    ).order_by(Notification.created_at.desc()).limit(limit).all()

# =============================================================================
# PHASE 4 - NOTIFICATIONS AUTOMATIQUES ABONNEMENTS
# =============================================================================

def send_subscription_pending_cancellation_notification(company):
    """PHASE 2 : Notification d'annulation programmée"""
    try:
        admin_users = UserCompany.query.filter(
            UserCompany.company_id == company.id,
            UserCompany.role.in_(['admin', 'super_admin']),
            UserCompany.is_active == True
        ).all()

        title = "Annulation programmée"
        message = f"Votre abonnement sera annulé à la fin de la période en cours. Vous pouvez le réactiver depuis les paramètres."

        for user_company in admin_users:
            send_notification(
                user_id=user_company.user_id,
                company_id=company.id,
                type='pending_cancellation',
                title=title,
                message=message,
                data={'cancellation_date': company.cancellation_date.isoformat() if company.cancellation_date else None}
            )

        current_app.logger.info(f"📧 Notifications annulation programmée envoyées pour {company.name}")

    except Exception as e:
        current_app.logger.error(f"❌ Erreur notification annulation programmée: {str(e)}")

def send_subscription_cancellation_notification(company):
    """
    PHASE 4: Notification automatique d'annulation d'abonnement
    Envoie notification in-app + email aux admins de l'entreprise
    """
    try:
        # 1. Trouver tous les admins actifs de l'entreprise
        admin_users = UserCompany.query.filter(
            UserCompany.company_id == company.id,
            UserCompany.role.in_(['admin', 'super_admin']),
            UserCompany.is_active == True
        ).all()

        if not admin_users:
            logger.warning(f"Aucun admin trouvé pour l'entreprise {company.name}")
            return False

        # 2. Calculer la date d'expiration
        try:
            import stripe
            subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)
            expiry_date = datetime.fromtimestamp(subscription['current_period_end'])
            expiry_str = expiry_date.strftime("%d/%m/%Y")
        except Exception as e:
            logger.error(f"Erreur récupération date expiration: {e}")
            expiry_str = "Non disponible"

        notifications_sent = 0
        emails_sent = 0

        for admin_user_company in admin_users:
            admin_user = admin_user_company.user

            # 3. Créer notification in-app
            notification = send_notification(
                user_id=admin_user.id,
                company_id=company.id,
                type='subscription_cancellation',
                title='⚠️ Abonnement annulé',
                message=f"Votre abonnement sera suspendu le {expiry_str}. Réactivez-le pour éviter l'interruption du service.",
                data={
                    'company_name': company.name,
                    'expiry_date': expiry_str,
                    'subscription_id': company.stripe_subscription_id,
                    'action_url': '/company/subscription'
                }
            )

            if notification:
                notifications_sent += 1

            # 4. Envoyer email de confirmation
            email_sent = send_cancellation_email(admin_user, company, expiry_str)
            if email_sent:
                emails_sent += 1

        logger.info(f"Notifications annulation envoyées pour {company.name}: {notifications_sent} in-app, {emails_sent} emails")
        return notifications_sent > 0 or emails_sent > 0

    except Exception as e:
        logger.error(f"Erreur envoi notifications annulation pour {company.name}: {str(e)}")
        return False

def send_grace_period_expiry_notification(company, days_remaining=7):
    """
    PHASE 4: Notification d'expiration imminente de période de grâce
    """
    try:
        # Récupérer période de grâce active
        # REFONTE STRIPE V2 : Grace period géré via subscription_status
        grace_period_active = company.subscription_status in ['past_due', 'pending_cancellation']
        if not grace_period_active:
            return False

        # Trouver admins
        admin_users = UserCompany.query.filter(
            UserCompany.company_id == company.id,
            UserCompany.role.in_(['admin', 'super_admin']),
            UserCompany.is_active == True
        ).all()

        if not admin_users:
            return False

        # REFONTE STRIPE V2 : Date d'expiration basée sur cancellation_date
        from datetime import datetime, timedelta
        expiry_date = (datetime.now() + timedelta(days=days_remaining)).strftime("%d/%m/%Y à %H:%M")

        notifications_sent = 0
        emails_sent = 0

        for admin_user_company in admin_users:
            admin_user = admin_user_company.user

            # Notification in-app urgente
            notification = send_notification(
                user_id=admin_user.id,
                company_id=company.id,
                type='grace_period_expiry',
                title=f'🚨 Période de grâce expire dans {days_remaining} jour(s)',
                message=f"Votre période de grâce expire le {expiry_date}. Mettez à jour votre abonnement rapidement.",
                data={
                    'company_name': company.name,
                    'expiry_date': expiry_date,
                    'days_remaining': days_remaining,
                    'subscription_status': company.subscription_status,
                    'action_url': '/company/subscription',
                    'urgency': 'high'
                }
            )

            if notification:
                notifications_sent += 1

            # Email d'urgence
            email_sent = send_grace_period_expiry_email(admin_user, company, expiry_date, days_remaining)
            if email_sent:
                emails_sent += 1

        logger.info(f"Notifications période de grâce pour {company.name}: {notifications_sent} in-app, {emails_sent} emails")
        return notifications_sent > 0 or emails_sent > 0

    except Exception as e:
        logger.error(f"Erreur notifications période de grâce pour {company.name}: {str(e)}")
        return False

def send_subscription_reactivated_notification(company):
    """
    PHASE 4: Notification de réactivation d'abonnement
    """
    try:
        admin_users = UserCompany.query.filter(
            UserCompany.company_id == company.id,
            UserCompany.role.in_(['admin', 'super_admin']),
            UserCompany.is_active == True
        ).all()

        if not admin_users:
            return False

        notifications_sent = 0
        emails_sent = 0

        for admin_user_company in admin_users:
            admin_user = admin_user_company.user

            # Notification positive
            notification = send_notification(
                user_id=admin_user.id,
                company_id=company.id,
                type='subscription_reactivated',
                title='✅ Abonnement réactivé',
                message=f"Votre abonnement a été réactivé avec succès. Votre service continue normalement.",
                data={
                    'company_name': company.name,
                    'reactivation_date': datetime.utcnow().strftime("%d/%m/%Y à %H:%M"),
                    'action_url': '/company/subscription'
                }
            )

            if notification:
                notifications_sent += 1

            # Email de confirmation
            email_sent = send_reactivation_email(admin_user, company)
            if email_sent:
                emails_sent += 1

        logger.info(f"Notifications réactivation pour {company.name}: {notifications_sent} in-app, {emails_sent} emails")
        return notifications_sent > 0 or emails_sent > 0

    except Exception as e:
        logger.error(f"Erreur notifications réactivation pour {company.name}: {str(e)}")
        return False

def send_payment_failed_notification(company):
    """
    PHASE 4: Notification d'échec de paiement
    """
    try:
        admin_users = UserCompany.query.filter(
            UserCompany.company_id == company.id,
            UserCompany.role.in_(['admin', 'super_admin']),
            UserCompany.is_active == True
        ).all()

        if not admin_users:
            return False

        notifications_sent = 0
        emails_sent = 0

        for admin_user_company in admin_users:
            admin_user = admin_user_company.user

            # Notification urgente paiement
            notification = send_notification(
                user_id=admin_user.id,
                company_id=company.id,
                type='payment_failed',
                title='❌ Échec de paiement',
                message=f"Le paiement de votre abonnement a échoué. Mettez à jour vos informations de paiement.",
                data={
                    'company_name': company.name,
                    'failure_date': datetime.utcnow().strftime("%d/%m/%Y à %H:%M"),
                    'action_url': '/company/subscription',
                    'urgency': 'high'
                }
            )

            if notification:
                notifications_sent += 1

            # Email urgent
            email_sent = send_payment_failed_email(admin_user, company)
            if email_sent:
                emails_sent += 1

        logger.info(f"Notifications échec paiement pour {company.name}: {notifications_sent} in-app, {emails_sent} emails")
        return notifications_sent > 0 or emails_sent > 0

    except Exception as e:
        logger.error(f"Erreur notifications échec paiement pour {company.name}: {str(e)}")
        return False

# =============================================================================
# FONCTIONS D'ENVOI D'EMAILS SPÉCIALISÉES
# =============================================================================

def send_cancellation_email(admin_user, company, expiry_date):
    """Envoyer email d'annulation d'abonnement"""
    try:
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto;">
            <div style="background-color: #fff3cd; padding: 20px; border-radius: 8px; border-left: 4px solid #ffc107;">
                <h2 style="color: #856404; margin-bottom: 20px;">⚠️ Abonnement annulé</h2>

                <p>Bonjour {admin_user.first_name} {admin_user.last_name},</p>

                <p>Nous vous informons que votre abonnement pour l'entreprise <strong>{company.name}</strong> a été annulé.</p>

                <div style="background-color: white; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #856404; margin-top: 0;">Informations importantes :</h3>
                    <ul>
                        <li><strong>Date d'expiration :</strong> {expiry_date}</li>
                        <li><strong>Accès maintenu :</strong> Jusqu'à la date d'expiration</li>
                        <li><strong>Réactivation :</strong> Possible à tout moment avant expiration</li>
                    </ul>
                </div>

                <div style="text-align: center; margin: 30px 0;">
                    <p style="margin-bottom: 15px;">Vous pouvez réactiver votre abonnement à tout moment :</p>
                    <a href="{APP_URL}/company/subscription"
                       style="background-color: #007bff; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; display: inline-block;">
                        Réactiver l'abonnement
                    </a>
                </div>

                <p style="font-size: 12px; color: #6c757d; margin-top: 30px;">
                    Si vous avez des questions, contactez notre support à support@finov-relance.com
                </p>
            </div>
        </body>
        </html>
        """

        return send_email_via_system_config(
            to_email=admin_user.email,
            subject=f"FinovRelance - Abonnement annulé pour {company.name}",
            html_content=html_content
        )

    except Exception as e:
        logger.error(f"Erreur envoi email annulation: {str(e)}")
        return False

def send_grace_period_expiry_email(admin_user, company, expiry_date, days_remaining):
    """Envoyer email d'expiration période de grâce"""
    try:
        urgency_color = "#dc3545" if days_remaining <= 3 else "#ffc107"
        urgency_text = "URGENT" if days_remaining <= 3 else "ATTENTION"

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto;">
            <div style="background-color: #f8d7da; padding: 20px; border-radius: 8px; border-left: 4px solid {urgency_color};">
                <h2 style="color: #721c24; margin-bottom: 20px;">🚨 {urgency_text} - Période de grâce expire bientôt</h2>

                <p>Bonjour {admin_user.first_name} {admin_user.last_name},</p>

                <p><strong>Votre période de grâce expire dans {days_remaining} jour(s) !</strong></p>

                <div style="background-color: white; padding: 15px; border-radius: 5px; margin: 20px 0; border: 2px solid {urgency_color};">
                    <h3 style="color: #721c24; margin-top: 0;">Détails urgents :</h3>
                    <ul>
                        <li><strong>Entreprise :</strong> {company.name}</li>
                        <li><strong>Expiration :</strong> {expiry_date}</li>
                        <li><strong>Jours restants :</strong> {days_remaining}</li>
                        <li><strong>Action requise :</strong> Mise à jour de l'abonnement</li>
                    </ul>
                </div>

                <div style="background-color: #fff3cd; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <p style="margin: 0; color: #856404;">
                        <strong>⚠️ Important :</strong> Après expiration, l'accès à votre compte sera suspendu.
                        Vos données seront préservées mais inaccessibles jusqu'au renouvellement.
                    </p>
                </div>

                <div style="text-align: center; margin: 30px 0;">
                    <a href="{APP_URL}/company/subscription"
                       style="background-color: {urgency_color}; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; display: inline-block; font-weight: bold;">
                        RENOUVELER MAINTENANT
                    </a>
                </div>

                <p style="font-size: 12px; color: #6c757d; margin-top: 30px;">
                    Email automatique - Support: support@finov-relance.com
                </p>
            </div>
        </body>
        </html>
        """

        return send_email_via_system_config(
            to_email=admin_user.email,
            subject=f"[{urgency_text}] Période de grâce expire dans {days_remaining} jour(s) - {company.name}",
            html_content=html_content
        )

    except Exception as e:
        logger.error(f"Erreur envoi email expiration grâce: {str(e)}")
        return False

def send_reactivation_email(admin_user, company):
    """Envoyer email de réactivation d'abonnement"""
    try:
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto;">
            <div style="background-color: #d4edda; padding: 20px; border-radius: 8px; border-left: 4px solid #28a745;">
                <h2 style="color: #155724; margin-bottom: 20px;">✅ Abonnement réactivé avec succès</h2>

                <p>Bonjour {admin_user.first_name} {admin_user.last_name},</p>

                <p>Excellente nouvelle ! Votre abonnement pour l'entreprise <strong>{company.name}</strong> a été réactivé avec succès.</p>

                <div style="background-color: white; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #155724; margin-top: 0;">Votre service continue normalement :</h3>
                    <ul>
                        <li>✅ Accès complet à votre compte</li>
                        <li>✅ Toutes les fonctionnalités disponibles</li>
                        <li>✅ Facturation selon votre plan actuel</li>
                        <li>✅ Support technique complet</li>
                    </ul>
                </div>

                <div style="text-align: center; margin: 30px 0;">
                    <a href="{APP_URL}/company/subscription"
                       style="background-color: #28a745; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; display: inline-block;">
                        Voir mon abonnement
                    </a>
                </div>

                <p style="font-size: 12px; color: #6c757d; margin-top: 30px;">
                    Merci de votre confiance - Support: support@finov-relance.com
                </p>
            </div>
        </body>
        </html>
        """

        return send_email_via_system_config(
            to_email=admin_user.email,
            subject=f"FinovRelance - Abonnement réactivé pour {company.name}",
            html_content=html_content
        )

    except Exception as e:
        logger.error(f"Erreur envoi email réactivation: {str(e)}")
        return False

def send_payment_failed_email(admin_user, company):
    """Envoyer email d'échec de paiement"""
    try:
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto;">
            <div style="background-color: #f8d7da; padding: 20px; border-radius: 8px; border-left: 4px solid #dc3545;">
                <h2 style="color: #721c24; margin-bottom: 20px;">❌ Échec de paiement - Action requise</h2>

                <p>Bonjour {admin_user.first_name} {admin_user.last_name},</p>

                <p>Le paiement de votre abonnement pour l'entreprise <strong>{company.name}</strong> a échoué.</p>

                <div style="background-color: white; padding: 15px; border-radius: 5px; margin: 20px 0; border: 2px solid #dc3545;">
                    <h3 style="color: #721c24; margin-top: 0;">Action immédiate requise :</h3>
                    <ul>
                        <li>🔄 Vérifiez vos informations de paiement</li>
                        <li>💳 Mettez à jour votre carte de crédit si nécessaire</li>
                        <li>📧 Vérifiez les emails de votre banque</li>
                        <li>⏰ Résolvez rapidement pour éviter la suspension</li>
                    </ul>
                </div>

                <div style="background-color: #fff3cd; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <p style="margin: 0; color: #856404;">
                        <strong>⚠️ Important :</strong> Si le problème n'est pas résolu rapidement,
                        votre service pourrait être suspendu temporairement.
                    </p>
                </div>

                <div style="text-align: center; margin: 30px 0;">
                    <a href="{APP_URL}/company/subscription"
                       style="background-color: #dc3545; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; display: inline-block; font-weight: bold;">
                        RÉSOUDRE LE PROBLÈME
                    </a>
                </div>

                <p style="font-size: 12px; color: #6c757d; margin-top: 30px;">
                    Besoin d'aide ? Contactez support@finov-relance.com
                </p>
            </div>
        </body>
        </html>
        """

        return send_email_via_system_config(
            to_email=admin_user.email,
            subject=f"[URGENT] Échec de paiement - {company.name}",
            html_content=html_content
        )

    except Exception as e:
        logger.error(f"Erreur envoi email échec paiement: {str(e)}")
        return False

def send_payment_method_added_notification(company, card_type, last4):
    """PHASE 9: Notification d'ajout de méthode de paiement"""
    try:
        from models import Notification, db

        notification = Notification(
            company_id=company.id,
            title="Nouvelle méthode de paiement",
            message=f"Nouvelle carte {card_type} ****{last4} ajoutée avec succès à votre compte.",
            type='payment_method_added'
        )
        db.session.add(notification)
        db.session.commit()

        current_app.logger.info(f"✅ Notification payment_method_added envoyée pour {company.name}")
        return True

    except Exception as e:
        current_app.logger.error(f"Erreur send_payment_method_added_notification: {str(e)}")
        return False

def send_payment_method_validated_notification(company):
    """PHASE 9: Notification de validation de méthode de paiement"""
    try:
        from models import Notification, db

        notification = Notification(
            company_id=company.id,
            title="Méthode de paiement validée",
            message="Votre méthode de paiement a été validée avec succès. Les paiements futurs seront traités automatiquement.",
            type='payment_method_validated'
        )
        db.session.add(notification)
        db.session.commit()

        current_app.logger.info(f"✅ Notification payment_method_validated envoyée pour {company.name}")
        return True

    except Exception as e:
        current_app.logger.error(f"Erreur send_payment_method_validated_notification: {str(e)}")
        return False

def send_plan_upgrade_notification(company, new_plan, quantity_licenses):
    """
    NOUVEAU: Notification automatique d'upgrade de plan
    Envoie notification in-app + email aux admins de l'entreprise
    """
    try:
        # 1. Trouver tous les admins actifs de l'entreprise
        admin_users = UserCompany.query.filter(
            UserCompany.company_id == company.id,
            UserCompany.role.in_(['admin', 'super_admin']),
            UserCompany.is_active == True
        ).all()

        if not admin_users:
            logger.warning(f"Aucun admin trouvé pour {company.name}")
            return False

        notifications_sent = 0
        emails_sent = 0

        for admin_user_company in admin_users:
            admin_user = admin_user_company.user

            # 2. Notification in-app positive
            notification = send_notification(
                user_id=admin_user.id,
                company_id=company.id,
                type='plan_upgraded',
                title='✅ Plan mis à niveau',
                message=f"Votre plan a été mis à niveau vers {new_plan.display_name} avec {quantity_licenses} licence(s).",
                data={
                    'company_name': company.name,
                    'new_plan': new_plan.display_name,
                    'quantity_licenses': quantity_licenses,
                    'upgrade_date': datetime.utcnow().strftime("%d/%m/%Y à %H:%M"),
                    'action_url': '/company/settings'
                }
            )

            if notification:
                notifications_sent += 1

            # 3. Email de confirmation d'upgrade
            try:
                html_content = f"""
                <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto;">
                    <div style="background-color: #d4edda; padding: 20px; border-radius: 8px; border-left: 4px solid #28a745;">
                        <h2 style="color: #155724; margin-bottom: 20px;">🚀 Plan mis à niveau avec succès</h2>

                        <p>Bonjour {admin_user.first_name} {admin_user.last_name},</p>

                        <p>Votre plan pour <strong>{company.name}</strong> a été mis à niveau avec succès !</p>

                        <div style="background-color: white; padding: 15px; border-radius: 5px; margin: 20px 0;">
                            <h3 style="color: #155724; margin-top: 0;">Détails du nouveau plan :</h3>
                            <ul style="margin-bottom: 0;">
                                <li><strong>Nouveau plan :</strong> {new_plan.display_name}</li>
                                <li><strong>Nombre de licences :</strong> {quantity_licenses}</li>
                                <li><strong>Date d'activation :</strong> Immédiate</li>
                            </ul>
                        </div>

                        <p>✅ <strong>Facturation :</strong> Le montant a été calculé au prorata et facturé immédiatement.</p>
                        <p>✅ <strong>Accès :</strong> Vos nouvelles fonctionnalités sont disponibles dès maintenant.</p>

                        <p>Si vous avez des questions, n'hésitez pas à nous contacter.</p>

                        <p>Cordialement,<br>L'équipe FinovRelance</p>
                    </div>
                </body>
                </html>
                """

                email_sent = send_email_via_system_config(
                    to_email=admin_user.email,
                    subject=f"Plan mis à niveau - {new_plan.display_name} - {company.name}",
                    html_content=html_content
                )

                if email_sent:
                    emails_sent += 1
                    logger.info(f"Email d'upgrade envoyé à {admin_user.email}")

            except Exception as e:
                logger.error(f"Erreur envoi email upgrade à {admin_user.email}: {str(e)}")

        logger.info(f"Notifications upgrade envoyées pour {company.name}: {notifications_sent} notifications, {emails_sent} emails")
        return notifications_sent > 0 or emails_sent > 0

    except Exception as e:
        logger.error(f"Erreur notifications upgrade: {str(e)}")
        return False