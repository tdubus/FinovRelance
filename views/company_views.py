# Company Views Module - Extracted from views.py
# Contains all company-related routes and functions
# PRESERVED: All logic, imports, decorators, and functionality from original views.py

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app, jsonify
from flask_login import login_required, current_user
import stripe
import os
from app import limiter
from utils.audit_service import log_action, log_user_action, log_sync_action, AuditActions, EntityTypes

# Create company blueprint
company_bp = Blueprint('company', __name__, url_prefix='/company')


def get_stripe_license_periods(company):
    """
    REFONTE STRIPE 2.0 : Récupérer les données de licences (architecture simplifiée)
    Retourne le nombre de licences actuel et programmé depuis Stripe
    """
    try:
        # REFONTE STRIPE 2.0 : Valeurs par défaut simplifiées
        current_licenses = company.quantity_licenses or 1
        next_licenses = company.quantity_licenses or 1

        if not company.stripe_subscription_id:
            current_app.logger.info(f"Aucun abonnement Stripe pour {company.name} - utilisation valeurs locales V2")
            return current_licenses, next_licenses

        # Récupérer l'abonnement Stripe avec les vraies données
        subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)
        current_app.logger.info(f"Récupération abonnement Stripe V2 pour UX: {subscription.id}")

        # REFONTE STRIPE 2.0 : Architecture simplifiée - pas de grâce complexe
        # Période en cours = quantités actuelles depuis Stripe
        stripe_licenses = company.get_stripe_programmed_quantities()
        current_licenses = stripe_licenses
        next_licenses = stripe_licenses

        current_app.logger.info(f"UX V2 - Licences actuelles et prochaines: {current_licenses}")

        return current_licenses, next_licenses

    except Exception as e:
        current_app.logger.error(f"Erreur récupération données UX Stripe V2: {str(e)}")
        # En cas d'erreur, retourner les valeurs par défaut
        default_licenses = company.quantity_licenses or 1
        return default_licenses, default_licenses


def get_stripe_subscription_quantities(subscription):
    """REFONTE STRIPE 2.0 : Extraire la quantité de licences depuis un abonnement Stripe (architecture simplifiée)"""
    try:
        from utils import _get_stripe_items_safely
        items_data = _get_stripe_items_safely(subscription)

        # Récupérer les prix depuis la company pour identifier les items
        company = current_user.get_selected_company()
        if not company or not company.plan_ref:
            return 1

        stripe_price_id = company.plan_ref.stripe_price_id

        for item in items_data:
            from stripe_finov.webhooks.helpers import get_item_price_id, get_item_quantity
            item_price_id = get_item_price_id(item)
            if item_price_id == stripe_price_id:
                item_quantity = get_item_quantity(item)
                current_app.logger.info(f"Quantité extraite de Stripe V2: {item_quantity} licences")
                return item_quantity

        current_app.logger.info(f"Aucun item correspondant trouvé dans Stripe, utilisation valeur par défaut: 1")
        return 1

    except Exception as e:
        current_app.logger.error(f"Erreur extraction quantités Stripe V2: {str(e)}")
        return 1

@company_bp.route('/subscription')
@login_required
def subscription():
    """Page moderne de gestion des abonnements"""
    from app import db
    from models import Company, Plan, UserCompany

    # Récupérer l'entreprise actuelle et les plans
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée', 'error')
        return redirect(url_for('main.dashboard'))

    plans = Plan.query.filter_by(is_active=True).all()

    # Préparer les données d'abonnement côté serveur pour éviter les problèmes d'API
    try:
        # Obtenir les licences via la méthode centralisée
        license_data = company.get_license_counts('stripe')
        stripe_licenses = {'admin': license_data['admin'], 'employee': license_data['employee']}  # NOTE: Stripe API utilise 'employee' en anglais
        local_licenses = company.get_license_counts('local')
        grace_licenses = company.get_license_counts('grace')
        effective_licenses = company.get_effective_license_counts()

        # Informations sur l'abonnement Stripe
        subscription_info = None
        if company.stripe_subscription_id:
            try:
                subscription_info = stripe.Subscription.retrieve(company.stripe_subscription_id)
            except Exception as e:
                current_app.logger.error(f"Erreur lors de la récupération de l'abonnement Stripe: {e}")
                subscription_info = None

        # Calculer les métriques pour l'interface
        total_effective = effective_licenses['admin'] + effective_licenses['employee']  # NOTE: Ces variables viennent de Stripe
        total_stripe = stripe_licenses['admin'] + stripe_licenses['employee']  # NOTE: Ces variables viennent de Stripe

        # Déterminer s'il y a une période de grâce active
        has_grace_period = grace_licenses['admin'] > 0 or grace_licenses['employee'] > 0  # NOTE: Ces variables viennent de Stripe

        return render_template('company/subscription.html',
                             company=company,
                             plans=plans,
                             stripe_licenses=stripe_licenses,
                             local_licenses=local_licenses,
                             grace_licenses=grace_licenses,
                             effective_licenses=effective_licenses,
                             subscription_info=subscription_info,
                             total_effective=total_effective,
                             total_stripe=total_stripe,
                             has_grace_period=has_grace_period)

    except Exception as e:
        current_app.logger.error(f"Erreur lors de la récupération des données d'abonnement: {e}")
        flash('Erreur lors du chargement des informations d\'abonnement.', 'error')
        return redirect(url_for('main.dashboard'))


def build_accounting_connector_context(company):
    """
    Helper function to normalize accounting connector data for UI display.
    Returns a dict with 'selected_connector' and 'available_connectors'.

    Selection logic:
    1. Active API connection (QuickBooks, Xero, Business Central) with most recent sync
    2. Configured Excel/CSV mapping if no API connection
    3. None if no connector is configured
    """
    from models import AccountingConnection, FileImportMapping

    all_connectors = []
    selected_connector = None

    # Get all API connections for this company
    # CRITICAL: nullslast() ensures connections WITH recent sync are prioritized
    api_connections = AccountingConnection.query.filter_by(
        company_id=company.id,
        is_active=True
    ).order_by(AccountingConnection.last_sync_at.desc().nullslast()).all()

    # Get Excel/CSV mapping
    file_mapping = FileImportMapping.query.filter_by(company_id=company.id).first()

    # Build normalized connector objects for API connections
    for conn in api_connections:
        connector_data = {
            'type': conn.system_type,  # 'quickbooks', 'xero', 'business_central', 'odoo'
            'name': {
                'quickbooks': 'QuickBooks Online',
                'xero': 'Xero Accounting',
                'business_central': 'Microsoft Business Central',
                'odoo': 'Odoo ERP'
            }.get(conn.system_type, conn.system_type),
            'logo': f"{conn.system_type.replace('_', '-')}-logo.png",
            'is_configured': conn.is_active,
            'is_active': conn.is_active,
            'last_sync_at': conn.last_sync_at,
            'sync_stats': conn.sync_stats,
            'connection_id': conn.id,
            'connection_object': conn,
            'description': {
                'quickbooks': 'Synchronisez automatiquement vos clients et factures depuis QuickBooks',
                'xero': 'Synchronisez automatiquement vos clients et factures depuis Xero',
                'business_central': 'Synchronisez automatiquement vos clients et factures via OData V4',
                'odoo': 'Synchronisez automatiquement vos clients et factures depuis Odoo via XML-RPC'
            }.get(conn.system_type, '')
        }

        # The first active API connection (most recent sync) is selected
        if conn.is_active and selected_connector is None:
            selected_connector = connector_data
        else:
            all_connectors.append(connector_data)

    # Add Excel/CSV connector
    # Consider configured if at least ONE mapping (clients OR invoices) is set
    has_any_mapping = file_mapping and (file_mapping.client_column_mappings or file_mapping.invoice_column_mappings)
    excel_csv_connector = {
        'type': 'file_import',
        'name': 'Import Excel/CSV',
        'logo': None,  # Uses icon instead
        'is_configured': has_any_mapping,
        'is_active': False,  # File import is not "active" like API connections
        'last_sync_at': None,
        'sync_stats': None,
        'connection_id': None,
        'mapping_object': file_mapping,
        'description': 'Importez vos clients et factures depuis Excel ou CSV avec mapping personnalisé'
    }

    # If no API connection is selected but Excel/CSV has at least one mapping, select it
    if selected_connector is None and has_any_mapping:
        selected_connector = excel_csv_connector
    else:
        all_connectors.append(excel_csv_connector)

    # Add non-connected API connectors
    connected_types = [conn.system_type for conn in api_connections]
    for system_type in ['quickbooks', 'xero', 'business_central', 'odoo']:
        if system_type not in connected_types:
            all_connectors.append({
                'type': system_type,
                'name': {
                    'quickbooks': 'QuickBooks Online',
                    'xero': 'Xero Accounting',
                    'business_central': 'Microsoft Business Central',
                    'odoo': 'Odoo ERP'
                }.get(system_type),
                'logo': f"{system_type.replace('_', '-')}-logo.png",
                'is_configured': False,
                'is_active': False,
                'last_sync_at': None,
                'sync_stats': None,
                'connection_id': None,
                'connection_object': None,
                'description': {
                    'quickbooks': 'Synchronisez automatiquement vos clients et factures depuis QuickBooks',
                    'xero': 'Synchronisez automatiquement vos clients et factures depuis Xero',
                    'business_central': 'Synchronisez automatiquement vos clients et factures via OData V4',
                    'odoo': 'Synchronisez automatiquement vos clients et factures depuis Odoo via XML-RPC'
                }.get(system_type, '')
            })

    return {
        'selected_connector': selected_connector,
        'available_connectors': all_connectors
    }


@company_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Company settings page"""
    from app import db
    from forms import CompanySettingsForm
    from models import Company, Plan, AccountingConnection, UserCompany

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé. Vous n\'avez pas les permissions pour accéder aux paramètres.', 'error')
        return redirect(url_for('main.dashboard'))

    # CORRECTION : Gestion des paramètres de retour Stripe Checkout
    checkout_status = request.args.get('checkout')
    if checkout_status == 'success':
        session_id = request.args.get('session_id')
        if session_id:
            flash('🎉 Paiement réussi ! Votre abonnement est maintenant actif.', 'success')
            current_app.logger.info(f"Paiement Stripe réussi pour {company.name}: {session_id}")
        else:
            flash('✅ Abonnement activé avec succès !', 'success')
    elif checkout_status == 'cancel':
        flash('Paiement annulé. Vous pouvez réessayer à tout moment.', 'info')

    # Récupérer le rôle de l'utilisateur pour vérifications de sécurité
    user_company = UserCompany.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()
    user_role = user_company.role if user_company else None

    # Create form with company data
    form = CompanySettingsForm()
    if request.method == 'GET':
        # Pré-remplir seulement en GET, pas en POST
        form.name.data = company.name
        form.email.data = company.email
        form.phone.data = company.phone
        form.address.data = company.address
        form.aging_calculation_method.data = company.aging_calculation_method or 'invoice_date'
        form.timezone.data = company.timezone or 'America/Montreal'
        form.currency.data = company.currency or 'CAD'
        form.primary_color.data = company.primary_color or '#007bff'
        form.secondary_color.data = company.secondary_color or '#6c757d'
        # Project field settings
        form.project_field_enabled.data = company.project_field_enabled or False
        form.project_field_name.data = company.project_field_name or 'Projet'

    if form.validate_on_submit():
        # SÉCURITÉ : Vérifier que seuls les super_admin peuvent modifier les paramètres généraux
        if user_role != 'super_admin':
            flash('Accès refusé. Seuls les super administrateurs peuvent modifier les paramètres généraux.', 'error')
            return redirect(url_for('company.settings'))

        from utils.secure_logging import sanitize_email_for_logs, sanitize_company_id_for_logs
        current_app.logger.info(f"SETTINGS: Formulaire soumis par {sanitize_email_for_logs(current_user.email)} pour company_id={sanitize_company_id_for_logs(company.id)}")
        # Handle form submission
        company.name = form.name.data
        company.email = form.email.data
        company.phone = form.phone.data
        company.address = form.address.data
        company.aging_calculation_method = form.aging_calculation_method.data
        company.timezone = form.timezone.data
        company.currency = form.currency.data or 'CAD'
        company.primary_color = form.primary_color.data or '#007bff'
        company.secondary_color = form.secondary_color.data or '#6c757d'

        # Handle project field settings
        company.project_field_enabled = form.project_field_enabled.data or False
        # If enabled but name is empty, use default
        if company.project_field_enabled and not form.project_field_name.data:
            company.project_field_name = 'Projet'
        else:
            company.project_field_name = form.project_field_name.data or 'Projet'

        # Handle logo upload - NOUVEAU: Encodage Base64 pour survie aux redéploiements
        if form.logo.data:
            import base64
            import imghdr

            try:
                # Lire le contenu du fichier uploadé
                logo_file = form.logo.data
                logo_file.seek(0)  # Reset position au début
                logo_data = logo_file.read()

                # Détecter le type MIME de l'image
                image_type = imghdr.what(None, h=logo_data)
                if image_type:
                    mime_type = f"image/{image_type}"
                else:
                    # Fallback basé sur l'extension du fichier
                    filename = logo_file.filename.lower()
                    if filename.endswith('.png'):
                        mime_type = "image/png"
                    elif filename.endswith(('.jpg', '.jpeg')):
                        mime_type = "image/jpeg"
                    elif filename.endswith('.gif'):
                        mime_type = "image/gif"
                    elif filename.endswith('.webp'):
                        mime_type = "image/webp"
                    else:
                        mime_type = "image/png"  # Défaut

                # Encoder en Base64 et créer le data URI complet
                logo_base64_encoded = base64.b64encode(logo_data).decode('utf-8')
                company.logo_base64 = f"data:{mime_type};base64,{logo_base64_encoded}"

                current_app.logger.info(f"Logo encodé en Base64 pour {company.name} ({mime_type}, {len(logo_data)} bytes)")

            except Exception as logo_error:
                current_app.logger.error(f"Erreur encodage logo Base64: {str(logo_error)}")
                flash('Erreur lors de l\'upload du logo. Veuillez réessayer.', 'error')


        try:
            db.session.commit()
            flash('Paramètres sauvegardés avec succès.', 'success')
        except Exception as e:
            db.session.rollback()
            flash('Erreur lors de la sauvegarde.', 'error')

        return redirect(url_for('company.settings'))

    # Get additional data for template
    # Trier par date de mise à jour (plus récent en premier)
    # Cela garantit que les nouvelles connexions apparaissent en haut
    accounting_connections = AccountingConnection.query.filter_by(
        company_id=company.id
    ).order_by(
        AccountingConnection.updated_at.desc()
    ).all()
    user_companies = UserCompany.query.filter_by(company_id=company.id).all()
    plans = Plan.query.filter_by(is_active=True).all()


    # Récupérer l'objet Plan depuis l'ID - CORRECTION pour l'onglet Abonnement
    plan_obj = None
    if hasattr(company, 'plan_id') and company.plan_id:
        from models import Plan
        plan_obj = Plan.query.get(company.plan_id)
        # CORRECTION CRITIQUE : Ne PAS modifier company.plan_ref pendant un GET (éviter flush automatique)

    # NOUVELLE LOGIQUE UX: Récupérer les vraies données Stripe pour interface intuitive
    current_period_licenses, next_period_licenses = get_stripe_license_periods(company)

    trial_info = {'is_trialing': False, 'trial_end': None, 'trial_end_display': None}
    if company.status == 'trialing' and company.stripe_subscription_id:
        trial_info['is_trialing'] = True
        try:
            import stripe
            sub = stripe.Subscription.retrieve(company.stripe_subscription_id)
            if sub.trial_end:
                from datetime import datetime
                trial_end_dt = datetime.fromtimestamp(sub.trial_end)
                trial_info['trial_end'] = trial_end_dt
                trial_info['trial_end_display'] = trial_end_dt.strftime('%d/%m/%Y')
        except Exception as e:
            current_app.logger.warning(f"Erreur récupération trial_end Stripe: {e}")

    subscription_data = {
        'subscription_status': company.subscription_status or 'active',
        'current_plan': {
            'display_name': plan_obj.display_name if plan_obj else (company.plan if hasattr(company, 'plan') and company.plan else 'Plan Premium'),
            'stripe_price_id': plan_obj.stripe_price_id if plan_obj else None
        },
        'user_counts': {
            'admin': len([uc for uc in user_companies if uc.role == 'admin']),
            'employee': len([uc for uc in user_companies if uc.role == 'employe'])
        },
        'licenses': {
            'current_period': current_period_licenses,
            'next_period': next_period_licenses
        },
        'cancellation': {
            'is_pending_cancellation': company.is_pending_cancellation() if hasattr(company, 'is_pending_cancellation') else False,
            'can_reactivate': company.can_be_reactivated() if hasattr(company, 'can_be_reactivated') else False,
            'cancellation_date': company.cancellation_date.strftime('%d/%m/%Y') if hasattr(company, 'cancellation_date') and company.cancellation_date else None
        },
        'trial': trial_info
    }

    # Vérifier les permissions d'accès aux connexions comptables
    from utils.permissions_helper import check_accounting_access
    accounting_permission = check_accounting_access(current_user, company)

    # Récupérer le rôle de l'utilisateur pour contrôler l'affichage
    user_role = current_user.get_role_in_company(company.id)

    # NOUVEAU: Normaliser les données des connecteurs comptables (architecture 2-sections)
    connector_context = build_accounting_connector_context(company)

    return render_template('company/settings.html',
                         company=company,
                         form=form,
                         plans=plans,
                         plan_obj=plan_obj,
                         accounting_connections=accounting_connections,  # Kept for backward compatibility
                         connector_context=connector_context,  # NEW: normalized connector data
                         user_companies=user_companies,
                         current_user=current_user,
                         user_role=user_role,
                         subscription_data=subscription_data,
                         accounting_permission=accounting_permission)


@company_bp.route('/test-bc')
@login_required
def test_bc():
    """Test Business Central template"""
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        return "No company selected"

    accounting_connections = AccountingConnection.query.filter_by(company_id=company.id).all()

    return render_template('test_bc.html',
                         accounting_connections=accounting_connections,
                         company=company)

@company_bp.route('/users')
@login_required
def users_list():
    """List all users in the company (admin only)"""
    from models import UserCompany, Company

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Vérifier les permissions : rôle ET plan
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Vous n\'avez pas les permissions pour gérer les utilisateurs.', 'error')
        return redirect(url_for('main.dashboard'))

    # Vérifier que le plan autorise la gestion d'équipe
    plan_features = company.get_plan_features() or {}
    if not plan_features.get('allows_team_management', False):
        flash('Cette fonctionnalité n\'est pas disponible avec votre plan actuel.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get all users for this company
    user_companies = UserCompany.query.filter_by(company_id=company.id).all()

    return render_template('users/list.html',
                           user_companies=user_companies,
                           company=company)


def add_user_to_company(email, company_id, role, form_data):
    """
    Service unifié pour ajouter un utilisateur à une entreprise.
    Respecte strictement le choix du type d'utilisateur.
    """
    from models import User, UserCompany
    from app import db
    from werkzeug.security import generate_password_hash
    import secrets
    import string

    try:
        user_type = form_data.get('user_type')
        existing_user = User.query.filter_by(email=email).first()

        # VÉRIFICATION DES LICENCES pour les rôles payants - MÉTHODE CENTRALISÉE
        from models import Company
        company = Company.query.get(company_id)
        if company:
            can_add, message = company.can_add_user(role)
            if not can_add:
                return {
                    'success': False,
                    'message': message
                }

        if user_type == 'new':
            # NOUVEL UTILISATEUR : L'email NE DOIT PAS exister
            if existing_user:
                return {
                    'success': False,
                    'message': 'Un utilisateur avec cette adresse email existe déjà. Veuillez choisir "Utilisateur existant".'
                }

            # Créer un nouvel utilisateur
            if not form_data.get('password'):
                temp_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
            else:
                temp_password = form_data['password']

            new_user = User(
                first_name=form_data['first_name'],
                last_name=form_data['last_name'],
                email=email,
                password_hash=generate_password_hash(temp_password),
                must_change_password=True
            )
            db.session.add(new_user)
            db.session.flush()  # Obtenir l'ID

            # Créer la relation avec l'entreprise
            user_company = UserCompany(
                user_id=new_user.id,
                company_id=company_id,
                role=role
            )
            db.session.add(user_company)
            db.session.commit()

            return {
                'success': True,
                'user': new_user,
                'operation_type': 'new',
                'temp_password': temp_password,
                'message': f'Nouvel utilisateur {new_user.full_name} créé avec succès.'
            }

        elif user_type == 'existing':
            # UTILISATEUR EXISTANT : L'email DOIT exister
            if not existing_user:
                return {
                    'success': False,
                    'message': 'Aucun utilisateur trouvé avec cette adresse email. Veuillez choisir "Nouvel utilisateur".'
                }

            # Vérifier s'il est déjà dans cette entreprise
            existing_relationship = UserCompany.query.filter_by(
                user_id=existing_user.id,
                company_id=company_id
            ).first()

            if existing_relationship:
                return {
                    'success': False,
                    'message': 'Cet utilisateur fait déjà partie de cette entreprise.'
                }

            # Ajouter l'utilisateur existant à l'entreprise SANS nouveau mot de passe
            user_company = UserCompany(
                user_id=existing_user.id,
                company_id=company_id,
                role=role
            )
            db.session.add(user_company)
            db.session.commit()

            return {
                'success': True,
                'user': existing_user,
                'operation_type': 'existing',
                'message': f'Utilisateur {existing_user.full_name} ajouté à l\'entreprise avec succès.'
            }

        else:
            return {
                'success': False,
                'message': 'Type d\'utilisateur invalide.'
            }

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur ajout utilisateur: {str(e)}')
        return {
            'success': False,
            'message': 'Erreur lors de l\'ajout de l\'utilisateur.'
        }


@company_bp.route('/test-smtp', methods=['POST'])
@login_required
def test_smtp():
    """Test SMTP connection with provided configuration"""
    import smtplib
    import socket
    from email.message import EmailMessage

    company = current_user.get_selected_company()
    if not company:
        return jsonify({
            'success': False,
            'message': 'Aucune entreprise sélectionnée.'
        }), 400

    if not current_user.can_access_company_settings():
        return jsonify({
            'success': False,
            'message': 'Accès refusé.'
        }), 403

    try:
        data = request.get_json()

        smtp_server = data.get('smtp_server', '').strip()
        smtp_port = data.get('smtp_port', '').strip()
        smtp_username = data.get('smtp_username', '').strip()
        smtp_password = data.get('smtp_password', '').strip()
        smtp_use_tls = data.get('smtp_use_tls', True)
        smtp_from_name = data.get('smtp_from_name', 'Test Finov\'Relance')

        if not smtp_server or not smtp_port or not smtp_username:
            return jsonify({
                'success': False,
                'message': 'Le serveur SMTP, le port et le nom d\'utilisateur sont obligatoires'
            }), 400

        if not smtp_password:
            return jsonify({
                'success': False,
                'message': 'Le mot de passe SMTP est obligatoire'
            }), 400

        if smtp_server.startswith('.') or len(smtp_server) < 3:
            return jsonify({
                'success': False,
                'message': 'Le serveur SMTP doit être un nom de domaine valide'
            }), 400

        try:
            port_int = int(smtp_port)
            if port_int < 1 or port_int > 65535:
                return jsonify({
                    'success': False,
                    'message': 'Le port doit être entre 1 et 65535'
                }), 400
        except ValueError:
            return jsonify({
                'success': False,
                'message': 'Le port doit être un nombre valide'
            }), 400

        class TempConfig:
            def __init__(self):
                self.smtp_server = smtp_server
                self.smtp_port = port_int
                self.smtp_username = smtp_username
                self.smtp_password = smtp_password
                self.smtp_use_tls = smtp_use_tls
                self.smtp_from_name = smtp_from_name
                self.name = 'Test Company'

        test_company = TempConfig()

        try:
            socket.gethostbyname(smtp_server)
        except socket.gaierror:
            return jsonify({
                'success': False,
                'message': 'Serveur SMTP introuvable. Le nom de domaine ne peut pas être résolu.'
            }), 400
        except Exception as e:
            return jsonify({
                'success': False,
                'message': 'Erreur de resolution DNS. Verifiez le nom du serveur SMTP.'
            }), 400

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((smtp_server, port_int))
            sock.close()

            if result != 0:
                return jsonify({
                    'success': False,
                    'message': f'Impossible de se connecter au serveur SMTP sur le port {port_int}. Connexion refusée ou firewall actif.'
                }), 424

        except socket.timeout:
            return jsonify({
                'success': False,
                'message': 'Délai d\'attente dépassé lors de la connexion au serveur SMTP.'
            }), 424
        except Exception as e:
            return jsonify({
                'success': False,
                'message': 'Erreur de connectivite reseau. Verifiez la configuration du serveur SMTP.'
            }), 424

        server = None
        try:
            if test_company.smtp_use_tls:
                server = smtplib.SMTP(test_company.smtp_server, test_company.smtp_port, timeout=10)
                server.ehlo()
                server.starttls()
                server.ehlo()
            else:
                server = smtplib.SMTP_SSL(test_company.smtp_server, test_company.smtp_port, timeout=10)
                server.ehlo()

            server.login(test_company.smtp_username, test_company.smtp_password)

            msg = EmailMessage()
            msg['Subject'] = 'Test de configuration SMTP - Finov\'Relance'
            msg['From'] = f"{test_company.smtp_from_name} <{test_company.smtp_username}>"
            msg['To'] = test_company.smtp_username
            msg.set_content('Ceci est un test de configuration SMTP. Si vous recevez ce message, votre configuration est correcte.')

            server.send_message(msg)

            return jsonify({
                'success': True,
                'message': 'Connexion SMTP réussie et email de test envoyé ! La configuration est correcte.'
            })

        except smtplib.SMTPAuthenticationError:
            return jsonify({
                'success': False,
                'message': 'Authentification échouée. Vérifiez le nom d\'utilisateur et le mot de passe.'
            }), 401
        except smtplib.SMTPException as e:
            error_msg = str(e)
            if 'authentication' in error_msg.lower():
                return jsonify({
                    'success': False,
                    'message': f'Problème d\'authentification SMTP: {error_msg}'
                }), 401
            return jsonify({
                'success': False,
                'message': f'Erreur SMTP: {error_msg}'
            }), 424
        except Exception as e:
            error_msg = str(e)
            if 'server_hostname' in error_msg:
                return jsonify({
                    'success': False,
                    'message': 'Serveur SMTP invalide. Vérifiez le nom du serveur.'
                }), 400
            elif 'ssl' in error_msg.lower():
                return jsonify({
                    'success': False,
                    'message': 'Erreur SSL/TLS. Vérifiez la configuration TLS.'
                }), 424
            current_app.logger.error(f'Erreur SMTP: {error_msg}')
            return jsonify({
                'success': False,
                'message': 'Erreur de connexion au serveur SMTP.'
            }), 424
        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    pass

    except Exception as e:
        current_app.logger.error(f'Erreur test SMTP: {str(e)}')
        return jsonify({
            'success': False,
            'message': 'Erreur lors du test de connexion.'
        }), 400

@company_bp.route('/microsoft/test', methods=['GET'])
@login_required
def test_microsoft_connection():
    """
    Fonction vérifiée par MDF le 29/01/2026.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Test Microsoft Graph API connection"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({
            'success': False,
            'message': 'Aucune entreprise sélectionnée.'
        }), 400

    if not current_user.can_access_company_settings():
        return jsonify({
            'success': False,
            'message': 'Accès refusé.'
        }), 403

    try:
        from models import EmailConfiguration

        # Get company's email configuration
        email_config = EmailConfiguration.query.filter_by(company_id=company.id).first()

        if not email_config:
            return jsonify({
                'success': False,
                'message': 'Configuration email non trouvée pour cette entreprise.'
            }), 404

        # Check if Microsoft OAuth is configured
        if not email_config.outlook_oauth_access_token:
            return jsonify({
                'success': False,
                'message': 'Microsoft Graph n\'est pas configuré. Veuillez d\'abord connecter votre compte Microsoft.'
            }), 400

        # Check if token needs refresh
        if hasattr(email_config, 'needs_token_refresh') and email_config.needs_token_refresh():
            try:
                from email_fallback import refresh_user_oauth_token
                refresh_user_oauth_token(email_config)
                current_app.logger.info("Microsoft OAuth token refreshed successfully")
            except Exception as refresh_error:
                return jsonify({
                    'success': False,
                    'message': f'Erreur lors du rafraîchissement du token: {str(refresh_error)}'
                }), 424

        # Test the connection by making a simple Graph API call
        import requests
        headers = {
            'Authorization': f'Bearer {email_config.outlook_oauth_access_token}',
            'Content-Type': 'application/json'
        }

        # Test with a simple GET request to user profile
        test_url = 'https://graph.microsoft.com/v1.0/me'
        response = requests.get(test_url, headers=headers, timeout=10)

        if response.status_code == 200:
            user_data = response.json()
            user_email = user_data.get('mail') or user_data.get('userPrincipalName', 'Non disponible')

            return jsonify({
                'success': True,
                'message': f'Connexion Microsoft réussie ! Compte: {user_email}'
            }), 200
        elif response.status_code == 401:
            return jsonify({
                'success': False,
                'message': 'Token d\'accès invalide ou expiré. Veuillez reconnecter votre compte Microsoft.'
            }), 401
        else:
            error_detail = response.json().get('error', {}).get('message', response.text)
            return jsonify({
                'success': False,
                'message': f'Erreur Microsoft Graph (code {response.status_code}): {error_detail}'
            }), 424

    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'message': 'Délai d\'attente dépassé lors de la connexion à Microsoft Graph.'
        }), 408
    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'message': 'Impossible de se connecter à Microsoft Graph. Vérifiez votre connexion internet.'
        }), 503
    except Exception as e:
        current_app.logger.error(f"Microsoft test error: {str(e)}")
        return jsonify({
            'success': False,
            'message': 'Une erreur interne est survenue lors du test.'
        }), 500


