"""
Gestionnaire d'erreurs d'authentification Business Central
"""
import logging
logger = logging.getLogger(__name__)

def handle_auth_error(connection):
    """Gestion des erreurs d'authentification - Notification immédiate aux super admins"""
    try:
        from models import Notification, User, UserCompany
        from app import db

        if not connection:
            return

        # Trouver les super admins de cette entreprise
        super_admins = User.query.join(UserCompany).filter(
            UserCompany.company_id == connection.company_id,
            UserCompany.role == 'super_admin',
            UserCompany.is_active == True
        ).all()

        for admin in super_admins:
            Notification.create_notification(
                user_id=admin.id,
                company_id=connection.company_id,
                type='business_central_auth_error_critical',
                title='🔴 URGENT: Business Central déconnecté',
                message=f'La synchronisation Business Central a échoué à cause d\'un problème d\'authentification. Le token a expiré ou été révoqué par Microsoft. Reconnexion immédiate requise dans les paramètres de l\'entreprise.'
            )

        # Désactiver temporairement la connexion pour éviter les tentatives répétées
        connection.is_active = False
        db.session.commit()

        logger.error(f"🔴 Connexion Business Central {connection.id} désactivée - notifications envoyées aux super admins")

    except Exception as e:
        logger.error(f"Erreur lors de la gestion de l'erreur d'authentification: {e}")