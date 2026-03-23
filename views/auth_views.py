# Auth Views Module - Extracted from views.py
# Contains all authentication-related routes and functions
# PRESERVED: All logic, imports, decorators, and functionality from original views.py

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin
from utils import send_password_reset_email
from utils.consent_helper import log_terms_consent, log_privacy_consent
from utils.audit_service import log_login, log_2fa, log_action, AuditActions, EntityTypes
import os
import threading
import logging
from app import db, limiter
from models import User, Company, UserCompany, Plan

logger = logging.getLogger(__name__)


def _send_2fa_async(app, to_email, first_name, code, ip_address):
    """Send 2FA code in a background thread. If this fails, the user can resend."""
    try:
        with app.app_context():
            from email_fallback import send_2fa_code_via_smtp
            send_2fa_code_via_smtp(to_email, first_name, code, ip_address)
            logger.info(f"2FA code sent asynchronously to {to_email}")
    except Exception as e:
        logger.error(f"Async 2FA send failed for {to_email}: {e}")

def is_safe_url(target):
    """Validate that the redirect URL is relative (same host) to prevent open redirects."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

# Create auth blueprint
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")  # Max 10 tentatives de connexion par minute
def login():
    """User login with 2FA"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    from forms import LoginForm
    from models import User, TwoFactorAuth

    form = LoginForm()

    # Check if email is pre-filled from URL parameter
    email_param = request.args.get('email')
    if email_param and not form.email.data:
        form.email.data = email_param

    if form.validate_on_submit():
        try:
            user = User.query.filter_by(email=form.email.data).first()

            if user and user.password_hash and form.password.data and check_password_hash(
                    user.password_hash, form.password.data):
                log_login(success=True, email=form.email.data, user=user)

                # SÉCURITÉ ÉTAPE 9 : Log sécurisé du succès de connexion
                from utils.secure_logging import create_secure_log_message
                success_message = create_secure_log_message(
                    "Successful password verification",
                    user_email=form.email.data,
                    user_id=user.id)
                current_app.logger.info(success_message)
                # Get IP address and user agent for security logging
                ip_address = request.environ.get(
                    'HTTP_X_FORWARDED_FOR', request.environ.get('REMOTE_ADDR'))
                user_agent = request.headers.get('User-Agent', 'Unknown')

                # SÉCURITÉ ÉTAPE 9 : Logging sécurisé avec masquage des données sensibles
                from utils.secure_logging import create_secure_log_message
                secure_message = create_secure_log_message(
                    "User login attempt",
                    user_email=form.email.data,
                    user_id=user.id,
                    ip_address=ip_address)
                current_app.logger.info(secure_message)

                # Check if user has TOTP active — skip email 2FA entirely
                from models import UserTOTP
                user_totp = UserTOTP.query.filter_by(user_id=user.id, is_active=True).first()

                if user_totp:
                    # TOTP active: skip email, go straight to verification
                    session['pending_2fa_user_id'] = user.id
                    session['pending_2fa_remember_me'] = form.remember_me.data
                    session['2fa_totp_only'] = True
                    next_page = request.args.get('next')
                    if next_page:
                        session['pending_2fa_next_page'] = next_page
                    flash(
                        'Entrez le code de votre application d\'authentification.',
                        'info')
                    return redirect(url_for('auth.verify_2fa'))

                # No TOTP — send 2FA code via email
                try:
                    # Anti-duplication: si un code valide a été créé dans les 30 dernières
                    # secondes, ne pas en créer un nouveau ni renvoyer un email.
                    recent_code = TwoFactorAuth.query.filter_by(
                        user_id=user.id, used=False
                    ).filter(
                        TwoFactorAuth.created_at > datetime.utcnow() - timedelta(seconds=30)
                    ).first()

                    if recent_code:
                        current_app.logger.warning(
                            f"Duplicate 2FA request detected for user {user.email} "
                            f"(code created at {recent_code.created_at}), skipping new send."
                        )
                        session['pending_2fa_user_id'] = user.id
                        session['pending_2fa_remember_me'] = form.remember_me.data
                        next_page = request.args.get('next')
                        if next_page:
                            session['pending_2fa_next_page'] = next_page
                        flash(
                            'Un code de vérification a été envoyé à votre adresse courriel.',
                            'success')
                        return redirect(url_for('auth.verify_2fa'))

                    two_factor_code = TwoFactorAuth.create_2fa_code(
                        user, ip_address, user_agent)

                    # Set session BEFORE starting email thread (avoid race condition)
                    session['pending_2fa_user_id'] = user.id
                    session['pending_2fa_remember_me'] = form.remember_me.data

                    next_page = request.args.get('next')
                    if next_page:
                        session['pending_2fa_next_page'] = next_page

                    # Send 2FA code asynchronously via background thread
                    # The code is already stored in DB — if the email fails,
                    # the user can request a resend from the verify_2fa page.
                    app = current_app._get_current_object()
                    t = threading.Thread(
                        target=_send_2fa_async,
                        args=(app, user.email, user.first_name,
                              two_factor_code.code, ip_address),
                        daemon=True
                    )
                    t.start()

                    log_2fa(AuditActions.TWO_FA_SENT, email=user.email, user=user)
                    flash(
                        'Un code de vérification a été envoyé à votre adresse courriel.',
                        'success')
                    return redirect(url_for('auth.verify_2fa'))

                except Exception as e:
                    current_app.logger.error(f"2FA email send failed: {e}")
                    from models import RecoveryCode
                    # Check if user has recovery codes
                    has_recovery = RecoveryCode.query.filter_by(user_id=user.id, used=False).count() > 0
                    session['pending_2fa_user_id'] = user.id
                    session['pending_2fa_remember_me'] = form.remember_me.data if hasattr(form, 'remember_me') else False
                    session['2fa_email_failed'] = True

                    next_page = request.args.get('next')
                    if next_page:
                        session['pending_2fa_next_page'] = next_page

                    flash("Le systeme d'envoi de courriel est temporairement indisponible.", 'warning')
                    if has_recovery:
                        flash("Utilisez un de vos codes de secours pour vous connecter.", 'info')
                    else:
                        flash("Vous n'avez pas de codes de secours configures. Veuillez reessayer plus tard.", 'error')
                    return redirect(url_for('auth.verify_2fa'))
            else:
                log_login(success=False, email=form.email.data, reason='invalid_credentials')
                flash('Courriel ou mot de passe incorrect.', 'error')
        except Exception as critical_error:
            import traceback
            current_app.logger.error(
                f"Critical login error: {str(critical_error)}\n{traceback.format_exc()}")
            flash('Erreur système lors de la connexion. Veuillez réessayer.',
                  'error')

    return render_template('auth/login.html', form=form)