@company_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
def create_user():
    """Create a new user in the current company"""
    from forms import CreateUserForm
    from models import User, UserCompany
    from app import db
    from werkzeug.security import generate_password_hash
    from notification_system import send_notification
    import secrets
    import string

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Vérifier les permissions : rôle ET plan
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Vous n\'avez pas les permissions pour gérer les utilisateurs.', 'error')
        return redirect(url_for('main.dashboard'))

    # Vérifier que le plan autorise la gestion d'équipe
    plan_features = company.get_plan_features() or {}
    if not plan_features.get('allows_team_management', False):
        flash('Cette fonctionnalité n\'est pas disponible avec votre plan actuel.', 'error')
        return redirect(url_for('main.dashboard'))

    form = CreateUserForm(company_id=company.id)

    if request.method == 'POST' and form.validate_on_submit():
        try:
            # Service unifié pour ajouter un utilisateur à une entreprise
            result = add_user_to_company(
                email=form.email.data,
                company_id=company.id,
                role=form.role.data,
                form_data={
                    'first_name': form.first_name.data,
                    'last_name': form.last_name.data,
                    'password': form.password.data,
                    'user_type': form.user_type.data
                }
            )

            if not result['success']:
                flash(result['message'], 'error')
                return render_template('users/create.html', form=form, company=company)

            user_to_add = result['user']
            user_type = result['operation_type']
            temp_password = result.get('temp_password')

            # Send email notification for new users
            role_display = {
                'super_admin': 'Super Admin',
                'admin': 'Administrateur',
                'employe': 'Employé',
                'lecteur': 'Lecteur'
            }.get(form.role.data, form.role.data)

            # Send email notification for new users and existing users with new password
            if temp_password:  # Un mot de passe temporaire a été généré
                try:
                    from email_fallback import send_email_via_system_config

                    if user_type == 'new':
                        email_subject = f"Bienvenue dans {company.name} - FinovRelance"
                        welcome_message = f"Votre compte a été créé avec succès dans l'application <strong>FinovRelance</strong> pour l'entreprise <strong>{company.name}</strong>."
                    else:
                        email_subject = f"Vous avez été ajouté à {company.name} - FinovRelance"
                        welcome_message = f"Vous avez été ajouté à l'équipe de <strong>{company.name}</strong> dans l'application <strong>FinovRelance</strong>."

                    email_content = f"""
                    <html>
                    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                        <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px;">
                            <h2 style="color: #8475EC; margin-bottom: 20px;">Bienvenue chez {company.name}</h2>

                            <p>Bonjour {user_to_add.first_name},</p>

                            <p>{welcome_message}</p>

                            <div style="background-color: #e9ecef; padding: 20px; border-radius: 8px; margin: 20px 0;">
                                <h3 style="color: #495057; margin-top: 0;">Informations de connexion :</h3>
                                <p><strong>Email :</strong> {user_to_add.email}</p>
                                <p><strong>Mot de passe temporaire :</strong> <code style="background-color: #fff; padding: 5px 10px; border-radius: 4px; font-weight: bold;">{temp_password}</code></p>
                                <p><strong>Rôle :</strong> {role_display}</p>
                            </div>

                            <div style="background-color: #fff3cd; border-left: 4px solid #856404; padding: 15px; margin: 20px 0;">
                                <p style="margin: 0;"><strong>Important :</strong> Vous devrez changer ce mot de passe lors de votre première connexion pour des raisons de sécurité.</p>
                            </div>

                            <p>Vous pouvez vous connecter en utilisant le lien suivant :</p>
                            <p style="text-align: center; margin: 30px 0;">
                                <a href="{request.url_root}auth/login"
                                   style="background-color: #8475EC; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; display: inline-block;">
                                    Se connecter à FinovRelance
                                </a>
                            </p>

                            <p>Si vous avez des questions, n'hésitez pas à contacter votre administrateur.</p>

                            <hr style="margin: 30px 0; border: none; border-top: 1px solid #dee2e6;">
                            <p style="text-align: center; color: #6c757d; font-size: 14px;">
                                FinovRelance - Gestion simplifiée de la relance client
                            </p>
                        </div>
                    </body>
                    </html>
                    """

                    send_email_via_system_config(user_to_add.email, email_subject, email_content)
                    from utils.secure_logging import sanitize_user_id_for_logs
                    current_app.logger.info(f"Email envoyé à user_id={sanitize_user_id_for_logs(user_to_add.id)}")

                except Exception as e:
                    current_app.logger.error(f"Erreur envoi email invitation utilisateur")
                    # N'interrompons pas le processus si l'email échoue
                    pass

                if user_type == 'new':
                    flash(f'Nouvel utilisateur {user_to_add.full_name} créé avec succès avec le rôle {role_display}. Un email contenant les informations de connexion a été envoyé.', 'success')
                else:
                    flash(f'Utilisateur {user_to_add.full_name} ajouté à l\'entreprise avec le rôle {role_display}. Un email contenant les informations de connexion a été envoyé.', 'success')
            else:
                flash(f'Utilisateur {user_to_add.full_name} ajouté à l\'entreprise avec le rôle {role_display}.', 'success')

            log_user_action(
                AuditActions.USER_INVITED,
                target_user=user_to_add,
                role=form.role.data,
                details={'operation': user_type},
                company=company
            )

            return redirect(url_for('company.users_list'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur création utilisateur: {str(e)}')
            flash('Erreur lors de la création de l\'utilisateur.', 'error')

    return render_template('users/create.html', form=form, company=company)


@company_bp.route('/api/check-user-exists', methods=['POST'])
@limiter.exempt
@login_required
def check_user_exists():
    """API endpoint to check if a user exists and is already in the company"""
    from models import User, UserCompany

    data = request.get_json()
    if not data or 'email' not in data:
        return jsonify({'error': 'Email requis'}), 400

    email = data['email'].strip()
    company = current_user.get_selected_company()

    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Check if user exists globally
    user = User.query.filter_by(email=email).first()

    if not user:
        return jsonify({'exists': False})

    # Check if user is already in this company
    user_company = UserCompany.query.filter_by(
        user_id=user.id,
        company_id=company.id
    ).first()

    return jsonify({
        'exists': True,
        'already_in_company': user_company is not None,
        'user_info': {
            'first_name': user.first_name,
            'last_name': user.last_name,
            'email': user.email
        }
    })


@company_bp.route('/update-licenses', methods=['POST'])
@login_required
def update_licenses():
    """Mettre à jour les quantités de licences"""
    from app import db
    from models import Company
    from notification_system import send_notification
    import stripe

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    try:
        # REFONTE STRIPE 2.0 : Architecture simplifiée avec quantity_licenses
        quantity_licenses = int(request.form.get('quantity_licenses', 1))

        current_app.logger.info(f"DEMANDE MODIFICATION LICENCES V2 pour {company.name}: {quantity_licenses} licences")

        if quantity_licenses < 1:
            flash('Le nombre de licences doit être d\'au moins 1.', 'error')
            return redirect(url_for('company.settings') + '#subscription')

        # Calculer les changements demandés
        current_licenses = company.quantity_licenses or 1
        license_change = quantity_licenses - current_licenses

        # Si pas de changement, retourner directement
        if license_change == 0:
            flash('Aucun changement détecté dans les quantités de licences.', 'info')
            return redirect(url_for('company.settings') + '#subscription')

        # Traiter la modification de licences via système V2 simplifié
        current_app.logger.info(f"MODIFICATION LICENCES V2: {quantity_licenses} licences")

        # Mettre à jour directement les quantités locales
        old_licenses = current_licenses
        company.quantity_licenses = quantity_licenses

        from app import db
        db.session.commit()

        log_action(AuditActions.LICENSE_UPDATED, entity_type=EntityTypes.COMPANY,
                  entity_id=company.id, entity_name=company.name,
                  details={'old_licenses': old_licenses, 'new_licenses': quantity_licenses})

        result = "success"

        current_app.logger.info(f"RÉSULTAT create_license_modification_session: {result}")

        # Traitement des résultats avec notifications appropriées
        if result == "success":
            # Notification de succès pour augmentation
            send_notification(
                user_id=current_user.id,
                company_id=company.id,
                type='license_upgrade',
                title='Licences augmentées avec succès',
                message=f'Vos licences ont été mises à jour: {quantity_licenses} licences. Facturation effective immédiatement.',
                data={
                    'quantity_licenses': quantity_licenses,
                    'license_change': license_change
                }
            )
            flash(f'Licences mises à jour avec succès: {quantity_licenses} licences. Facturation effective immédiatement.', 'success')
        elif result == "reduction_scheduled":
            # Notification pour réduction avec période de grâce
            send_notification(
                user_id=current_user.id,
                company_id=company.id,
                type='license_downgrade',
                title='Réduction de licences programmée',
                message=f'Vos licences seront réduites au prochain cycle: {quantity_licenses} licences. Vous conservez l\'accès pendant la période de grâce.',
                data={
                    'quantity_licenses': quantity_licenses,
                    'license_change': license_change
                }
            )
            flash(f'Réduction programmée avec succès. Vos licences seront ajustées au prochain cycle de facturation. Vous conservez l\'accès complet pendant la période de grâce.', 'success')
        elif result is None:
            flash('Aucun changement de licences détecté.', 'info')
        else:
            flash('Erreur lors de la mise à jour des licences.', 'error')

    except ValueError:
        flash('Valeurs invalides pour les quantités de licences.', 'error')
    except Exception as e:
        current_app.logger.error(f"Erreur lors de la mise à jour des licences: {e}")
        flash('Erreur lors de la mise à jour des licences.', 'error')

    return redirect(url_for('company.settings') + '#subscription')


@company_bp.route('/delete-logo', methods=['POST'])
@login_required
def delete_logo():
    """Delete company logo"""
    from app import db
    import os

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('company.settings'))

    try:
        # ANCIEN SYSTÈME: Delete logo file if it exists (temporaire pour migration)
        if company.logo_path:
            logo_path = os.path.join('static', 'uploads', 'logos', company.logo_path)
            if os.path.exists(logo_path):
                os.remove(logo_path)
            company.logo_path = None

        # NOUVEAU SYSTÈME: Clear logo Base64 from database
        if company.logo_base64:
            company.logo_base64 = None

        db.session.commit()
        log_action(AuditActions.LOGO_DELETED, entity_type=EntityTypes.COMPANY,
                  entity_id=company.id, entity_name=company.name)
        flash('Logo supprimé avec succès.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting logo for company {company.id}: {e}")
        flash('Une erreur est survenue lors de la suppression du logo. Veuillez reessayer.', 'error')

    return redirect(url_for('company.settings'))


@company_bp.route('/quickbooks-connect')
@login_required
def quickbooks_connect():
    """Initiate QuickBooks OAuth connection"""
    from quickbooks_connector import QuickBooksConnector

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    # Vérifier les permissions d'accès aux connexions comptables
    from utils.permissions_helper import check_accounting_access
    permission = check_accounting_access(current_user, company)

    if not permission['allowed']:
        flash(permission['restriction_reason'], 'error')
        return redirect(url_for('company.settings', _anchor='accounting'))

    try:
        qb_connector = QuickBooksConnector()
        qb_connector.client_id = current_app.config.get('QUICKBOOKS_CLIENT_ID')
        qb_connector.client_secret = current_app.config.get('QUICKBOOKS_CLIENT_SECRET')

        if not qb_connector.client_id:
            flash('QUICKBOOKS_CLIENT_ID non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        if not qb_connector.client_secret:
            flash('QUICKBOOKS_CLIENT_SECRET non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        import secrets
        state = secrets.token_urlsafe(32)
        auth_url = qb_connector.get_authorization_url(state)

        session['qb_company_id'] = company.id
        session['qb_state'] = state

        return redirect(auth_url)

    except Exception as e:
        current_app.logger.error(f"Error connecting QuickBooks: {e}")
        flash('Une erreur est survenue lors de la connexion QuickBooks. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings'))


@company_bp.route('/quickbooks/callback')
@login_required
def quickbooks_callback():
    """Handle QuickBooks OAuth callback"""
    from app import db
    from models import AccountingConnection
    from quickbooks_connector import QuickBooksConnector

    # Get parameters from callback
    authorization_code = request.args.get('code')
    realm_id = request.args.get('realmId')
    state = request.args.get('state')
    error = request.args.get('error')

    # Check for errors
    if error:
        flash(f'Erreur QuickBooks: {error}', 'error')
        return redirect(url_for('company.settings'))

    # Verify required parameters
    if not authorization_code or not realm_id:
        flash('Paramètres manquants dans la réponse QuickBooks.', 'error')
        return redirect(url_for('company.settings'))

    # Verify state parameter
    if state != session.get('qb_state'):
        flash('État de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('company.settings'))

    # Get company from session
    company_id = session.get('qb_company_id')
    if not company_id:
        flash('Session expirée. Veuillez réessayer.', 'error')
        return redirect(url_for('company.settings'))

    try:
        # Initialize connector and exchange code for tokens
        qb_connector = QuickBooksConnector()
        token_data = qb_connector.exchange_code_for_tokens(authorization_code, realm_id, state)

        # Check if connection already exists
        existing_connection = AccountingConnection.query.filter_by(
            company_id=company_id,
            system_type='quickbooks'
        ).first()

        if existing_connection:
            # Update existing connection
            existing_connection.access_token = token_data['access_token']
            existing_connection.refresh_token = token_data['refresh_token']
            existing_connection.token_expires_at = token_data['expires_at']
            existing_connection.company_id_external = realm_id
            existing_connection.is_active = True
            connection = existing_connection
        else:
            # Create new connection
            connection = AccountingConnection(
                company_id=company_id,
                system_type='quickbooks',
                system_name='QuickBooks Online',
                access_token=token_data['access_token'],
                refresh_token=token_data['refresh_token'],
                token_expires_at=token_data['expires_at'],
                company_id_external=realm_id,
                is_active=True
            )
            db.session.add(connection)

        db.session.commit()

        # Clean up session
        session.pop('qb_company_id', None)
        session.pop('qb_state', None)

        flash('Connexion QuickBooks établie avec succès !', 'success')
        return redirect(url_for('company.settings'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in QuickBooks callback: {e}")
        flash('Une erreur est survenue lors de la connexion QuickBooks. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings'))


@company_bp.route('/quickbooks-disconnect/<int:connection_id>')
@login_required
def quickbooks_disconnect(connection_id):
    """Disconnect QuickBooks"""
    from app import db
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id, company_id=company.id).first_or_404()

    # Désactiver la connexion
    connection.is_active = False

    # Supprimer les tokens OAuth pour sécurité
    connection.access_token = None
    connection.refresh_token = None
    connection.token_expires_at = None

    # Réinitialiser les statistiques de sync (optionnel mais plus propre)
    connection.sync_stats = None

    db.session.commit()

    log_action(AuditActions.OAUTH_DISCONNECTED, entity_type=EntityTypes.OAUTH,
              entity_id=connection_id, entity_name='quickbooks',
              details={'company_id': company.id})

    flash('QuickBooks a été déconnecté avec succès.', 'success')
    return redirect(url_for('company.settings'))


@company_bp.route('/quickbooks-sync', methods=['POST'])
@login_required
def quickbooks_sync():
    """Trigger QuickBooks synchronization"""
    from app import db
    from models import AccountingConnection
    from quickbooks_connector import QuickBooksConnector
    import threading
    from datetime import datetime

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='quickbooks',
        is_active=True
    ).first()

    if not connection:
        flash("Aucune connexion QuickBooks active trouvée", "danger")
        return redirect(url_for('company.settings'))

    # Sauvegarder les IDs nécessaires AVANT de créer le thread
    user_id = current_user.id
    company_id = company.id
    connection_id = connection.id

    def run_sync():
        # LOG IMMÉDIAT: Vérifier que le thread démarre
        import logging

        # Importer l'app Flask pour créer un contexte dans le thread
        from app import app

        # Créer un nouveau contexte d'application pour le thread
        with app.app_context():
            from models import SyncLog
            sync_log = None

            try:
                # LOG: Début de synchronisation

                # DIAGNOSTIC: Récupérer la connexion DIRECTEMENT sans safe_get (pour diagnostic)
                thread_connection = AccountingConnection.query.get(connection_id)

                # DIAGNOSTIC: Logger les informations de la connexion
                if not thread_connection:
                    current_app.logger.error(f"❌ SYNC ERROR: Connexion {connection_id} introuvable dans la base de données")
                    return

                # DIAGNOSTIC: Vérifier le company_id
                if thread_connection.company_id != company_id:
                    current_app.logger.error(
                        f"❌ SÉCURITÉ: Connexion {connection_id} appartient à company {thread_connection.company_id}, "
                        f"mais demandée pour company {company_id}"
                    )
                    return


                # Créer un SyncLog pour tracker cette synchronisation
                sync_log = SyncLog(
                    connection_id=connection_id,
                    sync_type='both',
                    status='running',
                    started_at=datetime.utcnow()
                )
                db.session.add(sync_log)
                db.session.commit()

                # Initialiser le connecteur avec validation de sécurité
                # La vérification manuelle ci-dessus + validation dans le constructeur = double sécurité
                qb_connector = QuickBooksConnector(thread_connection.id, company_id)
                customers_created, customers_updated = qb_connector.sync_customers(company_id, sync_log_id=sync_log.id)

                # Vérifier si la sync customers a été arrêtée manuellement
                db.session.refresh(sync_log)
                if sync_log.status == 'stopped_manual':
                    return  # Arrêter ici sans exécuter la sync invoices

                invoices_created, invoices_updated = qb_connector.sync_invoices(company_id, sync_log_id=sync_log.id)

                # Vérifier si la sync a été arrêtée manuellement après sync_invoices
                db.session.refresh(sync_log)
                if sync_log.status == 'stopped_manual':
                    # Envoyer notification d'arrêt manuel
                    from notification_system import send_notification
                    send_notification(
                        user_id=user_id,
                        company_id=company_id,
                        type='warning',
                        title='Synchronisation QuickBooks arrêtée',
                        message=f"La synchronisation a été arrêtée manuellement. Clients: {customers_created} créés, {customers_updated} mis à jour. Factures: {invoices_created} créées, {invoices_updated} mises à jour.",
                        data={
                            'customers_created': customers_created,
                            'customers_updated': customers_updated,
                            'invoices_created': invoices_created,
                            'invoices_updated': invoices_updated
                        }
                    )
                    return

                payments_created = qb_connector.sync_payments(company_id, sync_log_id=sync_log.id)
                current_app.logger.info(f"QB payment sync: {payments_created} enregistrements créés")

                # Mettre à jour les statistiques de synchronisation
                thread_connection.last_sync_at = db.func.now()
                thread_connection.sync_stats = {
                    'last_sync': datetime.utcnow().isoformat(),
                    'customers_created': customers_created,
                    'customers_updated': customers_updated,
                    'invoices_created': invoices_created,
                    'invoices_updated': invoices_updated,
                    'payments_created': payments_created,
                    'status': 'success'
                }
                db.session.commit()

                # Envoyer notification de succès
                from notification_system import send_notification

                total_operations = customers_created + customers_updated + invoices_created + invoices_updated

                # Message détaillé incluant les suppressions
                if invoices_created > 0 or invoices_updated > 0:
                    message = f"Synchronisation terminée avec succès : {customers_created} clients créés, {customers_updated} mis à jour, {invoices_created} factures créées, {invoices_updated} mises à jour. Les factures payées ont été automatiquement supprimées."
                else:
                    message = f"Synchronisation terminée avec succès : {customers_created} clients créés, {customers_updated} mis à jour. Aucune nouvelle facture en souffrance. Les factures payées ont été automatiquement supprimées."

                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='quickbooks_sync',
                    title='Synchronisation QuickBooks terminée',
                    message=message,
                    data={
                        'customers_created': customers_created,
                        'customers_updated': customers_updated,
                        'invoices_created': invoices_created,
                        'invoices_updated': invoices_updated,
                        'total_operations': total_operations
                    }
                )

                # Log audit for sync completion
                from models import AuditLog, User, Company
                sync_user = User.query.get(user_id)
                sync_company = Company.query.get(company_id)
                AuditLog.log_with_session(
                    db.session,
                    action=AuditActions.SYNC_COMPLETED,
                    entity_type=EntityTypes.SYNC,
                    entity_name='QuickBooks',
                    details={
                        'sync_type': 'quickbooks',
                        'stats': {
                            'customers_created': customers_created,
                            'customers_updated': customers_updated,
                            'invoices_created': invoices_created,
                            'invoices_updated': invoices_updated
                        }
                    },
                    user=sync_user,
                    company=sync_company
                )

            except Exception as e:
                # En cas d'erreur, enregistrer les détails
                try:
                    # SÉCURITÉ: Récupérer la connexion avec vérification company_id (version thread-safe)
                    from utils import safe_get_by_id_thread
                    thread_connection = safe_get_by_id_thread(AccountingConnection, connection_id, company_id)

                    if not thread_connection:
                        current_app.logger.error(f"SYNC ERROR THREAD: Connexion {connection_id} non trouvée pour log d'erreur")
                        return
                    thread_connection.sync_stats = {
                        'last_sync': datetime.utcnow().isoformat(),
                        'status': 'error',
                        'error': str(e)
                    }
                    db.session.commit()

                    # Envoyer notification d'erreur
                    from notification_system import send_notification
                    send_notification(
                        user_id=user_id,
                        company_id=company_id,
                        type='error',
                        title='Erreur lors de la synchronisation QuickBooks',
                        message=f"La synchronisation a échoué : {str(e)}",
                        data={'error': str(e)}
                    )

                except Exception:
                    pass  # Éviter les erreurs en cascade

    # Vérifier le quota de synchronisation journalier
    from models import CompanySyncUsage
    if not CompanySyncUsage.check_company_sync_limit(company_id):
        flash('❌ Quota de synchronisations journalier atteint pour votre forfait. Veuillez réessayer demain ou passer à un forfait supérieur.', 'error')
        return redirect(url_for('company.settings') + '#accounting')

    CompanySyncUsage.increment_company_sync_count(company_id)

    # Démarrer le monitoring pour surveiller cette synchronisation
    from sync_monitor import ensure_monitoring_started
    ensure_monitoring_started()

    threading.Thread(target=run_sync, daemon=True).start()

    # Répondre différemment selon le type de requête
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # Requête AJAX - répondre avec JSON
        from flask import jsonify
        return jsonify({'success': True, 'message': 'Synchronisation QuickBooks lancée en arrière-plan...'})
    else:
        # Requête normale - rediriger
        flash("Synchronisation QuickBooks lancée en arrière-plan...", "info")
        return redirect(url_for('company.settings'))


# ========================================
# Business Central Routes
# ========================================

@company_bp.route('/business-central/config')
@login_required
def business_central_config():
    """Page de configuration initiale du connecteur Business Central"""
    from models import AccountingConnection, BusinessCentralConfig

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    # Vérifier les permissions d'accès aux connexions comptables
    from utils.permissions_helper import check_accounting_access
    permission = check_accounting_access(current_user, company)

    if not permission['allowed']:
        flash(permission['restriction_reason'], 'error')
        return redirect(url_for('company.settings', _anchor='accounting'))

    # Vérifier si une connexion existe déjà
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central'
    ).first()

    bc_config = None
    if connection:
        bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

    return render_template('company/business_central_config.html',
                         company=company,
                         connection=connection,
                         bc_config=bc_config)

@company_bp.route('/business-central/save-config', methods=['POST'])
@login_required
def business_central_save_config():
    """Sauvegarder la configuration OData et initier la connexion OAuth"""
    from app import db
    from models import AccountingConnection, BusinessCentralConfig

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    customers_url = request.form.get('customers_odata_url', '').strip()
    invoices_url = request.form.get('invoices_odata_url', '').strip()

    if not customers_url or not invoices_url:
        flash('Veuillez fournir les URLs OData pour les clients et les factures.', 'error')
        return redirect(url_for('company.business_central_config'))

    session['bc_customers_url'] = customers_url
    session['bc_invoices_url'] = invoices_url
    session['bc_company_id'] = company.id

    # Rediriger vers OAuth
    return redirect(url_for('company.business_central_connect'))

@company_bp.route('/business-central/connect')
@login_required
def business_central_connect():
    """Initier la connexion OAuth avec Business Central"""
    from business_central_connector import BusinessCentralConnector

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    try:
        bc_connector = BusinessCentralConnector()

        if not bc_connector.client_id:
            flash('BUSINESS_CENTRAL_CLIENT_ID non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        if not bc_connector.client_secret:
            flash('BUSINESS_CENTRAL_CLIENT_SECRET non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        import secrets
        state = secrets.token_urlsafe(32)
        auth_url = bc_connector.get_authorization_url(state)

        # Sauvegarder l'état en session
        session['bc_state'] = state

        return redirect(auth_url)

    except Exception as e:
        current_app.logger.error(f"Error connecting Business Central: {e}")
        flash('Une erreur est survenue lors de la connexion Business Central. Veuillez reessayer.', 'error')
        return redirect(url_for('company.business_central_config'))

@company_bp.route('/business-central/oauth-callback')
def business_central_callback():
    """Gérer le callback OAuth de Business Central"""
    from app import db
    from models import AccountingConnection, BusinessCentralConfig
    from business_central_connector import BusinessCentralConnector
    from flask_login import current_user

    # Récupérer les paramètres du callback
    authorization_code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    # Vérifier les erreurs
    if error:
        flash(f'Erreur Business Central: {error}', 'error')
        # Si l'utilisateur n'est pas connecté, rediriger vers login
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        return redirect(url_for('company.business_central_config'))

    # Vérifier si l'utilisateur est connecté
    if not current_user.is_authenticated:
        # Sauvegarder les paramètres OAuth dans la session pour après connexion
        session['bc_oauth_code'] = authorization_code
        session['bc_oauth_state'] = state
        flash('Veuillez vous connecter pour terminer la configuration Business Central.', 'info')
        return redirect(url_for('auth.login'))

    # Vérifier l'état de sécurité
    if state != session.get('bc_state'):
        flash('État de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('company.business_central_config'))

    # Récupérer l'entreprise depuis la session
    company_id = session.get('bc_company_id')
    if not company_id:
        # Essayer de récupérer l'entreprise sélectionnée
        company = current_user.get_selected_company()
        if company:
            company_id = company.id
        else:
            flash('Session expirée. Veuillez réessayer.', 'error')
            return redirect(url_for('company.business_central_config'))

    try:
        # Échanger le code pour les tokens
        bc_connector = BusinessCentralConnector()

        if not authorization_code:
            flash('Code d\'autorisation manquant.', 'error')
            return redirect(url_for('company.business_central_config'))

        if not state:
            flash('État de sécurité manquant.', 'error')
            return redirect(url_for('company.business_central_config'))

        token_data = bc_connector.exchange_code_for_tokens(authorization_code, state)

        # Vérifier si c'est une reconnexion
        reconnect_connection_id = session.get('bc_reconnect_connection_id')
        if reconnect_connection_id:
            # Mode reconnexion - mettre à jour seulement les tokens
            existing_connection = AccountingConnection.query.get(reconnect_connection_id)
            if existing_connection and existing_connection.system_type == 'business_central':
                existing_connection.access_token = token_data['access_token']
                existing_connection.refresh_token = token_data['refresh_token']
                existing_connection.token_expires_at = token_data['expires_at']
                existing_connection.is_active = True

                db.session.commit()

                # Nettoyer la session
                session.pop('bc_state', None)
                session.pop('bc_reconnect_connection_id', None)

                flash('Business Central reconnecté avec succès. Le mapping existant a été préservé.', 'success')
                return redirect(url_for('company.settings'))
            else:
                flash('Connexion non trouvée pour la reconnexion.', 'error')
                return redirect(url_for('company.settings'))

        # Mode connexion normale - vérifier si une connexion existe déjà
        existing_connection = AccountingConnection.query.filter_by(
            company_id=company_id,
            system_type='business_central'
        ).first()

        if existing_connection:
            # Mettre à jour la connexion existante
            existing_connection.access_token = token_data['access_token']
            existing_connection.refresh_token = token_data['refresh_token']
            existing_connection.token_expires_at = token_data['expires_at']
            existing_connection.is_active = True
            existing_connection.system_name = 'Microsoft Business Central'
            connection = existing_connection
        else:
            # Créer une nouvelle connexion
            connection = AccountingConnection()
            connection.company_id = company_id
            connection.system_type = 'business_central'
            connection.system_name = 'Microsoft Business Central'
            connection.access_token = token_data['access_token']
            connection.refresh_token = token_data['refresh_token']
            connection.token_expires_at = token_data['expires_at']
            connection.is_active = True
            db.session.add(connection)

        db.session.flush()  # Pour obtenir l'ID de connexion

        customers_url = session.get('bc_customers_url')
        invoices_url = session.get('bc_invoices_url')

        if customers_url and invoices_url:
            bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

            if not bc_config:
                bc_config = BusinessCentralConfig()
                bc_config.connection_id = connection.id
                bc_config.customers_odata_url = customers_url
                bc_config.invoices_odata_url = invoices_url
                db.session.add(bc_config)
            else:
                bc_config.customers_odata_url = customers_url
                bc_config.invoices_odata_url = invoices_url

        db.session.commit()

        session.pop('bc_company_id', None)
        session.pop('bc_state', None)
        session.pop('bc_customers_url', None)
        session.pop('bc_invoices_url', None)

        flash('Connexion Business Central établie avec succès !', 'success')

        # Rediriger vers la page de mapping des champs
        return redirect(url_for('company.business_central_field_mapping'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in Business Central callback: {e}")
        flash('Une erreur est survenue lors de la connexion Business Central. Veuillez reessayer.', 'error')
        return redirect(url_for('company.business_central_config'))

@company_bp.route('/business-central/field-mapping')
@login_required
def business_central_field_mapping():
    """Page de mapping des champs et configuration des filtres OData"""
    from models import AccountingConnection, BusinessCentralConfig
    from business_central_connector import BusinessCentralConnector

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Récupérer la connexion Business Central
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    # Mode démo si pas de connexion - on peut quand même voir l'interface de mapping
    if not connection:
        flash('Mode démonstration - Configurez d\'abord la connexion Business Central pour synchroniser.', 'warning')
        # Créer une configuration fictive pour l'affichage
        from collections import namedtuple
        DemoConfig = namedtuple('DemoConfig', ['customers_filter', 'invoices_filter', 'customers_odata_url', 'invoices_odata_url'])
        bc_config = DemoConfig(customers_filter='', invoices_filter='', customers_odata_url='', invoices_odata_url='')
    else:
        # Récupérer la configuration OData
        bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

        if not bc_config or not bc_config.customers_odata_url:
            flash('Configuration OData manquante - Interface de mapping disponible pour préparer la configuration.', 'warning')
            # Configuration par défaut pour l'affichage
            from collections import namedtuple
            DemoConfig = namedtuple('DemoConfig', ['customers_filter', 'invoices_filter', 'customers_odata_url', 'invoices_odata_url'])
            bc_config = DemoConfig(customers_filter='', invoices_filter='', customers_odata_url='', invoices_odata_url='')

    customer_headers = None
    invoice_headers = None

    # Essayer de récupérer les en-têtes seulement si on a une vraie connexion
    if connection and bc_config and hasattr(bc_config, 'customers_odata_url') and bc_config.customers_odata_url:
        try:
            bc_connector = BusinessCentralConnector(connection.id)
            customer_headers = bc_connector.get_table_headers(bc_config.customers_odata_url)
            invoice_headers = bc_connector.get_table_headers(bc_config.invoices_odata_url)
        except Exception as e:
            current_app.logger.error(f"Error fetching BC headers: {e}")
            flash('Impossible de recuperer les donnees Business Central. Veuillez reessayer.', 'warning')

    # Récupérer le mapping existant ou utiliser les valeurs par défaut
    field_mapping = connection.get_field_mapping()

    # Charger les champs disponibles Business Central
    # Plus de champs statiques - tout est dynamique

    # Si pas de mapping existant, utiliser les défauts
    if not field_mapping:
        field_mapping = {}

    return render_template('company/business_central_mapping.html',
                         company=company,
                         connection=connection,
                         bc_config=bc_config,
                         customer_headers=customer_headers,
                         invoice_headers=invoice_headers,
                         field_mapping=field_mapping,
                         bc_customer_fields=[],
                         bc_invoice_fields=[])

@company_bp.route('/business-central/field-mapping-improved')
@login_required
def business_central_field_mapping_improved():
    """Interface améliorée de mapping des champs avec listes déroulantes"""
    from models import AccountingConnection, BusinessCentralConfig
    from business_central_connector import BusinessCentralConnector
    # Plus de champs statiques - tout est dynamique

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Récupérer la connexion Business Central
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    if not connection:
        flash('Aucune connexion Business Central active trouvée.', 'error')
        return redirect(url_for('company.business_central_config'))

    # Récupérer la configuration OData
    bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

    if not bc_config or not bc_config.customers_odata_url:
        flash('Configuration OData manquante. Veuillez reconfigurer.', 'error')
        return redirect(url_for('company.business_central_config'))

    # Récupérer le mapping existant ou utiliser les valeurs par défaut
    field_mapping = connection.get_field_mapping()

    # Si pas de mapping existant, utiliser les défauts
    if not field_mapping:
        field_mapping = {}

    # Essayer de récupérer un aperçu des données (optionnel)
    customer_headers = None
    try:
        bc_connector = BusinessCentralConnector(connection.id)
        customer_headers = bc_connector.get_table_headers(bc_config.customers_odata_url)
    except Exception:
        pass  # Pas grave si ça échoue, on a déjà les champs définis

    return render_template('company/business_central_mapping_improved.html',
                         company=company,
                         connection=connection,
                         bc_config=bc_config,
                         field_mapping=field_mapping,
                         bc_customer_fields=[],
                         bc_invoice_fields=[],
                         customer_headers=customer_headers)

@company_bp.route('/business-central/field-mapping-v2')
@login_required
def business_central_field_mapping_v2():
    """Interface V2 améliorée avec 6 champs d'adresse et mapping de langue"""
    from models import AccountingConnection, BusinessCentralConfig
    # Plus de champs statiques - tout est dynamique

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Récupérer la connexion Business Central (optionnelle en mode démo)
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    # Mode démo si pas de connexion
    if not connection:
        from collections import namedtuple
        DemoConfig = namedtuple('DemoConfig', ['customers_filter', 'invoices_filter', 'customers_odata_url', 'invoices_odata_url', 'bc_company_guid'])
        bc_config = DemoConfig(customers_filter='', invoices_filter='', customers_odata_url='', invoices_odata_url='', bc_company_guid='')
        field_mapping = {
            'language_mapping_fr': 'Français',
            'language_mapping_en': 'Anglais',
            'client_language_field': 'Language_Code'
        }
    else:
        # Récupérer la configuration OData
        bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

        if not bc_config or not bc_config.customers_odata_url:
            from collections import namedtuple
            DemoConfig = namedtuple('DemoConfig', ['customers_filter', 'invoices_filter', 'customers_odata_url', 'invoices_odata_url', 'bc_company_guid'])
            bc_config = DemoConfig(customers_filter='', invoices_filter='', customers_odata_url='', invoices_odata_url='', bc_company_guid='')

        # Récupérer le mapping existant
        field_mapping = connection.get_field_mapping()

        if not field_mapping:
            field_mapping = {
                'language_mapping_fr': 'Français',
                'language_mapping_en': 'Anglais',
                'client_language_field': 'Language_Code'
            }
        else:
            # Ajouter les valeurs par défaut pour le mapping de langue si manquant
            if 'language_mapping_fr' not in field_mapping:
                field_mapping['language_mapping_fr'] = 'Français'
            if 'language_mapping_en' not in field_mapping:
                field_mapping['language_mapping_en'] = 'Anglais'
            if 'client_language_field' not in field_mapping:
                field_mapping['client_language_field'] = 'Language_Code'

    # Récupérer les champs disponibles dynamiquement depuis Business Central
    customer_fields = []
    invoice_fields = []

    if connection and bc_config and hasattr(bc_config, 'customers_odata_url') and bc_config.customers_odata_url:
        try:
            # Récupérer dynamiquement les champs depuis Business Central
            from business_central_connector import BusinessCentralConnector
            bc_connector = BusinessCentralConnector(connection.id)

            # Champs clients
            try:
                dynamic_customer_fields = bc_connector.get_available_fields(bc_config.customers_odata_url)
                customer_fields = [(field, field) for field in dynamic_customer_fields]
                current_app.logger.info(f"Retrieved {len(customer_fields)} customer fields dynamically")
            except Exception as e:
                current_app.logger.warning(f"Failed to get dynamic customer fields: {e}")
                customer_fields = []

            # Champs factures
            if bc_config.invoices_odata_url:
                try:
                    dynamic_invoice_fields = bc_connector.get_available_fields(bc_config.invoices_odata_url)
                    invoice_fields = [(field, field) for field in dynamic_invoice_fields]
                    current_app.logger.info(f"Retrieved {len(invoice_fields)} invoice fields dynamically")
                except Exception as e:
                    current_app.logger.warning(f"Failed to get dynamic invoice fields: {e}")
                    invoice_fields = []

        except Exception as e:
            current_app.logger.error(f"Error initializing BC connector for field discovery: {e}")
            # Fallback aux champs prédéfinis
            customer_fields = []
            invoice_fields = []
    else:
        # Pas de connexion ou configuration, utiliser les champs prédéfinis
        customer_fields = []
        invoice_fields = []

    return render_template('company/business_central_mapping_v2.html',
                         company=company,
                         connection=connection,
                         bc_config=bc_config,
                         field_mapping=field_mapping,
                         bc_customer_fields=customer_fields,
                         bc_invoice_fields=invoice_fields)

@company_bp.route('/business-central/save-mapping', methods=['POST'])
@login_required
def business_central_save_mapping():
    """Sauvegarder le mapping des champs et les filtres OData"""
    from app import db
    from models import AccountingConnection, BusinessCentralConfig

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Récupérer la connexion
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    if not connection:
        flash('Aucune connexion Business Central active trouvée.', 'error')
        return redirect(url_for('company.settings'))

    # Récupérer la configuration
    bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

    if not bc_config:
        flash('Configuration Business Central non trouvée.', 'error')
        return redirect(url_for('company.settings'))

    try:
        # Construire l'adresse à partir des 6 champs (template v2 avec dropdowns)
        # OU utiliser le champ client_address directement (template improved avec champ texte)
        address_fields = []
        for i in range(1, 7):
            field = request.form.get(f'address_field_{i}', '')
            if field:
                address_fields.append(field)

        if address_fields:
            # Template v2 avec dropdowns
            client_address = ','.join(address_fields)
        else:
            # Template improved avec champ texte, ou valeur par défaut
            client_address = request.form.get('client_address', 'Address,Address_2,City,Post_Code,County,Country_Region_Code')

        # Construire le mapping des champs depuis le formulaire
        field_mapping = {
            # Champs clients
            'client_code': request.form.get('client_code', 'No'),
            'client_name': request.form.get('client_name', 'Name'),
            'client_email': request.form.get('client_email', 'E_Mail'),
            'client_phone': request.form.get('client_phone', 'Phone_No'),
            'client_address': client_address,
            'client_representative': request.form.get('client_representative', 'Salesperson_Code'),
            'client_payment_terms': request.form.get('client_payment_terms', 'Payment_Terms_Code'),
            'client_parent_code': request.form.get('client_parent_code', ''),
            'client_language_field': request.form.get('client_language_field', 'Language_Code'),
            # Mapping de langue FR/EN
            'language_mapping_fr': request.form.get('language_mapping_fr', 'Français'),
            'language_mapping_en': request.form.get('language_mapping_en', 'Anglais'),
            # Champs factures
            'invoice_customer_code': request.form.get('invoice_customer_code', 'Sell_to_Customer_No'),
            'invoice_number': request.form.get('invoice_number', 'No'),
            'invoice_amount': request.form.get('invoice_amount', 'Amount_Including_VAT'),
            'invoice_original_amount': request.form.get('invoice_original_amount', ''),
            'invoice_date': request.form.get('invoice_date', 'Document_Date'),
            'invoice_due_date': request.form.get('invoice_due_date', 'Due_Date'),
            'invoice_balance': request.form.get('invoice_balance', 'Remaining_Amount'),
            # Champ Delta Sync
            'delta_field': request.form.get('delta_field', '')
        }

        # Sauvegarder le mapping
        connection.set_field_mapping(field_mapping)

        # Configurer le Delta Sync
        delta_field = request.form.get('delta_field', '').strip()
        if delta_field:
            connection.delta_field = delta_field
            connection.delta_enabled = True
        else:
            connection.delta_field = None
            connection.delta_enabled = False

        # Sauvegarder les filtres OData
        bc_config.customers_filter = request.form.get('customers_filter', '').strip()
        bc_config.invoices_filter = request.form.get('invoices_filter', '').strip()

        bc_config.customers_orderby_field = request.form.get('customers_orderby_field', '').strip()
        bc_config.invoices_orderby_field = request.form.get('invoices_orderby_field', '').strip()

        # bc_company_guid est géré exclusivement par l'auto-détection du connecteur BC.
        # On le remet à NULL à chaque sauvegarde du formulaire pour forcer une re-détection
        # propre au prochain téléchargement PDF (évite un mauvais GUID persistant).
        bc_config.bc_company_guid = None

        db.session.commit()

        flash('Configuration sauvegardée avec succès !', 'success')
        return redirect(url_for('company.settings'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving BC field mapping: {e}")
        flash('Une erreur est survenue lors de la sauvegarde. Veuillez reessayer.', 'error')
        return redirect(url_for('company.business_central_field_mapping'))

@company_bp.route('/business-central/disconnect/<int:connection_id>', methods=['POST'])
@login_required
def business_central_disconnect(connection_id):
    """Déconnecter Business Central"""
    from app import db
    from models import AccountingConnection, BusinessCentralConfig

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id,
        company_id=company.id,
        system_type='business_central'
    ).first()

    if not connection:
        flash('Connexion Business Central non trouvée.', 'error')
        return redirect(url_for('company.settings'))

    try:
        current_app.logger.info(f'🔌 DEBUT DECONNEXION Business Central - Connection ID: {connection.id}, Company: {company.name}')

        # IMPORTANT: Une déconnexion ne supprime QUE les informations de connexion
        # Les données clients/factures/notes sont PRESERVEES

        # 1. Supprimer les logs de synchronisation pour éviter les contraintes FK
        from models import SyncLog
        sync_logs_deleted = SyncLog.query.filter_by(connection_id=connection.id).delete()

        # 2. Supprimer la configuration Business Central (mapping, URLs, filtres)
        config_deleted = BusinessCentralConfig.query.filter_by(connection_id=connection.id).delete()

        # 3. Supprimer la connexion accounting (tokens, credentials)
        connection_id_to_delete = connection.id
        db.session.delete(connection)

        # COMMIT - Application des suppressions
        db.session.commit()

        # Vérification post-suppression
        remaining_connections = AccountingConnection.query.filter_by(
            company_id=company.id,
            system_type='business_central'
        ).count()

        remaining_configs = BusinessCentralConfig.query.filter_by(connection_id=connection_id_to_delete).count()


        if remaining_connections == 0 and remaining_configs == 0:
            log_action(AuditActions.OAUTH_DISCONNECTED, entity_type=EntityTypes.OAUTH,
                      entity_id=connection_id_to_delete, entity_name='business_central',
                      details={'company_id': company.id})
            flash('Business Central déconnecté avec succès. Vos données clients et factures sont préservées.', 'success')
        else:
            current_app.logger.error(f'❌ PROBLEME: Il reste {remaining_connections} connexions et {remaining_configs} configs!')
            flash('Déconnexion partielle - veuillez contacter le support.', 'warning')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'❌ ERREUR DECONNEXION Business Central: {str(e)}')
        current_app.logger.error(f'❌ Type erreur: {type(e).__name__}')
        import traceback
        current_app.logger.error(f'❌ Traceback: {traceback.format_exc()}')
        flash('Une erreur est survenue lors de la deconnexion. Veuillez reessayer.', 'error')

    return redirect(url_for('company.settings') + '#accounting')

@company_bp.route('/business-central/reconnect/<int:connection_id>', methods=['GET'])
@login_required
def business_central_reconnect(connection_id):
    """Reconnecter Business Central en préservant le mapping existant"""
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id,
        company_id=company.id,
        system_type='business_central'
    ).first()

    if not connection:
        flash('Connexion Business Central non trouvée.', 'error')
        return redirect(url_for('company.settings'))

    try:
        # Initier nouvelle authentification OAuth sans supprimer le mapping
        from business_central_connector import BusinessCentralConnector
        bc_connector = BusinessCentralConnector()

        if not bc_connector.client_id:
            flash('BUSINESS_CENTRAL_CLIENT_ID non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        if not bc_connector.client_secret:
            flash('BUSINESS_CENTRAL_CLIENT_SECRET non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        import secrets
        state = secrets.token_urlsafe(32)
        auth_url = bc_connector.get_authorization_url(state)

        # Sauvegarder l'état en session pour reconnexion
        session['bc_state'] = state
        session['bc_reconnect_connection_id'] = connection_id

        current_app.logger.info(f'Business Central reconnection initiated for company {company.name}')
        return redirect(auth_url)

    except Exception as e:
        current_app.logger.error(f'Error reconnecting Business Central: {e}')
        flash('Une erreur est survenue lors de la reconnexion. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings'))

@company_bp.route('/business-central/sync-health', methods=['GET'])
@login_required
def business_central_sync_health():
    """Vérifier la santé des synchronisations et corriger les blocages"""
    from sync_monitor import check_sync_health
    from flask import jsonify

    try:
        # Forcer une vérification de santé
        results = check_sync_health()

        return jsonify({
            'success': True,
            'stuck_syncs_found': results['stuck_syncs_found'],
            'stuck_syncs_fixed': results['stuck_syncs_fixed'],
            'errors': results['errors'],
            'message': f"Vérification terminée. {results['stuck_syncs_fixed']} synchronisations bloquées corrigées."
        })

    except Exception as e:
        current_app.logger.error(f"Error in sync health check: {e}")
        return jsonify({
            'success': False,
            'error': 'Une erreur interne est survenue lors de la verification.'
        }), 500

@company_bp.route('/business-central/preview-data', methods=['POST'])
@login_required
def business_central_preview_data():
    """Récupérer un échantillon des données Business Central pour aperçu"""
    from models import AccountingConnection, BusinessCentralConfig
    from business_central_connector import BusinessCentralConnector

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 403

    if not current_user.can_access_company_settings():
        return jsonify({'error': 'Accès refusé'}), 403

    # Récupérer le type de données demandé
    data_type = request.form.get('type', 'customers')  # 'customers' ou 'invoices'

    # Récupérer la connexion
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    if not connection:
        return jsonify({'error': 'Aucune connexion Business Central active'}), 404

    # Récupérer la configuration
    bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

    if not bc_config:
        return jsonify({'error': 'Configuration Business Central non trouvée'}), 404

    try:
        bc_connector = BusinessCentralConnector(connection.id)

        # Récupérer l'URL appropriée
        if data_type == 'customers':
            url = bc_config.customers_odata_url
            filter_str = bc_config.customers_filter
        else:
            url = bc_config.invoices_odata_url
            filter_str = bc_config.invoices_filter

        if not url:
            return jsonify({'error': f'URL {data_type} non configurée'}), 400

        # Appliquer le filtre si présent
        if filter_str:
            url = bc_connector.apply_odata_filter(url, filter_str)

        # Ajouter la limite de 10 lignes
        separator = '&' if '?' in url else '?'
        url = f"{url}{separator}$top=10"

        # Récupérer les données
        data = bc_connector.fetch_data_page(url, None)

        if data and 'value' in data:
            # Prendre seulement les 10 premières lignes
            sample_data = data['value'][:10]

            # Extraire les colonnes disponibles
            columns = []
            if sample_data:
                columns = list(sample_data[0].keys())

            return jsonify({
                'success': True,
                'data': sample_data,
                'columns': columns,
                'count': len(sample_data),
                'type': data_type
            })
        else:
            return jsonify({'error': 'Aucune donnée trouvée'}), 404

    except Exception as e:
        current_app.logger.error(f"Erreur aperçu données: {str(e)}")
        return jsonify({'error': 'Erreur lors de la récupération des données.'}), 500

@company_bp.route('/check-sync-status', methods=['GET'])
@login_required
def check_sync_status():
    """Vérifier si une synchronisation est en cours"""
    from models import AccountingConnection, SyncLog
    from flask import jsonify

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 403

    # Récupérer la connexion
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    if not connection:
        return jsonify({'sync_in_progress': False})

    # Vérifier si une synchronisation est en cours
    running_sync = SyncLog.query.filter_by(
        connection_id=connection.id,
        status='running'
    ).first()

    if running_sync:
        return jsonify({
            'sync_in_progress': True,
            'message': 'Une synchronisation est déjà en cours pour votre entreprise.'
        })

    # Vérifier aussi le nombre global de syncs
    global_running_syncs = SyncLog.query.filter_by(status='running').count()
    MAX_CONCURRENT_SYNCS = 3

    if global_running_syncs >= MAX_CONCURRENT_SYNCS:
        return jsonify({
            'sync_in_progress': True,
            'message': f'Trop de synchronisations en cours ({global_running_syncs}/{MAX_CONCURRENT_SYNCS}). Veuillez réessayer dans quelques minutes.'
        })

    return jsonify({'sync_in_progress': False})

@company_bp.route('/sync-status', methods=['GET'])
@login_required
def sync_status():
    """Endpoint API pour obtenir le statut détaillé de la synchronisation en cours"""
    from models import AccountingConnection, SyncLog
    from flask import jsonify
    from datetime import datetime

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 403

    # Récupérer la connexion Business Central
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    if not connection:
        return jsonify({'in_progress': False, 'message': 'Aucune connexion Business Central'})

    # Récupérer la synchronisation en cours ou la dernière
    running_sync = SyncLog.query.filter_by(
        connection_id=connection.id,
        status='running'
    ).first()

    if running_sync:
        # Calculer le pourcentage de progression
        progress_percent = 0
        if running_sync.estimated_total and running_sync.estimated_total > 0:
            progress_percent = min(100, int((running_sync.items_processed or 0) * 100 / running_sync.estimated_total))

        # Calculer l'ETA
        eta_str = "Calcul en cours..."
        if running_sync.estimated_completion:
            eta_seconds = (running_sync.estimated_completion - datetime.utcnow()).total_seconds()
            if eta_seconds > 0:
                eta_minutes = int(eta_seconds / 60)
                if eta_minutes > 60:
                    eta_str = f"{int(eta_minutes / 60)}h {eta_minutes % 60}min"
                else:
                    eta_str = f"{eta_minutes} min"
            else:
                eta_str = "Presque terminé"

        # Calculer le taux de traitement
        processing_rate = running_sync.processing_rate or 0

        return jsonify({
            'in_progress': True,
            'entity_type': running_sync.entity_type or 'general',
            'items_processed': running_sync.items_processed or 0,
            'pages_processed': running_sync.pages_processed or 0,
            'estimated_total': running_sync.estimated_total or 0,
            'progress_percent': progress_percent,
            'eta': eta_str,
            'processing_rate': round(processing_rate, 1),
            'last_activity': running_sync.last_activity_at.isoformat() if running_sync.last_activity_at else None,
            'started_at': running_sync.started_at.isoformat() if running_sync.started_at else None,
            'is_delta_sync': running_sync.is_delta_sync or False,
            'can_resume': running_sync.can_resume or False
        })
    else:
        # Chercher la dernière synchronisation terminée
        last_sync = SyncLog.query.filter_by(
            connection_id=connection.id
        ).filter(
            SyncLog.status.in_(['completed', 'failed', 'partial'])
        ).order_by(SyncLog.completed_at.desc()).first()

        if last_sync:
            duration = None
            if last_sync.started_at and last_sync.completed_at:
                duration = int((last_sync.completed_at - last_sync.started_at).total_seconds() / 60)

            return jsonify({
                'in_progress': False,
                'last_sync': {
                    'status': last_sync.status,
                    'entity_type': last_sync.entity_type or 'general',
                    'clients_synced': last_sync.clients_synced or 0,
                    'invoices_synced': last_sync.invoices_synced or 0,
                    'errors_count': last_sync.errors_count or 0,
                    'completed_at': last_sync.completed_at.isoformat() if last_sync.completed_at else None,
                    'duration_minutes': duration,
                    'was_delta_sync': last_sync.is_delta_sync or False
                }
            })
        else:
            return jsonify({
                'in_progress': False,
                'message': 'Aucune synchronisation effectuée'
            })

@company_bp.route('/business-central/sync', methods=['POST'])
@login_required
def business_central_sync():
    """Déclencher la synchronisation Business Central - VERSION ASYNCHRONE AMÉLIORÉE"""
    from app import db
    from models import AccountingConnection, BusinessCentralConfig, SyncLog, Notification
    from business_central_connector import BusinessCentralConnector
    from datetime import datetime
    import threading

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Récupérer la connexion
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='business_central',
        is_active=True
    ).first()

    if not connection:
        flash('❌ Aucune connexion Business Central active trouvée.', 'error')
        return redirect(url_for('company.settings'))

    # Récupérer la configuration
    bc_config = BusinessCentralConfig.query.filter_by(connection_id=connection.id).first()

    if not bc_config:
        flash('❌ Configuration Business Central non trouvée.', 'error')
        return redirect(url_for('company.settings'))

    # 🔥 NOUVELLE VÉRIFICATION PRÉALABLE: Tester la connexion avant de lancer la sync
    bc_connector = BusinessCentralConnector(connection.id)

    # Vérifier que les URLs sont configurées
    sync_type = request.form.get('sync_type', 'customers')
    if sync_type == 'customers':
        test_url = bc_config.customers_odata_url
    else:
        test_url = bc_config.invoices_odata_url

    if not test_url:
        flash(f'❌ URL de synchronisation {sync_type} non configurée.', 'error')
        return redirect(url_for('company.settings'))

    test_key = 'invoices' if sync_type == 'payments' else sync_type
    try:
        test_success, test_message = bc_connector.test_connection({test_key: test_url})

        if not test_success:
            # Erreur d'authentification détectée - Action immédiate requise
            if "Authentication failed" in test_message or "401" in test_message:
                current_app.logger.error(f"❌ Erreur d'authentification détectée: {test_message}")

                # Notification immédiate aux super admins
                from models import User, UserCompany
                super_admins = User.query.join(UserCompany).filter(
                    UserCompany.company_id == company.id,
                    UserCompany.role == 'super_admin',
                    UserCompany.is_active == True
                ).all()

                for admin in super_admins:
                    Notification.create_notification(
                        user_id=admin.id,
                        company_id=company.id,
                        type='business_central_auth_error',
                        title='🔑 Reconnexion Business Central requise',
                        message=f'La synchronisation {sync_type} a échoué. Votre token d\'authentification Business Central a expiré ou été révoqué. Veuillez reconnecter Business Central dans les paramètres de l\'entreprise.'
                    )

                flash('🔑 Erreur d\'authentification Business Central. Une notification a été envoyée aux super administrateurs pour reconnecter le système.', 'error')
                return redirect(url_for('company.settings') + '#accounting')
            else:
                # Autre erreur (404, configuration, etc.)
                current_app.logger.error(f"❌ Erreur de configuration détectée: {test_message}")
                flash(f'❌ Erreur de configuration: {test_message}', 'error')
                return redirect(url_for('company.settings') + '#accounting')

    except Exception as e:
        current_app.logger.error(f"Error testing BC connection: {e}")
        flash('Impossible de tester la connexion Business Central. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings') + '#accounting')

    # Vérifier qu'une synchronisation n'est pas déjà en cours pour cette entreprise
    running_sync = SyncLog.query.filter_by(
        connection_id=connection.id,
        status='running'
    ).first()

    if running_sync:
        flash('⚠️ Une synchronisation est déjà en cours pour votre entreprise. Veuillez patienter.', 'warning')
        return redirect(url_for('company.settings') + '#accounting')

    # PROTECTION SERVEUR: Limiter le nombre de synchronisations simultanées globales
    global_running_syncs = SyncLog.query.filter_by(status='running').count()
    MAX_CONCURRENT_SYNCS = 3  # Maximum 3 entreprises peuvent synchroniser en même temps

    if global_running_syncs >= MAX_CONCURRENT_SYNCS:
        flash(f'⚠️ Trop de synchronisations en cours ({global_running_syncs}/{MAX_CONCURRENT_SYNCS}). Veuillez réessayer dans quelques minutes.', 'warning')
        current_app.logger.warning(f"Sync request rejected: {global_running_syncs}/{MAX_CONCURRENT_SYNCS} syncs running")
        return redirect(url_for('company.settings') + '#accounting')

    # Récupérer le type de synchronisation demandé
    sync_type = request.form.get('sync_type', 'customers')  # 'customers' ou 'invoices'

    # Créer un log de synchronisation avec support checkpoint
    now = datetime.utcnow()
    sync_log = SyncLog(
        connection_id=connection.id,
        sync_type='manual',
        status='running',
        started_at=now,
        last_activity_at=now,  # Initialiser pour éviter faux positif du sync_monitor
        last_processed_page=0,  # Initialiser les champs checkpoint
        last_processed_skip=0,
        can_resume=False
    )
    db.session.add(sync_log)
    db.session.commit()

    # Sauvegarder les IDs pour le thread
    connection_id = connection.id
    bc_config_id = bc_config.id
    sync_log_id = sync_log.id
    field_mapping = connection.get_field_mapping()

    # Fonction de synchronisation exécutée en arrière-plan
    def run_async_sync():
        """Synchronisation asynchrone en arrière-plan"""
        from app import app
        import time

        with app.app_context():
            sync_start_time = time.time()
            MAX_SYNC_DURATION = 3600  # Maximum 1 heure par synchronisation

            try:
                # HEARTBEAT: Marquer la synchronisation comme active
                sync_log = SyncLog.query.get(sync_log_id)
                if not sync_log:
                    raise Exception("Sync log not found - synchronization aborted")

                # Recharger les objets dans ce contexte
                connection = AccountingConnection.query.get(connection_id)
                bc_config = BusinessCentralConfig.query.get(bc_config_id)

                if not connection or not bc_config:
                    sync_log.status = 'failed'
                    sync_log.completed_at = datetime.utcnow()
                    sync_log.error_message = 'Configuration manquante'
                    db.session.commit()
                    raise Exception("Connection or config not found")

                bc_connector = BusinessCentralConnector(connection_id)

                # Injecter l'ID du sync log pour les mises à jour progressives
                setattr(bc_connector, 'sync_log_id', sync_log_id)


                total_stats = {'created': 0, 'updated': 0, 'errors': 0}

                if sync_type == 'customers':
                    # Synchroniser les clients avec support checkpoint
                    customer_stats = bc_connector.sync_customers(
                        bc_config.customers_odata_url,
                        bc_config.customers_filter,
                        field_mapping,
                        sync_log_id=sync_log.id,
                        orderby_field=bc_config.customers_orderby_field
                    )
                    total_stats = customer_stats

                elif sync_type == 'invoices':
                    # Synchroniser les factures avec support checkpoint
                    invoice_stats = bc_connector.sync_invoices(
                        bc_config.invoices_odata_url,
                        bc_config.invoices_filter,
                        field_mapping,
                        sync_log_id=sync_log.id,
                        orderby_field=bc_config.invoices_orderby_field
                    )
                    total_stats = invoice_stats

                elif sync_type == 'payments':
                    if not bc_config.invoices_odata_url:
                        current_app.logger.warning(
                            "BC payment sync: invoices_odata_url manquante"
                        )
                        raise ValueError(
                            "URL OData des factures non configurée — "
                            "nécessaire pour la synchronisation des paiements."
                        )
                    else:
                        payments_created = bc_connector.sync_payments(
                            company_id=connection.company_id,
                            sync_log_id=sync_log.id
                        )
                        total_stats = {
                            'created': payments_created,
                            'updated': 0,
                            'errors': 0
                        }

                # Vérifier le temps écoulé
                elapsed_time = time.time() - sync_start_time

                # Déterminer le statut final
                if elapsed_time >= MAX_SYNC_DURATION:
                    final_status = 'partial'
                    status_message = f"Synchronisation limitée après {int(elapsed_time/60)} minutes"
                else:
                    final_status = 'completed'
                    status_message = f"Synchronisation complète en {int(elapsed_time/60)} minutes"

                # Mettre à jour le log de synchronisation (protégé car le connector peut avoir déjà modifié le status)
                try:
                    sync_log = SyncLog.query.get(sync_log_id)
                    if sync_log:
                        # Toujours mettre à jour les stats
                        sync_log.clients_synced = total_stats.get('created', 0) + total_stats.get('updated', 0)
                        sync_log.error_count = total_stats.get('errors', 0)

                        # Le connector peut avoir déjà mis 'completed', on ne change que si partial ou pas encore terminé
                        if sync_log.status != 'completed' or final_status == 'partial':
                            sync_log.status = final_status
                            sync_log.completed_at = datetime.utcnow()
                            if final_status == 'partial':
                                sync_log.error_message = status_message

                        db.session.commit()
                except Exception as sync_log_error:
                    current_app.logger.warning(f"Erreur mise à jour sync_log: {sync_log_error}")
                    db.session.rollback()

                # ENVOI DE NOTIFICATION DE FIN DE SYNC - TOUJOURS exécuté
                try:
                    from models import Notification, User, UserCompany, Company

                    # Recharger la connexion pour être sûr d'avoir les bonnes données
                    connection = AccountingConnection.query.get(connection_id)
                    if not connection:
                        current_app.logger.error(f"Connexion {connection_id} non trouvée pour notification")
                        return

                    # Trouver les utilisateurs admins de cette entreprise
                    admin_users = User.query.join(UserCompany).filter(
                        UserCompany.company_id == connection.company_id,
                        UserCompany.role.in_(['super_admin', 'admin']),
                        UserCompany.is_active == True
                    ).all()

                    for admin_user in admin_users:
                        # Message simple et direct
                        if final_status == 'completed':
                            title = 'Synchronisation terminée'
                            if sync_type == 'customers':
                                message = f"Synchronisation terminée. {total_stats.get('created', 0)} clients créés, {total_stats.get('updated', 0)} mis à jour."
                            elif sync_type == 'payments':
                                message = f"Synchronisation paiements terminée. {total_stats.get('created', 0)} paiements importés."
                            else:
                                message = f"Synchronisation terminée. {total_stats.get('created', 0)} factures créées, {total_stats.get('updated', 0)} mises à jour."
                        else:
                            title = 'Synchronisation partiellement terminée'
                            if sync_type == 'customers':
                                message = f"Synchronisation partiellement terminée. {total_stats.get('created', 0)} clients créés, {total_stats.get('updated', 0)} mis à jour."
                            elif sync_type == 'payments':
                                message = f"Synchronisation paiements partiellement terminée. {total_stats.get('created', 0)} paiements importés."
                            else:
                                message = f"Synchronisation partiellement terminée. {total_stats.get('created', 0)} factures créées, {total_stats.get('updated', 0)} mises à jour."

                        if total_stats.get('errors', 0) > 0:
                            message += f" {total_stats.get('errors', 0)} erreurs."

                        Notification.create_notification(
                            user_id=admin_user.id,
                            company_id=connection.company_id,
                            type='business_central_sync',
                            title=title,
                            message=message
                        )

                    current_app.logger.info(f"Notifications envoyées aux admins de l'entreprise {connection.company_id}")

                    # Log audit for sync completion
                    from models import AuditLog
                    AuditLog.log_with_session(
                        db.session,
                        action=AuditActions.SYNC_COMPLETED,
                        entity_type=EntityTypes.SYNC,
                        entity_name='Business Central',
                        details={
                            'sync_type': 'business_central',
                            'data_type': sync_type,
                            'stats': {
                                'created': total_stats.get('created', 0),
                                'updated': total_stats.get('updated', 0),
                                'errors': total_stats.get('errors', 0)
                            },
                            'status': final_status
                        },
                        user=admin_users[0] if admin_users else None,
                        company=Company.query.get(connection.company_id)
                    )
                except Exception as notif_error:
                    current_app.logger.error(f"ERREUR lors de l'envoi des notifications de succès: {notif_error}")

            except Exception as e:
                current_app.logger.error(f"❌ Erreur sync asynchrone {sync_type}: {str(e)}")
                import traceback
                current_app.logger.error(f"Traceback: {traceback.format_exc()}")

                # Détecter spécifiquement les erreurs d'authentification
                is_auth_error = "Authentication failed" in str(e) or "401" in str(e)

                # S'assurer que le sync_log est mis à jour même en cas d'erreur
                try:
                    sync_log = SyncLog.query.get(sync_log_id)
                    if sync_log and sync_log.status == 'running':
                        sync_log.status = 'failed'
                        sync_log.completed_at = datetime.utcnow()
                        sync_log.error_message = str(e)[:500]  # Limiter la taille du message d'erreur
                        db.session.commit()
                except Exception as log_error:
                    current_app.logger.error(f"Impossible de mettre à jour le sync_log: {log_error}")

                # NOTIFICATION D'ERREUR - OBLIGATOIRE avec priorité pour erreurs auth
                try:
                    from models import Notification, User, UserCompany
                    connection = AccountingConnection.query.get(connection_id)
                    if connection:
                        if is_auth_error:
                            # Erreur d'authentification - Notifier uniquement les super admins avec priorité
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
                                    title='🔴 URGENT: Reconnexion Business Central requise',
                                    message=f'La synchronisation {sync_type} a échoué. Token d\'authentification expiré. Reconnexion immédiate requise dans les paramètres de l\'entreprise.'
                                )

                            current_app.logger.error(f"🔴 Notifications d'authentification critiques envoyées aux super admins")
                        else:
                            # Erreur générale - Notifier tous les admins
                            admin_users = User.query.join(UserCompany).filter(
                                UserCompany.company_id == connection.company_id,
                                UserCompany.role.in_(['super_admin', 'admin']),
                                UserCompany.is_active == True
                            ).all()

                            for admin_user in admin_users:
                                # Message d'erreur détaillé
                                error_message = f"🚨 Synchronisation {sync_type} interrompue: {str(e)[:150]}. Vérifiez la configuration et relancez si nécessaire."

                                Notification.create_notification(
                                    user_id=admin_user.id,
                                    company_id=connection.company_id,
                                    type='business_central_sync_error',
                                    title='🚨 Erreur de synchronisation',
                                    message=error_message
                                )


                except Exception as notif_error:
                    current_app.logger.error(f"CRITIQUE: Impossible d'envoyer les notifications d'erreur: {notif_error}")

            finally:
                # Nettoyage final - s'assurer qu'aucun sync ne reste "running"
                try:
                    sync_log = SyncLog.query.get(sync_log_id)
                    if sync_log and sync_log.status == 'running':
                        sync_log.status = 'failed'
                        sync_log.completed_at = datetime.utcnow()
                        sync_log.error_message = "Synchronisation interrompue de manière inattendue"
                        db.session.commit()
                        current_app.logger.warning(f"Sync {sync_log_id} était toujours 'running' et a été marqué comme 'failed'")
                except Exception as cleanup_error:
                    current_app.logger.error(f"Erreur lors du nettoyage final: {cleanup_error}")

    # Vérifier le quota de synchronisation journalier
    from models import CompanySyncUsage
    if not CompanySyncUsage.check_company_sync_limit(company.id):
        flash('❌ Quota de synchronisations journalier atteint pour votre forfait. Veuillez réessayer demain ou passer à un forfait supérieur.', 'error')
        return redirect(url_for('company.settings') + '#accounting')

    CompanySyncUsage.increment_company_sync_count(company.id)

    # Démarrer le monitoring pour surveiller cette synchronisation
    from sync_monitor import ensure_monitoring_started
    ensure_monitoring_started()

    # Lancer le thread de synchronisation
    sync_thread = threading.Thread(target=run_async_sync, daemon=True)
    sync_thread.start()

    # Retourner immédiatement avec un message
    flash(f'🔄 Synchronisation {sync_type} lancée en arrière-plan. Elle continuera jusqu\'à la fin sans bloquer l\'interface. Vous recevrez une notification à la fin.', 'info')
    current_app.logger.info(f"Thread de synchronisation {sync_type} démarré pour {company.name} ({global_running_syncs + 1}/{MAX_CONCURRENT_SYNCS} syncs actives)")

    return redirect(url_for('company.settings') + '#accounting')


# ========================================
# End Business Central Routes
# ========================================

@company_bp.route('/delete-company', methods=['POST'])
@login_required
def delete_company():
    """Delete company"""
    from app import db
    from models import Company, UserCompany
    from flask_wtf.csrf import validate_csrf

    # Validation CSRF
    try:
        validate_csrf(request.form.get('csrf_token'))
    except Exception as e:
        flash('Token de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('company.settings'))

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.is_super_admin(company.id):
        flash('Seuls les super administrateurs peuvent supprimer une entreprise.', 'error')
        return redirect(url_for('company.settings'))

    # Validation du champ de confirmation (nom de l'entreprise)
    confirmation = request.form.get('confirmation', '').strip()
    if confirmation != company.name:
        flash('Le nom de l\'entreprise saisi ne correspond pas. Suppression annulée.', 'error')
        current_app.logger.warning(f"Tentative de suppression échouée pour {company.name}: confirmation invalide (reçu: '{confirmation}')")
        return redirect(url_for('company.settings'))

    # Annuler automatiquement l'abonnement Stripe (sans blocage)
    if company.stripe_subscription_id:
        try:
            import stripe
            subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)
            if subscription.status not in ['canceled', 'incomplete_expired']:
                stripe.Subscription.delete(company.stripe_subscription_id)
                current_app.logger.info(f"Abonnement Stripe annulé automatiquement pour {company.name}: {company.stripe_subscription_id}")
        except stripe.error.InvalidRequestError:
            # Abonnement déjà supprimé ou inexistant
            pass
        except Exception as stripe_error:
            current_app.logger.error(f"Erreur annulation Stripe pour {company.name}: {str(stripe_error)}")
            flash(f'Attention: Erreur lors de l\'annulation Stripe automatique.', 'warning')

    try:
        company_name = company.name
        company_id = company.id

        # Import models needed for cascade deletion
        from models import (
            EmailConfiguration, Client, Invoice, Notification, ClientContact,
            SubscriptionAuditLog, CommunicationNote, User
        )

        # 🚀 OPTIMISATION MAXIMALE : SQL brut pour éviter OOM et timeouts

        # 0. Delete company_sync_usage (IMPORTANT: avant autres suppressions)
        db.session.execute(db.text("""
            DELETE FROM company_sync_usage WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 1. Delete accounting-related data avec SQL brut
        db.session.execute(db.text("""
            DELETE FROM sync_logs
            WHERE connection_id IN (
                SELECT id FROM accounting_connections WHERE company_id = :company_id
            )
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM business_central_configs
            WHERE connection_id IN (
                SELECT id FROM accounting_connections WHERE company_id = :company_id
            )
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM accounting_connections WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 2b. Delete received payments (FK vers clients et companies)
        db.session.execute(db.text("""
            DELETE FROM received_payments WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 3. Delete client-related data avec SQL brut
        db.session.execute(db.text("""
            DELETE FROM client_contacts
            WHERE client_id IN (
                SELECT id FROM clients WHERE company_id = :company_id
            )
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM invoices
            WHERE client_id IN (
                SELECT id FROM clients WHERE company_id = :company_id
            )
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM clients WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4. Delete communication notes
        db.session.execute(db.text("""
            DELETE FROM communication_notes WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4b. Delete password setup tokens
        db.session.execute(db.text("""
            DELETE FROM password_setup_tokens WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4c. Delete campaign emails (before campaigns due to FK)
        db.session.execute(db.text("""
            DELETE FROM campaign_emails
            WHERE campaign_id IN (
                SELECT id FROM campaigns WHERE company_id = :company_id
            )
        """), {"company_id": company_id})

        # 4d. Delete campaigns
        db.session.execute(db.text("""
            DELETE FROM campaigns WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4e. Delete receivables snapshots
        db.session.execute(db.text("""
            DELETE FROM receivables_snapshots WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4f. Delete file import mappings
        db.session.execute(db.text("""
            DELETE FROM file_import_mappings WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4g. Delete import jobs
        db.session.execute(db.text("""
            DELETE FROM import_jobs WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4h. Clean audit logs (nullable FK - set to NULL)
        db.session.execute(db.text("""
            UPDATE audit_logs SET company_id = NULL WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 5. Delete company data
        db.session.execute(db.text("""
            DELETE FROM email_configurations WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM notifications WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM subscription_audit_log WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM webhook_logs WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM business_central_sync_logs WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM email_templates WHERE company_id = :company_id
        """), {"company_id": company_id})

        # Note: grace_periods table removed - functionality now in subscription_audit_log

        db.session.execute(db.text("""
            DELETE FROM import_history WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM user_profiles WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 6. Delete user associations and clean orphaned users
        user_companies = UserCompany.query.filter_by(company_id=company_id).all()
        users_to_check = [uc.user for uc in user_companies]

        db.session.execute(db.text("""
            DELETE FROM user_companies WHERE company_id = :company_id
        """), {"company_id": company_id})

        # Clean up orphaned users (cannot use raw SQL for complex logic)
        for user in users_to_check:
            remaining_companies = UserCompany.query.filter_by(user_id=user.id).count()
            if remaining_companies == 0 and not user.is_super_admin:
                CommunicationNote.query.filter_by(user_id=user.id).update({'user_id': None})
                db.session.delete(user)

        # 7. Finally delete the company
        db.session.delete(company)
        db.session.commit()

        flash(f'Entreprise "{company_name}" supprimée avec succès.', 'success')
        return redirect(url_for('auth.logout'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur suppression entreprise {company.name}: {str(e)}')
        flash('Une erreur est survenue lors de la suppression. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings'))


# =====================================================
# XERO ACCOUNTING ROUTES
# =====================================================

@company_bp.route('/xero-connect')
@login_required
def xero_connect():
    """Initiate Xero OAuth connection"""
    from xero_connector import XeroConnector

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not current_user.can_access_company_settings():
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    # Vérifier les permissions d'accès aux connexions comptables
    from utils.permissions_helper import check_accounting_access
    permission = check_accounting_access(current_user, company)

    if not permission['allowed']:
        flash(permission['restriction_reason'], 'error')
        return redirect(url_for('company.settings', _anchor='accounting'))

    try:
        xero_connector = XeroConnector()

        # Les credentials sont déjà chargés depuis os.environ dans le constructeur
        if not xero_connector.client_id:
            flash('XERO_CLIENT_ID non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        if not xero_connector.client_secret:
            flash('XERO_CLIENT_SECRET non configuré. Contactez l\'administrateur.', 'error')
            return redirect(url_for('company.settings'))

        import secrets
        state = secrets.token_urlsafe(32)
        auth_url = xero_connector.get_authorization_url(state)

        session['xero_company_id'] = company.id
        session['xero_state'] = state

        return redirect(auth_url)

    except Exception as e:
        current_app.logger.error(f"Error connecting Xero: {e}")
        flash('Une erreur est survenue lors de la connexion Xero. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings'))


@company_bp.route('/xero/callback')
@login_required
def xero_callback():
    """Handle Xero OAuth callback"""
    from app import db
    from models import AccountingConnection
    from xero_connector import XeroConnector
    from datetime import datetime, timedelta

    # Get parameters from callback
    authorization_code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    # Check for errors
    if error:
        flash(f'Erreur Xero: {error}', 'error')
        return redirect(url_for('company.settings'))

    # Verify required parameters
    if not authorization_code:
        flash('Code d\'autorisation manquant dans la réponse Xero.', 'error')
        return redirect(url_for('company.settings'))

    # Verify state parameter
    if state != session.get('xero_state'):
        flash('État de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('company.settings'))

    # Get company from session
    company_id = session.get('xero_company_id')
    if not company_id:
        flash('Session expirée. Veuillez réessayer.', 'error')
        return redirect(url_for('company.settings'))

    # SÉCURITÉ CRITIQUE: Vérifier que l'utilisateur a toujours accès à cette entreprise
    from models import UserCompany
    user_access = UserCompany.query.filter_by(
        user_id=current_user.id,
        company_id=company_id
    ).first()

    if not user_access:
        flash('Accès non autorisé à cette entreprise.', 'error')
        session.pop('xero_state', None)
        session.pop('xero_company_id', None)
        return redirect(url_for('auth.logout'))

    try:
        # Initialize connector and exchange code for tokens
        xero_connector = XeroConnector()
        token_data = xero_connector.exchange_code_for_tokens(authorization_code, state)

        # Check if connection already exists
        existing_connection = AccountingConnection.query.filter_by(
            company_id=company_id,
            system_type='xero'
        ).first()

        if existing_connection:
            # Update existing connection
            existing_connection.access_token = token_data['access_token']
            existing_connection.refresh_token = token_data['refresh_token']
            existing_connection.token_expires_at = datetime.utcnow() + timedelta(seconds=token_data['expires_in'])
            existing_connection.company_id_external = token_data['tenant_id']  # Xero Tenant ID
            existing_connection.is_active = True
            existing_connection.updated_at = datetime.utcnow()  # Force update for sorting

            flash('Connexion Xero mise à jour avec succès!', 'success')
        else:
            # Create new connection
            new_connection = AccountingConnection(
                company_id=company_id,
                system_type='xero',
                system_name='Xero Accounting',
                access_token=token_data['access_token'],
                refresh_token=token_data['refresh_token'],
                token_expires_at=datetime.utcnow() + timedelta(seconds=token_data['expires_in']),
                company_id_external=token_data['tenant_id'],  # Xero Tenant ID
                is_active=True,
                updated_at=datetime.utcnow()  # Force for proper sorting (new connection appears first)
            )
            db.session.add(new_connection)

            flash('Connexion Xero établie avec succès!', 'success')

        db.session.commit()

        # Clean up session
        session.pop('xero_state', None)
        session.pop('xero_company_id', None)

        return redirect(url_for('company.settings', _anchor='accounting'))

    except Exception as e:
        current_app.logger.error(f'Error in Xero callback: {e}')
        flash('Une erreur est survenue lors de la connexion a Xero. Veuillez reessayer.', 'error')
        return redirect(url_for('company.settings'))


@company_bp.route('/xero-disconnect/<int:connection_id>', methods=['POST'])
@login_required
def xero_disconnect(connection_id):
    """Disconnect Xero"""
    from app import db
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id, company_id=company.id).first_or_404()

    # Désactiver la connexion
    connection.is_active = False

    # Supprimer les tokens OAuth pour sécurité
    connection.access_token = None
    connection.refresh_token = None
    connection.token_expires_at = None

    # Réinitialiser les statistiques de sync
    connection.sync_stats = None

    db.session.commit()

    log_action(AuditActions.OAUTH_DISCONNECTED, entity_type=EntityTypes.OAUTH,
              entity_id=connection_id, entity_name='xero',
              details={'company_id': company.id})

    flash('Xero a été déconnecté avec succès.', 'success')
    return redirect(url_for('company.settings'))


@company_bp.route('/xero-sync', methods=['POST'])
@login_required
def xero_sync():
    """Trigger Xero synchronization"""
    from app import db
    from models import AccountingConnection
    from xero_connector import XeroConnector
    import threading
    from datetime import datetime

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='xero',
        is_active=True
    ).first()

    if not connection:
        flash("Aucune connexion Xero active trouvée", "danger")
        return redirect(url_for('company.settings'))

    # Sauvegarder les IDs nécessaires AVANT de créer le thread
    user_id = current_user.id
    company_id = company.id
    connection_id = connection.id

    def run_sync():
        import logging

        from app import app

        with app.app_context():
            from models import SyncLog
            sync_log = None

            try:

                thread_connection = AccountingConnection.query.get(connection_id)

                if not thread_connection:
                    current_app.logger.error(f"❌ SYNC ERROR: Connexion {connection_id} introuvable")
                    return

                if thread_connection.company_id != company_id:
                    current_app.logger.error(
                        f"❌ SÉCURITÉ: Connexion {connection_id} appartient à company {thread_connection.company_id}, "
                        f"mais demandée pour company {company_id}"
                    )
                    return


                # Créer un SyncLog pour tracker cette synchronisation
                sync_log = SyncLog(
                    connection_id=connection_id,
                    sync_type='both',
                    status='running',
                    started_at=datetime.utcnow()
                )
                db.session.add(sync_log)
                db.session.commit()

                # Initialiser le connecteur avec validation de sécurité
                xero_connector = XeroConnector(thread_connection.id, company_id)
                customers_created, customers_updated = xero_connector.sync_customers(company_id, sync_log_id=sync_log.id)

                # Vérifier si la sync a été arrêtée manuellement
                db.session.refresh(sync_log)
                if sync_log.status == 'stopped_manual':
                    return

                invoices_created, invoices_updated = xero_connector.sync_invoices(company_id, sync_log_id=sync_log.id)

                # Vérifier à nouveau si arrêt manuel
                db.session.refresh(sync_log)
                if sync_log.status == 'stopped_manual':
                    from notification_system import send_notification
                    send_notification(
                        user_id=user_id,
                        company_id=company_id,
                        type='warning',
                        title='Synchronisation Xero arrêtée',
                        message=f"La synchronisation a été arrêtée manuellement. Clients: {customers_created} créés, {customers_updated} mis à jour. Factures: {invoices_created} créées, {invoices_updated} mises à jour.",
                        data={
                            'customers_created': customers_created,
                            'customers_updated': customers_updated,
                            'invoices_created': invoices_created,
                            'invoices_updated': invoices_updated,
                        }
                    )
                    return

                payments_created = xero_connector.sync_payments(company_id, sync_log_id=sync_log.id)
                current_app.logger.info(f"Xero payment sync: {payments_created} enregistrements créés")

                # Mettre à jour le SyncLog
                sync_log.status = 'completed'
                sync_log.clients_synced = customers_created + customers_updated
                sync_log.invoices_synced = invoices_created + invoices_updated
                sync_log.completed_at = datetime.utcnow()

                # Mettre à jour la dernière sync
                thread_connection.last_sync_at = datetime.utcnow()

                db.session.commit()

                # Log audit for sync completion
                from models import AuditLog, User, Company
                sync_user = User.query.get(user_id)
                sync_company = Company.query.get(company_id)
                AuditLog.log_with_session(
                    db.session,
                    action=AuditActions.SYNC_COMPLETED,
                    entity_type=EntityTypes.SYNC,
                    entity_name='Xero',
                    details={
                        'sync_type': 'xero',
                        'stats': {
                            'customers_created': customers_created,
                            'customers_updated': customers_updated,
                            'invoices_created': invoices_created,
                            'invoices_updated': invoices_updated
                        }
                    },
                    user=sync_user,
                    company=sync_company
                )

                # Envoyer notification de succès
                from notification_system import send_notification
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='success',
                    title='Synchronisation Xero réussie',
                    message=f"Clients: {customers_created} créés, {customers_updated} mis à jour. Factures: {invoices_created} créées, {invoices_updated} mises à jour.",
                    data={
                        'customers_created': customers_created,
                        'customers_updated': customers_updated,
                        'invoices_created': invoices_created,
                        'invoices_updated': invoices_updated,
                    }
                )

            except Exception as e:
                current_app.logger.error(f"❌ XERO SYNC ERROR: {str(e)}")

                if sync_log:
                    sync_log.status = 'failed'
                    sync_log.error_message = str(e)
                    sync_log.completed_at = datetime.utcnow()
                    db.session.commit()

                from notification_system import send_notification
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='error',
                    title='Erreur de synchronisation Xero',
                    message=f"La synchronisation a échoué: {str(e)}",
                    data={'error': str(e)}
                )

    # Vérifier le quota de synchronisation journalier
    from models import CompanySyncUsage
    if not CompanySyncUsage.check_company_sync_limit(company_id):
        flash('❌ Quota de synchronisations journalier atteint pour votre forfait. Veuillez réessayer demain ou passer à un forfait supérieur.', 'error')
        return redirect(url_for('company.settings') + '#accounting')

    CompanySyncUsage.increment_company_sync_count(company_id)

    # Démarrer le monitoring pour surveiller cette synchronisation
    from sync_monitor import ensure_monitoring_started
    ensure_monitoring_started()

    # Démarrer le thread de synchronisation
    sync_thread = threading.Thread(target=run_sync, daemon=True)
    sync_thread.start()

    flash("Synchronisation Xero lancée en arrière-plan. Vous serez notifié à la fin.", "success")
    return redirect(url_for('company.settings', _anchor='accounting'))


# ============================================
# ODOO CONNECTOR ROUTES
# ============================================

@company_bp.route('/odoo-setup', methods=['GET', 'POST'])
@login_required
def odoo_setup():
    """Odoo configuration page"""
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('company.settings'))

    # Get existing connection if any
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='odoo'
    ).first()

    return render_template(
        'company/odoo_config.html',
        company=company,
        connection=connection
    )


@company_bp.route('/odoo-test', methods=['POST'])
@login_required
def odoo_test():
    """Test Odoo connection"""
    from odoo_connector import OdooConnector
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'success': False, 'error': 'Aucune entreprise sélectionnée'}), 400

    # Get form data
    odoo_url = request.form.get('odoo_url', '').strip()
    odoo_database = request.form.get('odoo_database', '').strip()
    odoo_username = request.form.get('odoo_username', '').strip()
    odoo_api_key = request.form.get('odoo_api_key', '').strip()
    is_sandbox = request.form.get('is_sandbox') == 'true'

    if not all([odoo_url, odoo_database, odoo_username, odoo_api_key]):
        return jsonify({'success': False, 'error': 'Tous les champs sont requis'}), 400

    # Create temporary connection for testing
    temp_connection = AccountingConnection(
        company_id=company.id,
        system_type='odoo',
        system_name='Odoo',
        odoo_url=odoo_url,
        odoo_database=odoo_database,
        company_id_external=odoo_username,  # Store username here
        is_sandbox=is_sandbox
    )
    temp_connection.access_token = odoo_api_key  # This will be encrypted

    # Test connection
    connector = OdooConnector()
    connector.connection = temp_connection

    try:
        success, message = connector.test_connection()
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        current_app.logger.error(f"Error testing Odoo connection: {e}")
        return jsonify({'success': False, 'error': 'Une erreur interne est survenue'}), 500


@company_bp.route('/odoo-save', methods=['POST'])
@login_required
def odoo_save():
    """Save Odoo configuration"""
    from app import db
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('company.settings'))

    # Get form data
    odoo_url = request.form.get('odoo_url', '').strip()
    odoo_database = request.form.get('odoo_database', '').strip()
    odoo_username = request.form.get('odoo_username', '').strip()
    odoo_api_key = request.form.get('odoo_api_key', '').strip()
    is_sandbox = request.form.get('is_sandbox') == 'on'

    if not all([odoo_url, odoo_database, odoo_username, odoo_api_key]):
        flash('Tous les champs sont requis.', 'error')
        return redirect(url_for('company.odoo_setup'))

    # Get or create connection
    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='odoo'
    ).first()

    if connection:
        # Update existing connection
        connection.odoo_url = odoo_url
        connection.odoo_database = odoo_database
        connection.company_id_external = odoo_username
        connection.is_sandbox = is_sandbox
        connection.access_token = odoo_api_key  # Will be encrypted
        connection.is_active = True
        connection.system_name = f"Odoo - {odoo_database}"
    else:
        # Create new connection
        connection = AccountingConnection(
            company_id=company.id,
            system_type='odoo',
            system_name=f"Odoo - {odoo_database}",
            odoo_url=odoo_url,
            odoo_database=odoo_database,
            company_id_external=odoo_username,
            is_sandbox=is_sandbox,
            is_active=True
        )
        connection.access_token = odoo_api_key  # Will be encrypted
        db.session.add(connection)

    try:
        db.session.commit()
        flash('Configuration Odoo sauvegardée avec succès !', 'success')
        return redirect(url_for('company.odoo_mapping', connection_id=connection.id))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving Odoo config: {e}")
        flash('Une erreur est survenue lors de la sauvegarde. Veuillez reessayer.', 'error')
        return redirect(url_for('company.odoo_setup'))


@company_bp.route('/odoo-disconnect/<int:connection_id>', methods=['GET', 'POST'])
@login_required
def odoo_disconnect(connection_id):
    """Disconnect Odoo"""
    from app import db
    from models import AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id, company_id=company.id).first_or_404()

    # Disable connection
    connection.is_active = False

    # Clear API key for security
    connection.access_token = None

    # Reset sync stats
    connection.sync_stats = None

    db.session.commit()

    flash('Odoo a été déconnecté avec succès.', 'success')
    return redirect(url_for('company.settings'))


@company_bp.route('/odoo-sync', methods=['POST'])
@login_required
def odoo_sync():
    """Trigger Odoo synchronization"""
    from app import db
    from models import AccountingConnection
    from odoo_connector import OdooConnector
    import threading
    from datetime import datetime

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        system_type='odoo',
        is_active=True
    ).first()

    if not connection:
        flash("Aucune connexion Odoo active trouvée", "danger")
        return redirect(url_for('company.settings'))

    # Save IDs before creating thread
    user_id = current_user.id
    company_id = company.id
    connection_id = connection.id

    def run_sync():
        import logging

        from app import app

        with app.app_context():
            from models import SyncLog
            sync_log = None

            try:

                thread_connection = AccountingConnection.query.get(connection_id)

                if not thread_connection:
                    current_app.logger.error(f"❌ SYNC ERROR: Connection {connection_id} not found")
                    return

                if thread_connection.company_id != company_id:
                    current_app.logger.error(
                        f"❌ SECURITY: Connection {connection_id} belongs to company {thread_connection.company_id}, "
                        f"but requested for company {company_id}"
                    )
                    return


                # Create SyncLog to track this synchronization
                sync_log = SyncLog(
                    connection_id=connection_id,
                    sync_type='both',
                    status='running',
                    started_at=datetime.utcnow()
                )
                db.session.add(sync_log)
                db.session.commit()

                # Initialize connector with security validation
                odoo_connector = OdooConnector(thread_connection.id, company_id)
                customers_created, customers_updated = odoo_connector.sync_customers(company_id, sync_log_id=sync_log.id)

                # Check if sync was manually stopped
                db.session.refresh(sync_log)
                if sync_log.status == 'stopped':
                    return

                # Sync payments BEFORE invoices (invoices must still be in DB for matching)
                payments_created = 0
                try:
                    payments_created = odoo_connector.sync_payments(company_id)
                    current_app.logger.info(f"Odoo payment sync: {payments_created} records created")
                except Exception as payment_sync_error:
                    current_app.logger.warning(
                        f"Odoo payment sync failed (non-blocking): {payment_sync_error}"
                    )

                invoices_created, invoices_updated = odoo_connector.sync_invoices(company_id, sync_log_id=sync_log.id)

                # Check again if manually stopped
                db.session.refresh(sync_log)
                if sync_log.status == 'stopped':
                    from notification_system import send_notification
                    send_notification(
                        user_id=user_id,
                        company_id=company_id,
                        type='warning',
                        title='Synchronisation Odoo arrêtée',
                        message=f"La synchronisation a été arrêtée manuellement. Clients: {customers_created} créés, {customers_updated} mis à jour. Factures: {invoices_created} créées, {invoices_updated} mises à jour.",
                        data={
                            'customers_created': customers_created,
                            'customers_updated': customers_updated,
                            'invoices_created': invoices_created,
                            'invoices_updated': invoices_updated,
                        }
                    )
                    return

                # Update SyncLog
                sync_log.status = 'completed'
                sync_log.clients_synced = customers_created + customers_updated
                sync_log.invoices_synced = invoices_created + invoices_updated
                sync_log.completed_at = datetime.utcnow()

                # Update last sync time
                thread_connection.last_sync_at = datetime.utcnow()

                db.session.commit()

                # Log audit for sync completion
                from models import AuditLog, User, Company
                sync_user = User.query.get(user_id)
                sync_company = Company.query.get(company_id)
                AuditLog.log_with_session(
                    db.session,
                    action=AuditActions.SYNC_COMPLETED,
                    entity_type=EntityTypes.SYNC,
                    entity_name='Odoo',
                    details={
                        'sync_type': 'odoo',
                        'stats': {
                            'customers_created': customers_created,
                            'customers_updated': customers_updated,
                            'invoices_created': invoices_created,
                            'invoices_updated': invoices_updated
                        }
                    },
                    user=sync_user,
                    company=sync_company
                )

                # Send success notification
                from notification_system import send_notification
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='success',
                    title='Synchronisation Odoo réussie',
                    message=f"Clients: {customers_created} créés, {customers_updated} mis à jour. Factures: {invoices_created} créées, {invoices_updated} mises à jour.",
                    data={
                        'customers_created': customers_created,
                        'customers_updated': customers_updated,
                        'invoices_created': invoices_created,
                        'invoices_updated': invoices_updated,
                    }
                )

            except Exception as e:
                current_app.logger.error(f"❌ ODOO SYNC ERROR: {str(e)}")

                if sync_log:
                    sync_log.status = 'failed'
                    sync_log.error_message = str(e)
                    sync_log.completed_at = datetime.utcnow()
                    db.session.commit()

                from notification_system import send_notification
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='error',
                    title='Erreur de synchronisation Odoo',
                    message=f"La synchronisation a échoué: {str(e)}",
                    data={'error': str(e)}
                )

    # Vérifier le quota de synchronisation journalier
    from models import CompanySyncUsage
    if not CompanySyncUsage.check_company_sync_limit(company_id):
        flash('❌ Quota de synchronisations journalier atteint pour votre forfait. Veuillez réessayer demain ou passer à un forfait supérieur.', 'error')
        return redirect(url_for('company.settings') + '#accounting')

    CompanySyncUsage.increment_company_sync_count(company_id)

    # Démarrer le monitoring pour surveiller cette synchronisation
    from sync_monitor import ensure_monitoring_started
    ensure_monitoring_started()

    # Start sync thread
    sync_thread = threading.Thread(target=run_sync, daemon=True)
    sync_thread.start()

    flash("Synchronisation Odoo lancée en arrière-plan. Vous serez notifié à la fin.", "success")
    return redirect(url_for('company.settings', _anchor='accounting'))


@company_bp.route('/odoo-mapping/<int:connection_id>', methods=['GET'])
@login_required
def odoo_mapping(connection_id):
    """Odoo field mapping configuration page"""
    from models import AccountingConnection
    from odoo_connector import OdooConnector

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id,
        company_id=company.id,
        system_type='odoo'
    ).first_or_404()

    # Get sample data from Odoo
    connector = OdooConnector(connection.id, company.id)

    # Get customer fields
    customer_fields = connector.get_table_headers('res.partner', sample_size=5)

    # Get invoice fields
    invoice_fields = connector.get_table_headers('account.move', sample_size=5)

    # Get current field mapping
    current_mapping = connection.get_field_mapping()

    return render_template(
        'company/odoo_mapping.html',
        company=company,
        connection=connection,
        customer_fields=customer_fields,
        invoice_fields=invoice_fields,
        current_mapping=current_mapping
    )


@company_bp.route('/odoo-mapping-save/<int:connection_id>', methods=['POST'])
@login_required
def odoo_mapping_save(connection_id):
    """Save Odoo field mapping"""
    from app import db
    from models import AccountingConnection
    import json

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    connection = AccountingConnection.query.filter_by(
        id=connection_id,
        company_id=company.id,
        system_type='odoo'
    ).first_or_404()

    # Get mapping data from form
    mapping_data = request.get_json() if request.is_json else request.form.to_dict()

    # Save mapping
    connection.set_field_mapping(mapping_data)
    db.session.commit()

    if request.is_json:
        return jsonify({'success': True, 'message': 'Mapping sauvegardé avec succès'})
    else:
        flash('Mapping des champs Odoo sauvegardé avec succès !', 'success')
        return redirect(url_for('company.settings', _anchor='accounting'))


# ============================================
# EXCEL/CSV FILE IMPORT CONNECTOR ROUTES
# ============================================

@company_bp.route('/file-import-config', methods=['GET', 'POST'])
@login_required
def file_import_config():
    """Configuration page for Excel/CSV file import connector"""
    from models import Company, FileImportMapping

    company_id = current_user.company_id
    company = Company.query.get_or_404(company_id)

    # Vérification des permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Vous n\'avez pas les permissions pour configurer les imports.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get or create mapping configuration
    mapping_config = FileImportMapping.get_or_create_for_company(company_id)

    # Check if project feature is enabled
    from utils.project_helper import is_project_feature_enabled, get_project_label
    is_project_enabled = is_project_feature_enabled(company)
    project_label = get_project_label(company) if is_project_enabled else None

    if request.method == 'POST':
        # This handles the final mapping save (redirected from file-import-save-mapping)
        pass

    return render_template(
        'company/file_import_config.html',
        company=company,
        mapping_config=mapping_config,
        is_project_enabled=is_project_enabled,
        project_label=project_label
    )


@company_bp.route('/file-import-detect-headers', methods=['POST'])
@login_required
def file_import_detect_headers():
    """Detect column headers from uploaded Excel/CSV file"""
    from file_import_connector import detect_file_type, detect_headers_from_file

    # Vérification des permissions
    company_id = current_user.company_id
    user_role = current_user.get_role_in_company(company_id)
    if user_role not in ['super_admin', 'admin']:
        return jsonify({'success': False, 'error': 'Accès refusé'}), 403

    try:
        if 'sample_file' not in request.files:
            return jsonify({'success': False, 'error': 'Aucun fichier fourni'}), 400

        file = request.files['sample_file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Nom de fichier vide'}), 400

        # Detect file type
        file_type = detect_file_type(file.filename)
        if file_type == 'unknown':
            return jsonify({
                'success': False,
                'error': 'Type de fichier non supporté. Utilisez .xlsx ou .csv'
            }), 400

        # Read file content
        file_content = file.read()

        # Detect headers
        headers, error = detect_headers_from_file(file_content, file_type)

        if error:
            return jsonify({'success': False, 'error': error}), 400

        return jsonify({
            'success': True,
            'headers': headers,
            'file_type': file_type,
            'filename': file.filename
        })

    except Exception as e:
        current_app.logger.error(f"Error detecting headers: {e}")
        return jsonify({'success': False, 'error': 'Une erreur interne est survenue'}), 500


@company_bp.route('/file-import-save-mapping', methods=['POST'])
@login_required
def file_import_save_mapping():
    """Save column mapping configuration"""
    from app import db
    from models import FileImportMapping
    from file_import_connector import validate_mapping, get_required_fields

    try:
        company_id = current_user.company_id

        # Vérification des permissions
        user_role = current_user.get_role_in_company(company_id)
        if user_role not in ['super_admin', 'admin']:
            return jsonify({'success': False, 'error': 'Accès refusé'}), 403
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'Données manquantes'}), 400

        mapping_type = data.get('mapping_type')  # 'clients' or 'invoices'
        column_mapping = data.get('mapping', {})  # {"file_col": "standard_field"}
        language_mappings = data.get('language_mappings', {})  # {"FR": "Français", "EN": "Anglais"}

        if mapping_type not in ['clients', 'invoices']:
            return jsonify({'success': False, 'error': 'Type de mapping invalide'}), 400

        # Validate mapping
        is_valid, errors = validate_mapping(column_mapping, mapping_type)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': 'Mapping incomplet',
                'details': errors
            }), 400

        # Get or create mapping configuration
        mapping_config = FileImportMapping.get_or_create_for_company(company_id)

        # Update mapping
        if mapping_type == 'clients':
            mapping_config.client_column_mappings = column_mapping
            # Save language mappings only for client imports
            if language_mappings:
                mapping_config.language_value_mappings = language_mappings
        else:
            mapping_config.invoice_column_mappings = column_mapping

        # Mark as configured if at least one mapping is done (clients OR invoices)
        if mapping_config.client_column_mappings or mapping_config.invoice_column_mappings:
            mapping_config.is_configured = True

        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'Configuration {mapping_type} sauvegardée avec succès',
            'is_fully_configured': mapping_config.is_configured
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving mapping: {e}")
        return jsonify({'success': False, 'error': 'Une erreur interne est survenue'}), 500


