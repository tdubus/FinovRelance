"""
Callback OAuth Microsoft simplifié pour EmailConfiguration
Remplace le système chaotique existant
"""
from flask import Blueprint, session, request, redirect, url_for, flash, current_app
from flask_login import current_user
from datetime import datetime, timedelta
from models import EmailConfiguration, SystemEmailConfiguration, db

oauth_callback_bp = Blueprint('oauth_callback', __name__)

@oauth_callback_bp.route('/auth/microsoft/callback')
def microsoft_callback():
    """Callback OAuth Microsoft unifie pour EmailConfiguration."""
    try:
        # Vérifier que l'utilisateur est connecté
        if not current_user.is_authenticated:
            flash('Session expirée. Veuillez vous reconnecter.', 'error')
            return redirect(url_for('auth.login'))

        # Récupérer code et state depuis les paramètres de la requête
        auth_code = request.args.get('code')
        state = request.args.get('state')

        if not auth_code:
            flash('Code d\'autorisation manquant. Veuillez recommencer.', 'error')
            return redirect(url_for('profile.email_configuration'))

        # Vérifier si c'est un flux OAuth système ou utilisateur
        is_system_oauth = session.get('system_email_oauth_flow', False)

        if is_system_oauth:
            # Flux OAuth pour configuration système
            system_config_id = session.get('system_email_config_id')
            if not system_config_id:
                flash('Session OAuth système expirée. Veuillez recommencer.', 'error')
                return redirect(url_for('admin.system_email_configs'))

            return _handle_system_oauth_callback(auth_code, state, system_config_id)
        else:
            # Flux OAuth pour configuration utilisateur (existant)
            company_id = session.get('oauth_company_id')
            target_email = session.get('oauth_target_email')

            if not company_id or not target_email:
                flash('Session OAuth expirée. Veuillez recommencer.', 'error')
                return redirect(url_for('profile.email_configuration'))

            return _handle_user_oauth_callback(auth_code, state, company_id, target_email)

    except Exception as e:
        current_app.logger.error(f"Error in OAuth authentication: {e}")
        flash('Une erreur est survenue. Veuillez reessayer.', 'error')
        return redirect(url_for('profile.email_configuration'))


def _handle_system_oauth_callback(auth_code, state, system_config_id):
    """Handle OAuth callback for system email configuration."""
    try:
        # Récupérer les paramètres du callback
        if not auth_code:
            flash('Code d\'autorisation manquant.', 'error')
            return redirect(url_for('admin.system_email_configs'))

        # Échanger le code contre les tokens
        from microsoft_oauth import MicrosoftOAuthConnector
        connector = MicrosoftOAuthConnector()
        tokens = connector.exchange_code_for_tokens(auth_code, state)

        # Récupérer la configuration système
        system_config = SystemEmailConfiguration.query.get_or_404(system_config_id)

        # Mettre à jour les tokens OAuth
        system_config.outlook_oauth_access_token = tokens['access_token']
        system_config.outlook_oauth_refresh_token = tokens.get('refresh_token')
        # Use real token expiry - refresh mechanism handles re-authentication
        expires_in = tokens.get('expires_in', 3600)
        system_config.outlook_oauth_token_expires = datetime.utcnow() + timedelta(seconds=expires_in)
        system_config.outlook_oauth_connected_at = datetime.utcnow()

        db.session.commit()

        # Nettoyer la session
        session.pop('system_email_config_id', None)
        session.pop('system_email_oauth_flow', None)

        flash(f'Configuration OAuth mise à jour avec succès pour "{system_config.config_name}".', 'success')
        return redirect(url_for('admin.system_email_configs'))

    except Exception as e:
        current_app.logger.error(f"Error in system OAuth configuration: {e}")
        flash('Une erreur est survenue lors de la configuration OAuth. Veuillez reessayer.', 'error')
        return redirect(url_for('admin.system_email_configs'))


def _handle_user_oauth_callback(auth_code, state, company_id, target_email):
    """Handle OAuth callback for user email configuration."""
    try:
        # Récupérer les paramètres du callback
        if not auth_code:
            flash('Code d\'autorisation manquant.', 'error')
            return redirect(url_for('profile.email_configuration'))

        # Échanger le code contre les tokens
        from microsoft_oauth import MicrosoftOAuthConnector
        connector = MicrosoftOAuthConnector()
        tokens = connector.exchange_code_for_tokens(auth_code, state)

        # Récupérer ou créer EmailConfiguration
        email_config = EmailConfiguration.query.filter_by(
            user_id=current_user.id,
            company_id=company_id
        ).first()

        # Effacer TOUS les tokens Gmail (même expirés) pour garantir l'exclusivité mutuelle
        if email_config and (email_config.gmail_oauth_access_token or email_config.gmail_email):
            email_config.gmail_oauth_access_token = None
            email_config.gmail_oauth_refresh_token = None
            email_config.gmail_oauth_token_expires = None
            email_config.gmail_oauth_connected_at = None
            email_config.gmail_email = None

        if not email_config:
            email_config = EmailConfiguration()
            email_config.user_id = current_user.id
            email_config.company_id = company_id
            email_config.outlook_email = target_email
            db.session.add(email_config)

        # Sauvegarder les tokens OAuth ET mettre à jour l'email si différent
        email_config.outlook_oauth_access_token = tokens['access_token']
        email_config.outlook_oauth_refresh_token = tokens['refresh_token']
        # Use real token expiry - refresh mechanism handles re-authentication
        expires_in = tokens.get('expires_in', 3600)
        email_config.outlook_oauth_token_expires = datetime.now() + timedelta(seconds=expires_in)
        email_config.outlook_oauth_connected_at = datetime.now()

        # Mettre à jour l'email avec celui de la session si différent
        if email_config.outlook_email != target_email:
            email_config.outlook_email = target_email

        db.session.commit()

        # Nettoyer la session
        session.pop('oauth_company_id', None)
        session.pop('oauth_target_email', None)
        session.pop('oauth_state', None)

        flash(f'Connexion Microsoft réussie pour {target_email}', 'success')
        return redirect(url_for('profile.email_configuration'))

    except Exception as e:
        current_app.logger.error(f"Error in user OAuth connection: {e}")
        flash('Une erreur est survenue lors de la connexion OAuth. Veuillez reessayer.', 'error')
        return redirect(url_for('profile.email_configuration'))