@auth_bp.route('/verify-2fa', methods=['GET', 'POST'])
@limiter.limit("15 per minute"
               )  # Max 15 tentatives 2FA par minute (plus généreux que login)
def verify_2fa():
    """Verify 2FA code"""
    from forms import TwoFactorForm
    from models import User, TwoFactorAuth
    from app import db
    from datetime import datetime

    # Check if user is pending 2FA verification
    pending_user_id = session.get('pending_2fa_user_id')
    if not pending_user_id:
        flash('Session expirée. Veuillez vous reconnecter.', 'error')
        return redirect(url_for('auth.login'))

    user = User.query.get(pending_user_id)
    if not user:
        session.pop('pending_2fa_user_id', None)
        flash('Utilisateur introuvable. Veuillez vous reconnecter.', 'error')
        return redirect(url_for('auth.login'))

    form = TwoFactorForm()

    if form.validate_on_submit():
        from models import RecoveryCode, UserTOTP
        valid_code = False

        # 1. Check TOTP first (always works, even when email is down)
        user_totp = UserTOTP.query.filter_by(user_id=user.id, is_active=True).first()
        if user_totp and user_totp.verify(form.code.data):
            valid_code = True

        # 2. Try recovery code if email failed
        if not valid_code and session.get('2fa_email_failed'):
            if RecoveryCode.verify_code(user.id, form.code.data):
                valid_code = True
                remaining = RecoveryCode.query.filter_by(user_id=user.id, used=False).count()
                if remaining <= 1:
                    flash(f"Attention : il vous reste {remaining} code(s) de secours. Regenerez-en dans votre profil.", 'warning')
            else:
                # Also try normal 2FA code (maybe email recovered)
                two_factor_code = TwoFactorAuth.find_valid_code(user.id, form.code.data)
                if two_factor_code:
                    two_factor_code.mark_as_used()
                    valid_code = True

        # 3. Normal flow: try recovery code, then email 2FA code
        if not valid_code and not session.get('2fa_email_failed'):
            if RecoveryCode.verify_code(user.id, form.code.data):
                valid_code = True
                remaining = RecoveryCode.query.filter_by(user_id=user.id, used=False).count()
                if remaining <= 1:
                    flash(f"Attention : il vous reste {remaining} code(s) de secours. Regenerez-en dans votre profil.", 'warning')
            else:
                two_factor_code = TwoFactorAuth.find_valid_code(user.id, form.code.data)
                if two_factor_code:
                    two_factor_code.mark_as_used()
                    valid_code = True

        if valid_code:
            log_2fa(AuditActions.TWO_FA_SUCCESS, email=user.email, success=True, user=user)

            # Prevent session fixation: regenerate session while preserving 2FA data
            pending_data = {
                'pending_2fa_user_id': session.get('pending_2fa_user_id'),
                'pending_2fa_remember_me': session.get('pending_2fa_remember_me'),
                'pending_2fa_next_page': session.get('pending_2fa_next_page'),
            }
            session.clear()
            session.update({k: v for k, v in pending_data.items() if v is not None})

            # Now actually log the user in
            remember_me = session.pop('pending_2fa_remember_me', False)
            login_user(user, remember=remember_me)

            # For permanent sessions (remember me), extend session to 7 days
            if remember_me:
                from datetime import timedelta
                session.permanent = True
                current_app.permanent_session_lifetime = timedelta(days=7)

            # Update last login timestamp
            user.last_login = datetime.utcnow()
            db.session.commit()

            # Clear 2FA session data
            session.pop('pending_2fa_user_id', None)
            session.pop('2fa_email_failed', None)

            # Check if password change is required
            if user.must_change_password:
                flash(
                    'Vous devez changer votre mot de passe avant de continuer.',
                    'warning')
                return redirect(url_for('auth.change_password'))

            # Set default company for session (prefer last used company)
            companies = user.get_companies()
            if companies:
                selected = companies[0]
                if user.last_company_id:
                    last = next((c for c in companies if c.id == user.last_company_id), None)
                    if last:
                        selected = last
                session['selected_company_id'] = selected.id
                flash(f'Connecté avec succès. Entreprise: {selected.name}',
                      'success')
            else:
                flash('Vous ne faites partie d\'aucune entreprise, veuillez contacter l\'administrateur de votre compte.', 'error')
                return redirect(url_for('auth.logout'))

            # Vérifier s'il y a une action post-connexion à effectuer
            post_login_action = session.pop('post_login_action', None)
            if post_login_action == 'add_company':
                session.pop('add_company_email', None)  # Nettoyer
                flash(
                    'Connecté avec succès ! Vous pouvez maintenant ajouter une nouvelle entreprise.',
                    'success')
                return redirect(url_for('onboarding.add_company_plan_selection'))

            # Redirect to intended page or dashboard
            next_page = session.pop('pending_2fa_next_page', None)
            if next_page and is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for('main.dashboard'))
        else:
            log_2fa(AuditActions.TWO_FA_FAILED, email=user.email, success=False, user=user)
            flash('Code de vérification invalide ou expiré.', 'error')

    return render_template('auth/verify_2fa.html', form=form, user=user)