@company_bp.route('/file-import-clients/start', methods=['POST'])
@login_required
def file_import_clients_start():
    """Start client import and return session_id for SSE tracking"""
    from models import Company, FileImportMapping
    from import_progress import progress_manager
    import tempfile
    import os

    try:
        company_id = current_user.company_id
        company = Company.query.get_or_404(company_id)

        # Vérification des permissions
        user_role = current_user.get_role_in_company(company.id)
        if user_role not in ['super_admin', 'admin']:
            return jsonify({'success': False, 'error': 'Accès refusé'}), 403

        # Get mapping configuration
        mapping_config = FileImportMapping.query.filter_by(company_id=company_id).first()
        if not mapping_config or not mapping_config.client_column_mappings:
            return jsonify({'success': False, 'error': 'Configuration du mapping clients manquante'}), 400

        # Check file upload
        if 'import_file' not in request.files:
            return jsonify({'success': False, 'error': 'Aucun fichier fourni'}), 400

        file = request.files['import_file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Nom de fichier vide'}), 400

        # Save file temporarily
        temp_fd, temp_path = tempfile.mkstemp(suffix=os.path.splitext(file.filename)[1])
        try:
            file.save(temp_path)
        finally:
            os.close(temp_fd)

        # Create progress session
        session_id = progress_manager.create_session(0)

        # Capture the concrete Flask app object for the background thread
        app = current_app._get_current_object()

        # Store import job data in session (for background processing)
        import threading
        user_id = current_user.id
        def process_import():
            try:
                with app.app_context():
                    _file_import_process(temp_path, file.filename, company_id, mapping_config, session_id, user_id)
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        # Start import in background thread
        thread = threading.Thread(target=process_import)
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'session_id': session_id})

    except Exception as e:
        current_app.logger.error(f"Error starting import: {e}")
        return jsonify({'success': False, 'error': 'Une erreur interne est survenue'}), 500


def _file_import_process(file_path, filename, company_id, mapping_config, session_id, user_id=None):
    """Internal function to process file import with progress updates"""
    from file_import_connector import detect_file_type, transform_file_to_standard_format
    from import_progress import progress_manager
    from models import Client, ImportJob
    from app import db
    from datetime import datetime
    import io

    import_job = None

    try:
        import time
        from flask import current_app as app
        start_time = time.time()

        # Étape 1: Lecture (0-20%)
        progress_manager.update_progress(session_id, 0, 'Lecture du fichier', 'Lecture en cours...')

        # Read file
        with open(file_path, 'rb') as f:
            file_content = f.read()

        # Create ImportJob for logging
        import_job = ImportJob(
            company_id=company_id,
            user_id=user_id,
            import_type='clients',
            import_mode='append',
            filename=filename,
            file_size=len(file_content),
            status='processing'
        )
        import_job.started_at = datetime.utcnow()
        db.session.add(import_job)
        db.session.commit()

        file_type = detect_file_type(filename)
        app.logger.info(f"⏱️ Lecture fichier: {time.time() - start_time:.2f}s")

        # Update session to use percentage-based progress (0-100)
        progress_manager.set_total_rows(session_id, 100)

        # Étape 2: Transformation (20-40%)
        progress_manager.update_progress(session_id, 20, 'Transformation', 'Analyse du fichier...')
        transform_start = time.time()

        # Transform file
        transformed_rows, total_rows, errors = transform_file_to_standard_format(
            file_content,
            file_type,
            mapping_config.client_column_mappings,
            'clients',
            mapping_config.get_language_mappings(),
            include_project_field=False  # Not applicable for clients
        )
        app.logger.info(f"⏱️ Transformation: {time.time() - transform_start:.2f}s pour {total_rows} lignes")

        progress_manager.update_progress(session_id, 40, 'Préparation', f'{total_rows} lignes à importer')

        if errors and not transformed_rows:
            progress_manager.complete_session(session_id, success=False, error_message='; '.join(errors))
            return

        # Étape 3: Préparation import (40-50%)
        progress_manager.update_progress(session_id, 45, 'Préparation', 'Chargement des données existantes...')
        cache_start = time.time()

        success_count = 0
        # NOTE: transformed_rows contient déjà uniquement les données (sans en-tête)
        data_rows = transformed_rows

        # Load existing clients cache
        all_existing_clients = Client.query.filter_by(company_id=company_id).all()
        clients_cache = {client.code_client: client for client in all_existing_clients}
        app.logger.info(f"⏱️ Chargement cache ({len(clients_cache)} clients): {time.time() - cache_start:.2f}s")

        # Étape 4: Analyse des données (50-60%)
        progress_manager.update_progress(session_id, 50, 'Analyse', 'Validation des données...')
        analysis_start = time.time()

        # Collect client data
        clients_to_create = []
        new_clients_to_insert = []
        clients_to_update = []

        for row_num, row in enumerate(data_rows, start=2):
            if len(row) < 9:
                continue

            code_client = str(row[0]).strip()
            if not code_client:
                continue

            # Validate and sanitize language code (row[8], not row[7])
            language_value = str(row[8]).strip().lower() if len(row) > 8 and row[8] else 'fr'
            # Only accept valid language codes (2-5 chars, starting with letter)
            if not language_value or len(language_value) > 5 or not language_value[0].isalpha():
                language_value = 'fr'
            # Ensure it's a valid language code (fr, en, es, etc.)
            if language_value not in ['fr', 'en', 'es', 'de', 'it', 'pt', 'nl', 'ar']:
                language_value = 'fr'

            client_data = {
                'code_client': code_client,
                'name': str(row[1]).strip() if row[1] else '',
                'email': str(row[2]).strip() if row[2] else None,
                'phone': str(row[3]).strip() if row[3] else None,
                'address': str(row[4]).strip() if row[4] else None,
                'representative_name': str(row[5]).strip() if row[5] else None,
                'payment_terms': str(row[6]).strip() if row[6] else None,
                'language': language_value,
                'row_num': row_num,
                'existing_client': clients_cache.get(code_client)
            }

            if client_data['existing_client']:
                clients_to_update.append(client_data)
            else:
                new_clients_to_insert.append(client_data)

        app.logger.info(f"⏱️ Analyse lignes: {time.time() - analysis_start:.2f}s - {len(new_clients_to_insert)} nouveaux, {len(clients_to_update)} à MAJ")

        # Étape 5: Import des nouveaux clients (60-80%)
        progress_manager.update_progress(session_id, 60, 'Import', f'Création de {len(new_clients_to_insert)} nouveaux clients...')
        insert_start = time.time()

        # Bulk insert new clients
        if new_clients_to_insert:
            clients_bulk_data = []
            for client_data in new_clients_to_insert:
                clients_bulk_data.append({
                    'code_client': client_data['code_client'],
                    'name': client_data['name'],
                    'email': client_data['email'],
                    'phone': client_data['phone'],
                    'address': client_data['address'],
                    'representative_name': client_data['representative_name'],
                    'payment_terms': client_data['payment_terms'],
                    'language': client_data['language'],
                    'company_id': company_id,
                    'created_at': datetime.utcnow(),
                    'updated_at': datetime.utcnow()
                })

            db.session.execute(Client.__table__.insert(), clients_bulk_data)
            db.session.flush()
            success_count += len(new_clients_to_insert)
            app.logger.info(f"⏱️ Bulk insert: {time.time() - insert_start:.2f}s pour {len(new_clients_to_insert)} clients")

            progress_manager.update_progress(session_id, 75, 'Import', f'{success_count} clients créés')

        # Étape 6: Mise à jour des clients existants (80-85%) - OPTIMIZED BULK UPDATE
        if clients_to_update:
            progress_manager.update_progress(session_id, 80, 'Mise à jour', f'Mise à jour de {len(clients_to_update)} clients...')
            update_start = time.time()

            # Optimization: Update in batches using SQL UPDATE with CASE WHEN
            # This is MUCH faster than bulk_update_mappings for large datasets
            batch_size = 1000
            for i in range(0, len(clients_to_update), batch_size):
                batch = clients_to_update[i:i+batch_size]

                # Build lists for CASE WHEN clauses
                ids = []
                names_map = {}
                emails_map = {}
                phones_map = {}
                addresses_map = {}
                reps_map = {}
                terms_map = {}
                langs_map = {}

                for client_data in batch:
                    client_id = client_data['existing_client'].id
                    ids.append(client_id)
                    names_map[client_id] = client_data['name']
                    emails_map[client_id] = client_data['email']
                    phones_map[client_id] = client_data['phone']
                    addresses_map[client_id] = client_data['address']
                    reps_map[client_id] = client_data['representative_name']
                    terms_map[client_id] = client_data['payment_terms']
                    langs_map[client_id] = client_data['language']

                # Build CASE WHEN clauses for each column
                from sqlalchemy import case, literal_column

                name_cases = case(
                    *[(Client.id == cid, names_map[cid]) for cid in ids],
                    else_=Client.name
                )
                email_cases = case(
                    *[(Client.id == cid, emails_map[cid]) for cid in ids],
                    else_=Client.email
                )
                phone_cases = case(
                    *[(Client.id == cid, phones_map[cid]) for cid in ids],
                    else_=Client.phone
                )
                address_cases = case(
                    *[(Client.id == cid, addresses_map[cid]) for cid in ids],
                    else_=Client.address
                )
                rep_cases = case(
                    *[(Client.id == cid, reps_map[cid]) for cid in ids],
                    else_=Client.representative_name
                )
                terms_cases = case(
                    *[(Client.id == cid, terms_map[cid]) for cid in ids],
                    else_=Client.payment_terms
                )
                lang_cases = case(
                    *[(Client.id == cid, langs_map[cid]) for cid in ids],
                    else_=Client.language
                )

                # Execute single UPDATE for entire batch
                db.session.query(Client).filter(Client.id.in_(ids)).update(
                    {
                        'name': name_cases,
                        'email': email_cases,
                        'phone': phone_cases,
                        'address': address_cases,
                        'representative_name': rep_cases,
                        'payment_terms': terms_cases,
                        'language': lang_cases,
                        'updated_at': datetime.utcnow()
                    },
                    synchronize_session=False
                )

            success_count += len(clients_to_update)
            app.logger.info(f"⏱️ Bulk update optimisé: {time.time() - update_start:.2f}s pour {len(clients_to_update)} clients")
            progress_manager.update_progress(session_id, 85, 'Mise à jour', f'{len(clients_to_update)} clients mis à jour')

        # Étape 7: Finalisation (85-95%)
        progress_manager.update_progress(session_id, 90, 'Finalisation', 'Enregistrement en base de données...')
        commit_start = time.time()

        db.session.commit()
        app.logger.info(f"⏱️ Commit DB: {time.time() - commit_start:.2f}s")

        # Étape 8: Terminé (95-100%)
        progress_manager.update_progress(session_id, 95, 'Terminé', f'{success_count} clients traités')
        app.logger.info(f"⏱️ TOTAL import: {time.time() - start_time:.2f}s pour {success_count} clients")

        # Update ImportJob with success
        if import_job:
            import_job.status = 'completed'
            import_job.completed_at = datetime.utcnow()
            import_job.total_rows = total_rows
            import_job.processed_rows = success_count
            import_job.success_count = success_count
            import_job.result_message = f'{success_count} clients importés/mis à jour'
            db.session.commit()

        progress_manager.complete_session(session_id, success=True)

    except Exception as e:
        db.session.rollback()
        # Update ImportJob with failure
        if import_job:
            try:
                import_job.status = 'failed'
                import_job.completed_at = datetime.utcnow()
                import_job.result_message = str(e)
                db.session.commit()
            except Exception:
                pass
        progress_manager.complete_session(session_id, success=False, error_message=str(e))