@auth_bp.route('/resend-2fa', methods=['POST'])
@limiter.limit("3 per minute")  # Max 3 demandes de renvoi par minute
def resend_2fa():
    """Resend 2FA code"""
    from models import User, TwoFactorAuth
    from email_fallback import send_2fa_code_via_smtp

    # Check if user is pending 2FA verification
    pending_user_id = session.get('pending_2fa_user_id')
    if not pending_user_id:
        flash('Session expirée. Veuillez vous reconnecter.', 'error')
        return redirect(url_for('auth.login'))

    user = User.query.get(pending_user_id)
    if not user:
        session.pop('pending_2fa_user_id', None)
        flash('Utilisateur introuvable. Veuillez vous reconnecter.', 'error')
        return redirect(url_for('auth.login'))

    try:
        # Get IP address and user agent for security logging
        ip_address = request.environ.get('HTTP_X_FORWARDED_FOR',
                                         request.environ.get('REMOTE_ADDR'))
        user_agent = request.headers.get('User-Agent', 'Unknown')

        # Create new 2FA code
        two_factor_code = TwoFactorAuth.create_2fa_code(
            user, ip_address, user_agent)

        # Send 2FA code via email
        send_2fa_code_via_smtp(user.email, user.first_name,
                               two_factor_code.code, ip_address)

        # Switch from TOTP-only to email mode
        session.pop('2fa_totp_only', None)
        flash(
            'Un nouveau code de vérification a été envoyé à votre adresse courriel.',
            'success')

    except Exception as e:
        current_app.logger.error(f"Failed to resend 2FA code: {e}")
        flash(
            'Erreur lors de l\'envoi du code. Veuillez réessayer.',
            'error')

    return redirect(url_for('auth.verify_2fa'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per minute")  # Max 5 demandes de reset par minute
def forgot_password():
    """Request password reset"""
    from forms import ForgotPasswordForm
    from models import User, PasswordResetToken
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            # Create reset token
            reset_token = PasswordResetToken.create_reset_token(user)

            # Send reset email
            reset_url = url_for('auth.reset_password',
                                token=reset_token.token,
                                _external=True)

            # Use existing Microsoft email system
            try:
                send_password_reset_email(user.email, user.first_name,
                                          reset_url)
                log_action(AuditActions.PASSWORD_RESET_REQUESTED, entity_type=EntityTypes.USER,
                          entity_id=user.id, details={'email': user.email})
                flash(
                    'Un lien de réinitialisation a été envoyé à votre adresse email.',
                    'success')
            except Exception as e:
                current_app.logger.error(f"Failed to send reset email: {e}")
                flash(
                    'Erreur lors de l\'envoi de l\'email. Veuillez réessayer.',
                    'error')
        else:
            # Always show success message for security (don't reveal if email exists)
            flash(
                'Un lien de réinitialisation a été envoyé à votre adresse email.',
                'success')

        return redirect(url_for('auth.login'))

    return render_template('auth/forgot_password.html', form=form)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per minute")  # Max 10 tentatives de reset par minute
def reset_password(token):
    """Reset password with token"""
    from app import db
    from forms import ResetPasswordForm
    from models import PasswordResetToken

    reset_token = PasswordResetToken.query.filter_by(token=token).first()

    if not reset_token or not reset_token.is_valid():
        flash('Le lien de réinitialisation est invalide ou expiré.', 'error')
        return redirect(url_for('auth.login'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        # Update user password
        user = reset_token.user
        user.password_hash = generate_password_hash(form.password.data or '')

        # Mark token as used
        reset_token.mark_as_used()

        db.session.commit()

        log_action(AuditActions.PASSWORD_CHANGED, entity_type=EntityTypes.USER,
                  entity_id=user.id, details={'email': user.email, 'method': 'reset_token'})

        flash('Votre mot de passe a été réinitialisé avec succès.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', form=form)


@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit("20 per minute")  # Max 20 tentatives d'inscription par minute
def register():
    """Page d'inscription - étape 1 : choisir le type de compte"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    from forms import AccountTypeForm
    from models import User
    form = AccountTypeForm()

    if form.validate_on_submit():
        # Vérifier si l'email existe déjà
        existing_user = User.query.filter_by(email=form.email.data).first()

        if form.account_type.data == 'new_account':
            # L'utilisateur veut créer un nouveau compte
            if existing_user:
                # Vérifier si l'utilisateur a encore des entreprises actives
                active_companies = [
                    uc for uc in existing_user.user_companies if uc.is_active
                ]

                if active_companies:
                    # Email existe déjà et a des entreprises, suggérer le bon parcours
                    flash(
                        f'Un compte existe déjà avec l\'email {form.email.data}. Vous devez sélectionner "Nouvelle entreprise pour mon compte existant" à la place.',
                        'warning')
                    return render_template('auth/register_type.html',
                                           form=form,
                                           email_exists=True,
                                           existing_email=form.email.data)
                else:
                    # L'utilisateur existe mais n'a plus d'entreprises actives
                    # Permettre la création d'un nouveau compte (suppression de l'ancien)
                    try:
                        from app import db
                        # Supprimer l'ancien compte utilisateur sans entreprise
                        db.session.delete(existing_user)
                        db.session.commit()
                        current_app.logger.info(
                            f"Suppression compte orphelin pour {form.email.data}"
                        )
                    except Exception as e:
                        current_app.logger.error(
                            f"Erreur suppression compte orphelin: {str(e)}")
                        from app import db
                        db.session.rollback()
                        flash(
                            'Erreur lors de la préparation du compte. Veuillez réessayer.',
                            'error')
                        return redirect(url_for('auth.register'))

                    # Continuer avec la création du nouveau compte
                    pass

            return redirect(url_for('marketing.tarifs'))
        else:
            # L'utilisateur veut créer une nouvelle entreprise
            if existing_user:
                # Rediriger vers la page de connexion avec l'email pré-rempli
                # Stocker l'intention d'ajouter une entreprise dans la session
                session['post_login_action'] = 'add_company'
                session['add_company_email'] = form.email.data
                return redirect(url_for('auth.login', email=form.email.data))
            else:
                flash(
                    f'Aucun compte trouvé avec l\'email {form.email.data}. Vous devez sélectionner "Nouveau compte utilisateur" à la place.',
                    'warning')
                return render_template('auth/register_type.html',
                                       form=form,
                                       email_not_exists=True,
                                       entered_email=form.email.data)

    return render_template('auth/register_type.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    """User logout"""
    user_email = current_user.email if current_user else None
    log_action(AuditActions.LOGOUT, entity_type=EntityTypes.USER, details={'email': user_email})

    # Clear selected company from session
    session.pop('selected_company_id', None)
    logout_user()
    flash('Vous avez été déconnecté.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/business-central/logout')
def business_central_logout():
    """Business Central logout endpoint - utilisé par Microsoft lors de la déconnexion"""
    # Nettoyer toutes les sessions Business Central
    session.pop('bc_company_id', None)
    session.pop('bc_state', None)
    session.pop('bc_customers_url', None)
    session.pop('bc_invoices_url', None)
    session.pop('bc_oauth_code', None)
    session.pop('bc_oauth_state', None)

    # Si l'utilisateur est connecté, le rediriger vers le dashboard
    if current_user.is_authenticated:
        flash('Session Business Central fermée.', 'info')
        return redirect(url_for('main.dashboard'))
    else:
        # Sinon rediriger vers la page de connexion
        return redirect(url_for('auth.login'))


@auth_bp.route('/switch-company/<int:company_id>')
@login_required
def switch_company(company_id):
    """Switch to a different company"""
    from models import UserCompany, Company

    # Verify user has access to this company
    user_company = UserCompany.query.filter_by(user_id=current_user.id,
                                               company_id=company_id,
                                               is_active=True).first()

    if not user_company:
        flash('Accès refusé à cette entreprise.', 'error')
        return redirect(url_for('main.dashboard'))

    # SÉCURITÉ: Vérifier que la company existe AVANT de mettre dans session
    company = Company.query.get(company_id)
    if not company:
        current_app.logger.error(
            f"SÉCURITÉ: Company {company_id} n'existe pas mais UserCompany existe - incohérence BD"
        )
        flash('Entreprise introuvable - veuillez contacter le support.',
              'error')
        return redirect(url_for('main.dashboard'))

    # Update session seulement si la company existe
    session['selected_company_id'] = company_id

    current_user.last_company_id = company_id
    db.session.commit()

    flash(f'Basculé vers {company.name}', 'success')
    return redirect(url_for('main.dashboard'))


@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Force password change for new users"""
    from app import db
    from forms import ChangePasswordForm

    if not current_user.must_change_password:
        return redirect(url_for('main.dashboard'))

    form = ChangePasswordForm()
    form.set_current_user(current_user)

    if form.validate_on_submit():
        current_user.password_hash = generate_password_hash(
            form.new_password.data or '')
        current_user.must_change_password = False
        db.session.commit()

        flash('Mot de passe changé avec succès.', 'success')
        return redirect(url_for('main.dashboard'))

    return render_template('auth/change_password.html', form=form)


@auth_bp.route('/api/log-cookie-consent', methods=['POST'])
def log_cookie_consent_api():
    """API endpoint pour enregistrer le consentement aux cookies (RGPD/Loi 25)"""
    from flask import jsonify
    from utils.consent_helper import log_cookies_consent

    try:
        data = request.get_json()
        if not data or 'accepted' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing accepted field'
            }), 400

        accepted = data['accepted']

        # Si l'utilisateur est connecté, enregistrer avec son ID
        user_id = current_user.id if current_user.is_authenticated else None

        # Enregistrer le consentement
        log_cookies_consent(user_id, accepted)
        db.session.commit()

        return jsonify({'success': True}), 200

    except Exception as e:
        current_app.logger.error(
            f"Erreur enregistrement consentement cookies: {e}")
        return jsonify({'success': False, 'error': 'Une erreur interne est survenue'}), 500


@auth_bp.route('/api/accept-existing-user-consent', methods=['POST'])
@login_required
def accept_existing_user_consent():
    """API endpoint pour enregistrer le consentement des utilisateurs existants (RGPD/Loi 25)"""
    from flask import jsonify
    from utils.consent_helper import log_terms_consent, log_privacy_consent, log_cookies_consent

    try:
        data = request.get_json()
        if not data or 'accepted' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing accepted field'
            }), 400

        accepted = data['accepted']

        if not accepted:
            return jsonify({
                'success': False,
                'error': 'Consent must be accepted'
            }), 400

        # Enregistrer les consentements CGU, confidentialité et cookies
        log_terms_consent(current_user.id, accepted=True)
        log_privacy_consent(current_user.id, accepted=True)
        log_cookies_consent(current_user.id, accepted=True)
        db.session.commit()

        current_app.logger.info(
            f"Consentements CGU, confidentialité et cookies enregistrés pour utilisateur existant {current_user.id}"
        )

        return jsonify({'success': True}), 200

    except Exception as e:
        current_app.logger.error(
            f"Erreur enregistrement consentements utilisateur existant: {e}"
        )
        return jsonify({'success': False, 'error': 'Une erreur interne est survenue'}), 500