@company_bp.route('/file-import-clients', methods=['POST'])
@login_required
def file_import_clients():
    """Import clients from Excel/CSV file using saved mapping (non-AJAX fallback)"""
    from models import Company, FileImportMapping, ImportJob
    from file_import_connector import detect_file_type, transform_file_to_standard_format
    from import_progress import progress_manager
    from app import db
    from datetime import datetime
    import io
    from defusedcsv import csv

    # Create progress session ID (will be used by frontend to connect to SSE)
    progress_session_id = None
    import_job = None

    try:
        company_id = current_user.company_id
        company = Company.query.get_or_404(company_id)

        # Vérification des permissions
        user_role = current_user.get_role_in_company(company.id)
        if user_role not in ['super_admin', 'admin']:
            flash('Accès refusé. Vous n\'avez pas les permissions pour importer des clients.', 'error')
            return redirect(url_for('main.dashboard'))

        # Get mapping configuration
        mapping_config = FileImportMapping.query.filter_by(company_id=company_id).first()
        if not mapping_config or not mapping_config.client_column_mappings:
            flash("Configuration du mapping clients manquante. Configurez d'abord le mapping.", "error")
            return redirect(url_for('company.file_import_config'))

        # Check file upload
        if 'import_file' not in request.files:
            flash("Aucun fichier fourni", "error")
            return redirect(url_for('company.file_import_config'))

        file = request.files['import_file']
        if file.filename == '':
            flash("Nom de fichier vide", "error")
            return redirect(url_for('company.file_import_config'))

        # Detect file type
        file_type = detect_file_type(file.filename)
        if file_type == 'unknown':
            flash("Type de fichier non supporté. Utilisez .xlsx ou .csv", "error")
            return redirect(url_for('company.file_import_config'))

        # Create progress session (start with unknown total, will update after transform)
        progress_session_id = progress_manager.create_session(0)
        progress_manager.update_progress(progress_session_id, 0, 'Lecture du fichier', 'Lecture en cours...')

        # Read and transform file
        file_content = file.read()

        # Create ImportJob for logging
        import_job = ImportJob(
            company_id=company_id,
            user_id=current_user.id,
            import_type='clients',
            import_mode='append',
            filename=file.filename,
            file_size=len(file_content),
            status='processing'
        )
        import_job.started_at = datetime.utcnow()
        db.session.add(import_job)
        db.session.commit()

        transformed_rows, total_rows, errors = transform_file_to_standard_format(
            file_content,
            file_type,
            mapping_config.client_column_mappings,
            'clients',
            mapping_config.get_language_mappings(),
            include_project_field=False  # Not applicable for clients
        )

        # Update job with total rows
        import_job.total_rows = total_rows
        db.session.commit()

        # Update session with actual total (thread-safe)
        progress_manager.set_total_rows(progress_session_id, total_rows)
        progress_manager.update_progress(progress_session_id, total_rows, 'Transformation complétée', f'{total_rows} lignes transformées')

        if errors and not transformed_rows:
            import_job.mark_as_failed('; '.join(errors))
            db.session.commit()
            progress_manager.complete_session(progress_session_id, success=False, error_message='; '.join(errors))
            flash(f"Erreur lors de la transformation : {'; '.join(errors)}", "error")
            return redirect(url_for('company.file_import_config'))

        # Process import using 3-pass logic (same as import_views.py)
        from models import Client

        success_count = 0
        error_count = 0
        errors_list = []

        # NOTE: transformed_rows contient déjà uniquement les données (sans en-tête)
        # car transform_file_to_standard_format commence à min_row=2 pour Excel
        data_rows = transformed_rows

        # OPTIMIZATION: Load ALL existing clients ONCE to avoid N queries
        current_app.logger.info('Chargement des clients existants en cache...')
        all_existing_clients = Client.query.filter_by(company_id=company_id).all()

        # Build in-memory cache: {code_client: Client object}
        clients_cache = {}
        for client in all_existing_clients:
            clients_cache[client.code_client] = client

        # First pass: Collect client data
        clients_to_create = []
        parent_relationships = []

        # row_num commence à 2 car ligne 1 est l'en-tête dans le fichier original
        for row_num, row in enumerate(data_rows, start=2):
            try:
                if len(row) < 9:
                    errors_list.append(f'Ligne {row_num}: Données insuffisantes')
                    error_count += 1
                    continue

                code_client = row[0].strip()
                name = row[1].strip()
                email = row[2].strip() if row[2].strip() else None
                phone = row[3].strip() if row[3].strip() else None
                address = row[4].strip() if row[4].strip() else None
                representative_name = row[5].strip() if row[5].strip() else None
                payment_terms = row[6].strip() if row[6].strip() else None
                parent_code = row[7].strip() if row[7].strip() else None

                # Validate and sanitize language code
                language = row[8].strip().lower() if row[8].strip() else 'fr'
                # Only accept valid language codes (2-5 chars, starting with letter)
                if not language or len(language) > 5 or not language[0].isalpha():
                    language = 'fr'
                # Ensure it's a valid language code (fr, en, es, etc.)
                if language not in ['fr', 'en', 'es', 'de', 'it', 'pt', 'nl', 'ar']:
                    language = 'fr'

                if not name or not code_client:
                    errors_list.append(f'Ligne {row_num}: Code client et nom requis')
                    error_count += 1
                    continue

                # Check if client already exists - FAST CACHE LOOKUP (no SQL query)
                existing_client = clients_cache.get(code_client)

                client_data = {
                    'row_num': row_num,
                    'code_client': code_client,
                    'name': name,
                    'email': email,
                    'phone': phone,
                    'address': address,
                    'representative_name': representative_name,
                    'payment_terms': payment_terms,
                    'parent_code': parent_code,
                    'language': language,
                    'existing_client': existing_client
                }
                clients_to_create.append(client_data)

                if parent_code:
                    parent_relationships.append({
                        'child_code': code_client,
                        'parent_code': parent_code,
                        'row_num': row_num
                    })

            except Exception as e:
                errors_list.append(f'Ligne {row_num}: {str(e)}')
                error_count += 1

        # Check license capacity
        new_clients_count = sum(1 for c in clients_to_create if not c['existing_client'])
        if new_clients_count > 0:
            try:
                company.assert_client_capacity(new_clients_count)
            except ValueError as e:
                flash(str(e), 'error')
                return redirect(url_for('company.settings', _anchor='accounting'))

        # Second pass: Create/update clients
        # OPTIMIZED: Use bulk insert for new clients, individual update for existing
        created_clients = {}

        # Separate new clients from updates
        new_clients_to_insert = []
        clients_to_update = []

        for client_data in clients_to_create:
            if client_data['existing_client']:
                clients_to_update.append(client_data)
            else:
                new_clients_to_insert.append(client_data)

        # BULK INSERT: Create all new clients at once (FAST)
        if new_clients_to_insert:
            try:
                from datetime import datetime
                current_app.logger.info(f'Bulk insert de {len(new_clients_to_insert)} nouveaux clients...')

                # Prepare bulk insert data
                clients_bulk_data = []
                for client_data in new_clients_to_insert:
                    clients_bulk_data.append({
                        'code_client': client_data['code_client'],
                        'name': client_data['name'],
                        'email': client_data['email'],
                        'phone': client_data['phone'],
                        'address': client_data['address'],
                        'representative_name': client_data['representative_name'],
                        'payment_terms': client_data['payment_terms'],
                        'language': client_data['language'],
                        'company_id': company_id,
                        'created_at': datetime.utcnow(),
                        'updated_at': datetime.utcnow()
                    })

                # Execute bulk insert
                db.session.execute(Client.__table__.insert(), clients_bulk_data)
                db.session.flush()  # Get IDs immediately

                # Retrieve ALL newly created clients in ONE query (not N queries!)
                new_client_codes = [c['code_client'] for c in new_clients_to_insert]
                newly_created = Client.query.filter(
                    Client.code_client.in_(new_client_codes),
                    Client.company_id == company_id
                ).all()

                # Map to dictionary
                for client in newly_created:
                    created_clients[client.code_client] = client
                success_count += len(newly_created)


            except Exception as e:
                # CRITICAL: Rollback session immediately to keep it usable
                db.session.rollback()
                current_app.logger.error(f'Erreur bulk insert clients: {e}')
                flash('Erreur critique lors de la creation en masse des clients. Veuillez reessayer.', 'error')
                return redirect(url_for('company.file_import_config'))

        # BULK UPDATE: Update all existing clients at once - FAST!
        if clients_to_update:
            try:
                current_app.logger.info(f'Bulk update de {len(clients_to_update)} clients existants...')

                # Prepare bulk update data
                updates_bulk_data = []
                for client_data in clients_to_update:
                    existing_client = client_data['existing_client']
                    updates_bulk_data.append({
                        'id': existing_client.id,
                        'name': client_data['name'],
                        'email': client_data['email'],
                        'phone': client_data['phone'],
                        'address': client_data['address'],
                        'representative_name': client_data['representative_name'],
                        'payment_terms': client_data['payment_terms'],
                        'language': client_data['language'],
                        'updated_at': datetime.utcnow()
                    })
                    # Track in created_clients dict for parent relationships
                    created_clients[client_data['code_client']] = existing_client

                # Execute bulk update - FAST! (preserves contacts, collector_id, created_at)
                db.session.bulk_update_mappings(Client, updates_bulk_data)
                success_count += len(clients_to_update)


            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f'Erreur bulk update clients: {str(e)}')
                errors_list.append(f'Erreur bulk update: {str(e)}')
                error_count += len(clients_to_update)

        # Third pass: Set up parent relationships
        parent_errors = []
        if success_count > 0:
            db.session.flush()

            for rel in parent_relationships:
                try:
                    child_client = created_clients.get(rel['child_code'])
                    if not child_client:
                        continue

                    parent_client = Client.query.filter_by(
                        code_client=rel['parent_code'],
                        company_id=company_id
                    ).first()

                    if not parent_client:
                        parent_errors.append(f'Ligne {rel["row_num"]}: Parent "{rel["parent_code"]}" non trouvé')
                        continue

                    child_client.parent_client_id = parent_client.id

                except Exception as e:
                    parent_errors.append(f'Ligne {rel["row_num"]}: {str(e)}')

        # Update progress before commit
        progress_manager.update_progress(progress_session_id, total_rows, 'Sauvegarde en cours', 'Enregistrement dans la base de données...')

        # Commit all changes
        try:
            db.session.commit()

            # Update ImportJob with final results
            if import_job:
                import_job.status = 'completed'
                import_job.completed_at = datetime.utcnow()
                import_job.success_count = success_count
                import_job.error_count = error_count
                import_job.processed_rows = success_count + error_count
                import_job.progress = 100
                import_job.errors = errors_list if errors_list else None
                import_job.result_message = f'{success_count} clients importés/mis à jour'
                db.session.commit()

            # Mark progress as complete
            progress_manager.complete_session(progress_session_id, success=True)

            if success_count > 0:
                flash(f"Import réussi : {success_count} clients importés/mis à jour", "success")
            if error_count > 0:
                flash(f"Erreurs de validation : {error_count} lignes ignorées", "warning")
            if parent_errors:
                flash(f"Relations parent : {len(parent_errors)} relations non créées (parents introuvables)", "info")
        except Exception as e:
            db.session.rollback()
            # Reload ImportJob after rollback to update it
            if import_job and import_job.id:
                try:
                    job = ImportJob.query.get(import_job.id)
                    if job:
                        job.mark_as_failed(str(e))
                        db.session.commit()
                except Exception:
                    pass
            progress_manager.complete_session(progress_session_id, success=False, error_message=str(e))
            flash('Une erreur est survenue lors de la sauvegarde. Veuillez reessayer.', 'error')

        return redirect(url_for('company.settings', _anchor='accounting'))

    except Exception as e:
        current_app.logger.error(f"Error importing clients from file: {e}")
        # Reload ImportJob to update it after potential rollback
        if import_job and import_job.id:
            try:
                job = ImportJob.query.get(import_job.id)
                if job:
                    job.mark_as_failed(str(e))
                    db.session.commit()
            except Exception:
                pass
        if progress_session_id:
            progress_manager.complete_session(progress_session_id, success=False, error_message=str(e))
        flash('Une erreur est survenue lors de l\'import. Veuillez reessayer.', 'error')
        return redirect(url_for('company.file_import_config'))


def _sync_invoices_delta(transformed_rows, company_id, sync_log_id=None):
    """
    Execute delta sync for invoices: update existing, create new, delete missing
    Returns: (created_count, updated_count, deleted_count)
    """
    from models import Client, Invoice
    from datetime import datetime
    from app import db

    created_count = 0
    updated_count = 0
    deleted_count = 0

    # NOTE: transformed_rows contient déjà uniquement les données (sans en-tête)
    data_rows = transformed_rows

    # Build client cache (colonnes legeres: code_client + id seulement)
    client_rows = db.session.query(Client.code_client, Client.id).filter_by(company_id=company_id).all()
    clients_cache = {row[0]: row[1] for row in client_rows}

    # Load existing invoices (colonnes legeres: invoice_number + id seulement)
    from sqlalchemy.orm import load_only
    existing_invoices = Invoice.query.filter_by(company_id=company_id).options(
        load_only(Invoice.id, Invoice.invoice_number)
    ).all()
    invoices_dict = {inv.invoice_number: inv for inv in existing_invoices}

    # Track which invoices are in the file (for cleanup later)
    file_invoice_numbers = set()

    # Lists for bulk operations
    invoices_to_create = []
    invoices_to_update = []

    # Process all rows from file
    for row_num, row in enumerate(data_rows, start=2):
        try:
            # Standard format from transform_file_to_standard_format:
            # [code_client, invoice_number, amount, original_amount, issue_date, due_date, project_name (optional)]
            if len(row) < 6:
                continue

            code_client = row[0].strip() if row[0] else ''
            invoice_number = row[1].strip() if row[1] else ''
            amount_str = row[2].strip() if row[2] else ''
            original_amount_str = row[3].strip() if row[3] else None
            issue_date_str = row[4].strip() if row[4] else ''
            due_date_str = row[5].strip() if row[5] else ''
            project_name = row[6].strip() if len(row) > 6 and row[6] else None

            if not all([code_client, invoice_number, amount_str, issue_date_str, due_date_str]):
                continue

            # Track this invoice number
            file_invoice_numbers.add(invoice_number)

            # Get client ID from cache
            client_id = clients_cache.get(code_client)
            if not client_id:
                continue

            # Parse dates
            try:
                invoice_date = datetime.strptime(issue_date_str, '%Y-%m-%d').date()
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                continue

            # Parse amount
            try:
                amount = float(amount_str.replace(',', '.'))
            except (ValueError, AttributeError):
                continue

            # Parse original_amount (optional)
            original_amount = None
            if original_amount_str:
                try:
                    original_amount = float(original_amount_str.replace(',', '.'))
                except (ValueError, AttributeError):
                    pass

            # Check if invoice already exists
            existing_invoice = invoices_dict.get(invoice_number)

            if existing_invoice:
                # UPDATE existing invoice (only update amount)
                invoices_to_update.append({
                    'id': existing_invoice.id,
                    'amount': amount,
                    'updated_at': datetime.utcnow()
                })
            else:
                # CREATE new invoice
                invoices_to_create.append({
                    'invoice_number': invoice_number,
                    'client_id': client_id,
                    'company_id': company_id,
                    'invoice_date': invoice_date,
                    'due_date': due_date,
                    'amount': amount,
                    'original_amount': original_amount,
                    'project_name': project_name,
                    'is_paid': False,
                    'created_at': datetime.utcnow(),
                    'updated_at': datetime.utcnow()
                })

        except Exception as e:
            current_app.logger.warning(f'Ligne {row_num}: {str(e)}')

    # Execute bulk operations with BATCHING to avoid SSL timeout
    BATCH_SIZE = 500

    # 1. Bulk insert new invoices (in batches)
    if invoices_to_create:
        for i in range(0, len(invoices_to_create), BATCH_SIZE):
            batch = invoices_to_create[i:i + BATCH_SIZE]
            db.session.execute(Invoice.__table__.insert(), batch)
            db.session.commit()
        created_count = len(invoices_to_create)

    # 2. Bulk update existing invoices (in batches)
    if invoices_to_update:
        for i in range(0, len(invoices_to_update), BATCH_SIZE):
            batch = invoices_to_update[i:i + BATCH_SIZE]
            db.session.bulk_update_mappings(Invoice, batch)
            db.session.commit()
        updated_count = len(invoices_to_update)

    # 3. DELETE missing invoices (in batches)
    missing_invoice_numbers = set(invoices_dict.keys()) - file_invoice_numbers

    if missing_invoice_numbers:
        missing_list = list(missing_invoice_numbers)
        for i in range(0, len(missing_list), BATCH_SIZE):
            batch = missing_list[i:i + BATCH_SIZE]
            Invoice.query.filter(
                Invoice.company_id == company_id,
                Invoice.invoice_number.in_(batch)
            ).delete(synchronize_session=False)
            db.session.commit()
        deleted_count = len(missing_invoice_numbers)


    return created_count, updated_count, deleted_count