@auth_bp.route('/initial-recovery-codes')
@login_required
def show_initial_recovery_codes():
    """Show recovery codes generated during registration (one-time display)."""
    codes = session.pop('new_user_recovery_codes', None)
    if not codes:
        return redirect(url_for('main.dashboard'))
    return render_template('auth/recovery_codes.html', recovery_codes=codes)


@auth_bp.route('/generate-recovery-codes', methods=['GET', 'POST'])
@login_required
def generate_recovery_codes():
    """Generate new recovery codes for the current user."""
    from models import RecoveryCode

    if request.method == 'POST':
        codes = RecoveryCode.generate_codes(current_user.id)
        flash('Nouveaux codes de secours generes. Conservez-les en lieu sur.', 'success')
        return render_template('auth/recovery_codes.html', recovery_codes=codes)

    # GET: show confirmation page
    existing_count = RecoveryCode.query.filter_by(user_id=current_user.id, used=False).count()
    return render_template('auth/generate_recovery_codes.html', existing_count=existing_count)


@auth_bp.route('/setup-totp', methods=['GET', 'POST'])
@login_required
def setup_totp():
    """Setup TOTP (Google Authenticator) for the current user."""
    from models import UserTOTP, RecoveryCode

    existing = UserTOTP.query.filter_by(user_id=current_user.id, is_active=True).first()
    if existing:
        flash('TOTP deja active.', 'info')
        return redirect(url_for('profile.settings'))

    if request.method == 'GET':
        import pyotp
        import qrcode
        import io
        import base64

        secret = pyotp.random_base32()
        session['totp_setup_secret'] = secret
        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name=current_user.email, issuer_name='Finov Relance')

        # Generate QR code
        img = qrcode.make(uri)
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()

        return render_template('auth/setup_totp.html', qr_base64=qr_base64, secret=secret)

    # POST: verify code to activate
    code = request.form.get('code', '')
    secret = session.get('totp_setup_secret')
    if not secret:
        flash('Session expiree, veuillez recommencer.', 'error')
        return redirect(url_for('auth.setup_totp'))

    import pyotp
    totp = pyotp.TOTP(secret)
    if totp.verify(code, valid_window=1):
        user_totp = UserTOTP(user_id=current_user.id)
        user_totp.secret = secret
        user_totp.is_active = True
        db.session.add(user_totp)
        db.session.commit()
        session.pop('totp_setup_secret', None)

        # Generate recovery codes
        codes = RecoveryCode.generate_codes(current_user.id)
        flash('Authentification TOTP activee avec succes.', 'success')
        return render_template('auth/totp_activated.html', recovery_codes=codes)
    else:
        flash('Code invalide. Verifiez votre application et reessayez.', 'error')
        return redirect(url_for('auth.setup_totp'))


@auth_bp.route('/disable-totp', methods=['POST'])
@login_required
def disable_totp():
    """Disable TOTP for the current user (requires password confirmation)."""
    from models import UserTOTP

    password = request.form.get('password', '')
    if not check_password_hash(current_user.password_hash, password):
        flash('Mot de passe incorrect.', 'error')
        return redirect(url_for('profile.settings'))

    user_totp = UserTOTP.query.filter_by(user_id=current_user.id).first()
    if user_totp:
        db.session.delete(user_totp)
        db.session.commit()
    flash('TOTP desactive.', 'success')
    return redirect(url_for('profile.settings'))