def _sync_invoices_delta_with_session(session, transformed_rows, company_id):
    """
    Execute delta sync for invoices with ISOLATED session (for thread safety)
    Returns: (created_count, updated_count, deleted_count)
    """
    from models import Client, Invoice
    from datetime import datetime

    created_count = 0
    updated_count = 0
    deleted_count = 0

    data_rows = transformed_rows

    # Build client cache using isolated session (colonnes legeres: code_client + id)
    client_rows = session.query(Client.code_client, Client.id).filter_by(company_id=company_id).all()
    clients_cache = {row[0]: row[1] for row in client_rows}

    # Load existing invoices (colonnes legeres: invoice_number + id)
    from sqlalchemy.orm import load_only
    existing_invoices = session.query(Invoice).filter_by(company_id=company_id).options(
        load_only(Invoice.id, Invoice.invoice_number)
    ).all()
    invoices_dict = {inv.invoice_number: inv for inv in existing_invoices}

    file_invoice_numbers = set()
    invoices_to_create = []
    invoices_to_update = []

    for row_num, row in enumerate(data_rows, start=2):
        try:
            if len(row) < 6:
                continue

            code_client = row[0].strip() if row[0] else ''
            invoice_number = row[1].strip() if row[1] else ''
            amount_str = row[2].strip() if row[2] else ''
            original_amount_str = row[3].strip() if row[3] else None
            issue_date_str = row[4].strip() if row[4] else ''
            due_date_str = row[5].strip() if row[5] else ''
            project_name = row[6].strip() if len(row) > 6 and row[6] else None

            if not all([code_client, invoice_number, amount_str, issue_date_str, due_date_str]):
                continue

            file_invoice_numbers.add(invoice_number)

            client_id = clients_cache.get(code_client)
            if not client_id:
                continue

            try:
                invoice_date = datetime.strptime(issue_date_str, '%Y-%m-%d').date()
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                continue

            try:
                amount = float(amount_str.replace(',', '.'))
            except (ValueError, AttributeError):
                continue

            original_amount = None
            if original_amount_str:
                try:
                    original_amount = float(original_amount_str.replace(',', '.'))
                except (ValueError, AttributeError):
                    pass

            existing_invoice = invoices_dict.get(invoice_number)

            if existing_invoice:
                invoices_to_update.append({
                    'id': existing_invoice.id,
                    'amount': amount,
                    'updated_at': datetime.utcnow()
                })
            else:
                invoices_to_create.append({
                    'invoice_number': invoice_number,
                    'client_id': client_id,
                    'company_id': company_id,
                    'invoice_date': invoice_date,
                    'due_date': due_date,
                    'amount': amount,
                    'original_amount': original_amount,
                    'project_name': project_name,
                    'is_paid': False,
                    'created_at': datetime.utcnow(),
                    'updated_at': datetime.utcnow()
                })

        except Exception as e:
            pass  # Skip problematic rows silently

    # Execute bulk operations with BATCHING (using isolated session)
    BATCH_SIZE = 500

    if invoices_to_create:
        for i in range(0, len(invoices_to_create), BATCH_SIZE):
            batch = invoices_to_create[i:i + BATCH_SIZE]
            session.execute(Invoice.__table__.insert(), batch)
            session.commit()
        created_count = len(invoices_to_create)

    if invoices_to_update:
        for i in range(0, len(invoices_to_update), BATCH_SIZE):
            batch = invoices_to_update[i:i + BATCH_SIZE]
            session.bulk_update_mappings(Invoice, batch)
            session.commit()
        updated_count = len(invoices_to_update)

    missing_invoice_numbers = set(invoices_dict.keys()) - file_invoice_numbers

    if missing_invoice_numbers:
        missing_list = list(missing_invoice_numbers)
        for i in range(0, len(missing_list), BATCH_SIZE):
            batch = missing_list[i:i + BATCH_SIZE]
            session.query(Invoice).filter(
                Invoice.company_id == company_id,
                Invoice.invoice_number.in_(batch)
            ).delete(synchronize_session=False)
            session.commit()
        deleted_count = len(missing_invoice_numbers)

    return created_count, updated_count, deleted_count


@company_bp.route('/file-import-invoices', methods=['POST'])
@login_required
def file_import_invoices():
    """Import invoices from Excel/CSV file using saved mapping - ASYNC VERSION"""
    import threading
    from datetime import datetime
    from app import db
    from models import Company, FileImportMapping, SyncLog, ImportJob
    from file_import_connector import detect_file_type

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Vérification des permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Vous n\'avez pas les permissions pour importer des factures.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get mapping configuration
    mapping_config = FileImportMapping.query.filter_by(company_id=company.id).first()
    if not mapping_config or not mapping_config.invoice_column_mappings:
        flash("Configuration du mapping factures manquante. Configurez d'abord le mapping.", "error")
        return redirect(url_for('company.file_import_config'))

    # Check file upload
    if 'import_file' not in request.files:
        flash("Aucun fichier fourni", "error")
        return redirect(url_for('company.settings', _anchor='accounting'))

    file = request.files['import_file']
    if file.filename == '':
        flash("Nom de fichier vide", "error")
        return redirect(url_for('company.settings', _anchor='accounting'))

    # Detect file type
    file_type = detect_file_type(file.filename)
    if file_type == 'unknown':
        flash("Type de fichier non supporté. Utilisez .xlsx ou .csv", "error")
        return redirect(url_for('company.settings', _anchor='accounting'))

    # Read file content into memory (avoid temp files which may not be accessible across workers)
    file_content = file.read()

    # Create ImportJob for logging
    import_job = ImportJob(
        company_id=company.id,
        user_id=current_user.id,
        import_type='invoices',
        import_mode='sync',
        filename=file.filename,
        file_size=len(file_content),
        status='processing'
    )
    import_job.started_at = datetime.utcnow()
    db.session.add(import_job)
    db.session.commit()
    import_job_id = import_job.id

    # Save IDs and file content before creating thread
    user_id = current_user.id
    company_id = company.id
    mapping_config_id = mapping_config.id
    # Keep file content in memory for thread (temp files may not be accessible across workers)
    thread_file_content = file_content

    # Start async sync in background thread with ISOLATED DB SESSION
    def run_async_invoices_sync():
        """Synchronisation asynchrone des factures en arrière-plan avec session DB isolée"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import scoped_session, sessionmaker
        import os
        import logging

        logger = logging.getLogger(__name__)

        # Create a NEW database session for this thread (isolated from Flask's db.session)
        database_url = os.environ.get('DATABASE_URL')
        engine = create_engine(database_url, pool_pre_ping=True)
        Session = scoped_session(sessionmaker(bind=engine))
        session = Session()

        try:
            from file_import_connector import transform_file_to_standard_format
            from models import Client, Invoice, Company, FileImportMapping, ImportJob, Notification, User
            from utils.project_helper import is_project_feature_enabled

            company = session.query(Company).get(company_id)
            mapping_config = session.query(FileImportMapping).get(mapping_config_id)

            if not company or not mapping_config:
                raise Exception(f"Company ou mapping introuvable")

            # Check if project feature is enabled
            include_project = is_project_feature_enabled(company)

            # Transform file (use thread_file_content passed from main thread)
            transformed_rows, total_rows, errors = transform_file_to_standard_format(
                thread_file_content,
                file_type,
                mapping_config.invoice_column_mappings,
                'invoices',
                include_project_field=include_project
            )

            if errors and not transformed_rows:
                raise Exception(f"Transformation error: {'; '.join(errors)}")

            # Execute delta sync with ISOLATED session
            created_count, updated_count, deleted_count = _sync_invoices_delta_with_session(
                session, transformed_rows, company_id
            )

            # Update ImportJob with results
            job = session.query(ImportJob).get(import_job_id)
            if job:
                job.status = 'completed'
                job.completed_at = datetime.utcnow()
                job.total_rows = total_rows
                job.processed_rows = created_count + updated_count + deleted_count
                job.success_count = created_count + updated_count
                job.created_count = created_count
                job.updated_count = updated_count
                job.deleted_count = deleted_count
                job.progress = 100
                job.result_message = f'{created_count} créées, {updated_count} mises à jour, {deleted_count} supprimées'
                session.commit()

            # Send notification
            user = session.query(User).get(user_id)
            if user:
                notif = Notification(
                    user_id=user.id,
                    company_id=company_id,
                    type='file_import_success',
                    title='✅ Synchronisation factures terminée',
                    message=f'Import réussi : {created_count} factures créées, {updated_count} mises à jour, {deleted_count} supprimées',
                    is_read=False
                )
                session.add(notif)
                session.commit()

            logger.info(f"Import factures terminé: {created_count} créées, {updated_count} mises à jour, {deleted_count} supprimées")

            # Créer un snapshot des comptes à recevoir après import réussi
            try:
                from utils.receivables_snapshot import create_receivables_snapshot
                create_receivables_snapshot(company_id, trigger_type='import', session=session)
            except Exception as snap_err:
                logger.warning(f"Erreur création snapshot CAR: {snap_err}")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Erreur synchronisation factures: {error_msg}")

            # Rollback isolated session
            session.rollback()

            # Update ImportJob with error
            try:
                job = session.query(ImportJob).get(import_job_id)
                if job:
                    job.status = 'failed'
                    job.completed_at = datetime.utcnow()
                    job.result_message = error_msg
                    session.commit()
            except Exception as job_error:
                session.rollback()
                logger.error(f"Erreur mise à jour ImportJob: {str(job_error)}")

            # Send error notification
            try:
                user = session.query(User).get(user_id)
                if user:
                    notif = Notification(
                        user_id=user.id,
                        company_id=company_id,
                        type='file_import_error',
                        title='❌ Erreur synchronisation factures',
                        message=f'Erreur lors de l\'import : {error_msg}',
                        is_read=False
                    )
                    session.add(notif)
                    session.commit()
            except Exception as notif_error:
                logger.error(f"Erreur envoi notification: {str(notif_error)}")
        finally:
            # CRITICAL: Always close the isolated session and dispose engine
            session.close()
            Session.remove()
            engine.dispose()

    # Launch thread
    thread = threading.Thread(target=run_async_invoices_sync)
    thread.daemon = True
    thread.start()

    flash('🔄 Synchronisation des factures lancée en arrière-plan. Vous serez notifié à la fin.', 'info')
    return redirect(url_for('company.settings') + '#accounting')