# Admin Views Module - Extracted from views.py
# Contains all admin-related routes and functions
# PRESERVED: All logic, imports, decorators, and functionality from original views.py

import os
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, session
from flask_login import login_required, current_user
from defusedcsv import csv
from app import csrf, limiter
from utils.audit_service import log_action, AuditActions, EntityTypes
from constants import DEFAULT_PAGE_SIZE


class CompaniesResult:
    """Wrapper to make a list of companies behave like a pagination object for templates."""
    def __init__(self, companies_list):
        self.items = companies_list
        self.total = len(companies_list)
        self.pages = 1
        self.page = 1
        self.per_page = len(companies_list)
        self.prev_num = None
        self.next_num = None
        self.has_prev = False
        self.has_next = False
    def __iter__(self):
        return iter(self.items)
    def __len__(self):
        return len(self.items)
    def iter_pages(self):
        return [1]


# Create admin blueprint
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

@admin_bp.before_request
def require_admin():
    """Ensure user is admin before accessing admin routes"""
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))

    if not (hasattr(current_user, 'is_superuser') and current_user.is_superuser):
        flash('Accès refusé. Privilèges administrateur requis.', 'error')
        return redirect(url_for('main.dashboard'))

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
def dashboard():
    """Admin dashboard"""
    from models import User, Company, Plan

    # Get summary statistics
    total_users = User.query.count()
    total_companies = Company.query.count()
    active_plans = Plan.query.filter_by(is_active=True).count()

    # Recent activity
    recent_companies = Company.query.order_by(Company.created_at.desc()).limit(5).all()
    recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()

    # Calculer les vraies statistiques d'abonnement depuis la base de données
    # Entreprises avec un abonnement Stripe actif
    companies_with_subscription = Company.query.filter(Company.stripe_subscription_id.isnot(None)).all()

    # Filtrer par statut
    active_companies = [c for c in companies_with_subscription
                        if c.subscription_status in ['active', None] or c.subscription_status == '']
    pending_companies = [c for c in companies_with_subscription
                         if c.subscription_status == 'pending_cancellation']
    grace_companies = [c for c in companies_with_subscription
                       if c.subscription_status == 'grace_period']
    canceled_companies = [c for c in companies_with_subscription
                          if c.subscription_status == 'cancelled']

    # Calculer la somme des licences (pas juste le nombre d'abonnements)
    active_licenses = sum(c.quantity_licenses or 1 for c in active_companies)
    pending_licenses = sum(c.quantity_licenses or 1 for c in pending_companies)
    grace_licenses = sum(c.quantity_licenses or 1 for c in grace_companies)
    canceled_licenses = sum(c.quantity_licenses or 1 for c in canceled_companies)

    subscription_health = {
        'active': active_licenses,
        'pending_cancellation': pending_licenses,
        'grace_period': grace_licenses,
        'canceled': canceled_licenses
    }

    return render_template('admin/dashboard.html',
                         total_users=total_users,
                         total_companies=total_companies,
                         active_plans=active_plans,
                         recent_companies=recent_companies,
                         recent_users=recent_users,
                         subscription_health=subscription_health)

# ENDPOINTS MANQUANTS CRITIQUES - AJOUT COMPLET POUR ÉVITER ERREURS 500

@admin_bp.route('/users')
@login_required
def users():
    """Gestion des utilisateurs"""
    from models import User

    # Création form de recherche simple avec méthodes callable
    class SearchForm:
        def __init__(self):
            self.query = lambda **kwargs: f'<input type="text" name="query" class="{kwargs.get("class", "")}" placeholder="{kwargs.get("placeholder", "")}" value="">'

    search_form = SearchForm()
    page = request.args.get('page', 1, type=int)
    users_result = User.query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=DEFAULT_PAGE_SIZE, error_out=False
    )
    return render_template('admin/users.html', users=users_result, search_form=search_form)

@admin_bp.route('/companies')
@login_required
def companies():
    """Gestion des entreprises"""
    from models import Company

    # Création form de recherche simple avec méthodes callable
    class SearchForm:
        def __init__(self):
            self.query = lambda **kwargs: f'<input type="text" name="query" class="{kwargs.get("class", "")}" placeholder="{kwargs.get("placeholder", "")}" value="">'
            self.filter_by = lambda **kwargs: f'<select name="filter_by" class="{kwargs.get("class", "")}"><option value="">Tous</option></select>'
            self.plan_filter = lambda **kwargs: f'<select name="plan_filter" class="{kwargs.get("class", "")}"><option value="">Tous les plans</option></select>'

    search_form = SearchForm()
    page = request.args.get('page', 1, type=int)
    companies_result = Company.query.order_by(Company.created_at.desc()).paginate(
        page=page, per_page=DEFAULT_PAGE_SIZE, error_out=False
    )

    return render_template('admin/companies.html', companies=companies_result, search_form=search_form)

@admin_bp.route('/plans')
@login_required
def plans():
    """Gestion des plans"""
    from models import Plan
    plans = Plan.query.all()
    return render_template('admin/plans.html', plans=plans)

@admin_bp.route('/plans/edit/<int:plan_id>', methods=['GET', 'POST'])
@login_required
def edit_plan(plan_id):
    """Modifier plan"""
    from models import Plan
    from admin_forms import PlanForm
    from app import db
    from flask import flash, redirect, url_for, request

    plan = Plan.query.get_or_404(plan_id)
    form = PlanForm(obj=plan)

    if request.method == 'POST' and form.validate_on_submit():
        try:
            # Mettre à jour le plan
            form.populate_obj(plan)

            # Nettoyer les champs vides
            if not plan.stripe_product_id:
                plan.stripe_product_id = None
            if not plan.stripe_price_id:
                plan.stripe_price_id = None
            if not plan.max_clients:
                plan.max_clients = 0

            db.session.commit()

            # Invalider le cache des plans apres modification
            from utils.plan_cache import invalidate_plan_cache
            invalidate_plan_cache(plan_id)

            log_action(AuditActions.ADMIN_PLAN_CHANGED, entity_type=EntityTypes.PLAN,
                      entity_id=plan_id, entity_name=plan.display_name)

            current_app.logger.info(f"Plan modifié avec succès: {plan.display_name} (ID: {plan_id})")
            flash(f'Plan "{plan.display_name}" modifié avec succès.', 'success')
            return redirect(url_for('admin.plans'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur modification plan {plan.display_name}: {str(e)}')
            flash(f'Erreur lors de la modification du plan: {str(e)}', 'error')

    return render_template('admin/plan_form.html', form=form, plan=plan, action="Modifier")

@admin_bp.route('/subscription-management')
@login_required
def subscription_management():
    """Gestion des abonnements"""
    from models import Company, db
    from flask import request
    from datetime import datetime

    # Récupérer les paramètres de filtrage
    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()

    # Query de base pour les entreprises avec abonnements
    query = Company.query.filter(Company.stripe_subscription_id.isnot(None))

    # Filtrage par recherche
    if search_query:
        query = query.filter(
            (Company.name.ilike(f'%{search_query}%')) |
            (Company.email.ilike(f'%{search_query}%'))
        )

    # Filtrage par statut
    if status_filter:
        if status_filter == 'active':
            query = query.filter((Company.subscription_status == 'active') | (Company.subscription_status == None))
        else:
            query = query.filter(Company.subscription_status == status_filter)

    companies = query.all()

    companies_result = CompaniesResult(companies)

    # Récupérer toutes les entreprises pour les statistiques globales
    all_companies = Company.query.filter(Company.stripe_subscription_id.isnot(None)).all()

    # Statistiques COMPLÈTES pour le template subscription_management
    stats = {
        'total': len(all_companies),
        'active': len([c for c in all_companies if getattr(c, 'subscription_status', 'active') == 'active' or getattr(c, 'subscription_status', None) is None]),
        'pending_cancellation': len([c for c in all_companies if getattr(c, 'subscription_status', '') == 'pending_cancellation']),
        'cancelled': len([c for c in all_companies if getattr(c, 'subscription_status', '') == 'cancelled']),
        'canceled': len([c for c in all_companies if getattr(c, 'subscription_status', '') == 'cancelled']),  # Alias pour template
        'expired': 0  # Aucun expiré pour l'instant
    }

    # REFONTE STRIPE V2 - GracePeriod supprimé, système simplifié
    grace_periods = {}  # Ancien système supprimé dans la refonte V2

    return render_template('admin/subscription_management.html',
                         companies=companies_result,
                         stats=stats,
                         search_query=search_query,
                         status_filter=status_filter,
                         grace_periods=grace_periods,
                         moment=datetime)

@admin_bp.route('/audit-logs')
@login_required
def audit_logs():
    """Logs d'audit"""
    from models import SubscriptionAuditLog

    logs_list = SubscriptionAuditLog.query.order_by(SubscriptionAuditLog.created_at.desc()).limit(100).all()

    # Création objet avec attributs requis par template audit_logs
    class LogsResult:
        def __init__(self, logs_list):
            self.items = logs_list  # Template attend logs.items
            self.total = len(logs_list)
            self.pages = 1
            self.page = 1
            self.has_prev = False
            self.has_next = False
            self.prev_num = None
            self.next_num = None
        def __iter__(self):
            return iter(self.items)
        def __len__(self):
            return len(self.items)
        def iter_pages(self):
            return [1]  # Une seule page

    logs = LogsResult(logs_list)

    # Actions uniques pour le filtre
    unique_actions = list(set([log.action_type for log in logs_list if log.action_type]))

    # Calculer les statistiques de santé du système de manière sécurisée
    from datetime import datetime, timedelta

    total_events = len(logs_list)

    # Utiliser une approche sécurisée pour analyser les statuts
    success_events = 0
    failed_events = 0
    total_system_actions = 0
    successful_system_actions = 0

    # Événements des dernières 24h
    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)
    last_24h_events = len([log for log in logs_list if log.created_at >= yesterday])

    # Analyser chaque log de manière sécurisée (CORRECTION: utiliser system_actions_status)
    for log in logs_list:
        # Analyser les actions système depuis system_actions_status JSON
        if hasattr(log, 'system_actions_status') and log.system_actions_status:
            try:
                import json
                actions = json.loads(log.system_actions_status)

                # Compter toutes les actions et les succès
                for action_data in actions.values():
                    total_system_actions += 1
                    if action_data.get('status') == 'success':
                        successful_system_actions += 1

                # Déterminer le statut global du log basé sur les actions
                all_success = all(action.get('status') == 'success' for action in actions.values())
                any_failed = any(action.get('status') == 'failed' for action in actions.values())

                if all_success and actions:
                    success_events += 1
                elif any_failed:
                    failed_events += 1
            except Exception:
                # Si erreur JSON, compter comme échec
                failed_events += 1
        else:
            # Si pas d'actions système, utiliser action_type comme indicateur
            if hasattr(log, 'action_type') and log.action_type:
                success_events += 1  # Présence d'action = succès minimal

    # Calculer le taux de succès
    success_rate = round((success_events / total_events * 100) if total_events > 0 else 0, 1)

    health_summary = {
        'total_events': total_events,
        'success_rate': success_rate,
        'failed_events': failed_events,
        'last_24h': last_24h_events,
        'successful_actions': successful_system_actions,
        'total_actions': total_system_actions,
        'failed_actions': []
    }

    return render_template('admin/audit_logs_enriched.html',
                         logs=logs,
                         unique_actions=unique_actions,
                         company_id=None,
                         action_filter=None,
                         status_filter=None,
                         health_summary=health_summary)

@admin_bp.route('/audit-logs/export-excel')
@login_required
@limiter.limit("10 per minute")
def export_audit_logs_excel():
    """Export audit logs to Excel avec toutes les colonnes enrichies"""
    from models import SubscriptionAuditLog, Company, Plan, User
    from app import db
    import xlsxwriter
    import io
    import pytz
    from datetime import datetime
    from flask import send_file

    # Filtres depuis la requête
    company_id = request.args.get('company_id', type=int)
    action_filter = request.args.get('action', '')
    status_filter = request.args.get('status', '')

    # Requête enrichie avec toutes les jointures
    query = SubscriptionAuditLog.query.options(
        db.joinedload(SubscriptionAuditLog.company),
        db.joinedload(SubscriptionAuditLog.old_plan),
        db.joinedload(SubscriptionAuditLog.new_plan),
        db.joinedload(SubscriptionAuditLog.user)
    )

    # Appliquer les mêmes filtres que la vue
    if company_id:
        query = query.filter(SubscriptionAuditLog.company_id == company_id)
    if action_filter:
        query = query.filter(SubscriptionAuditLog.action_type == action_filter)
    # Filtrage par statut retiré temporairement car attribut global_status inexistant

    logs = query.order_by(SubscriptionAuditLog.created_at.desc()).all()

    # Créer le fichier Excel en mémoire
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)
    worksheet = workbook.add_worksheet('Audit Logs Stripe')

    # Formats pour le styling
    header_format = workbook.add_format({
        'bold': True,
        'bg_color': '#4472C4',
        'font_color': 'white',
        'border': 1
    })

    success_format = workbook.add_format({'bg_color': '#C6EFCE', 'border': 1})
    error_format = workbook.add_format({'bg_color': '#FFC7CE', 'border': 1})
    normal_format = workbook.add_format({'border': 1})

    # En-têtes des colonnes (23 colonnes)
    headers = [
        'Date/Heure (Montréal)', 'Entreprise', 'ID Entreprise', 'Action',
        'Ancien Plan', 'Nouveau Plan', 'Anciennes Licences', 'Nouvelles Licences',
        'Résumé du Changement', 'Utilisateur', 'Event Stripe ID', 'Date Effective',
        'Est Programmé', 'Date Prédite', 'Statut Global', 'Actions Système',
        'Email Status', 'Email Détails', 'DB Status', 'DB Détails',
        'Actions Réussies', 'Actions Échouées', 'Notes'
    ]

    # Écrire les en-têtes
    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)

    # Fuseau horaire de Montréal
    montreal_tz = pytz.timezone('America/Montreal')

    # Écrire les données
    for row, log in enumerate(logs, 1):
        # Conversion de la date au fuseau Montréal
        local_time = log.created_at.replace(tzinfo=pytz.UTC).astimezone(montreal_tz)
        date_str = local_time.strftime('%Y-%m-%d %H:%M:%S')

        # Analyser les actions système (CORRECTION: utiliser system_actions_status)
        actions = {}
        if hasattr(log, 'system_actions_status') and log.system_actions_status:
            try:
                import json
                actions = json.loads(log.system_actions_status)
            except Exception:
                actions = {}

        # Calculer les statistiques d'actions
        successful_actions = len([a for a in actions.values() if a.get('status') == 'success'])
        failed_actions = len([a for a in actions.values() if a.get('status') == 'failed'])

        # Données de la ligne
        row_data = [
            date_str,
            log.company.name if log.company else 'N/A',
            log.company_id or '',
            log.action_type or '',
            log.old_plan.name if log.old_plan else '',
            log.new_plan.name if log.new_plan else '',
            log.old_quantity_licenses or '',
            log.new_quantity_licenses or '',
            log.summary or '',
            log.user.email if log.user else 'Système',
            log.stripe_event_id or '',
            log.effective_date.strftime('%Y-%m-%d') if log.effective_date else '',
            'Oui' if log.is_scheduled else 'Non',
            log.predicted_date.strftime('%Y-%m-%d') if log.predicted_date else '',
            'success' if log.action_type else 'pending',  # Statut simplifié basé sur action_type
            f'{len(actions)} actions' if actions else 'Aucune',
            actions.get('email', {}).get('status', 'N/A'),
            actions.get('email', {}).get('details', ''),
            actions.get('database', {}).get('status', 'N/A'),
            actions.get('database', {}).get('details', ''),
            str(successful_actions),
            str(failed_actions),
            log.notes or ''
        ]

        # Choisir le format selon la présence d'action
        if log.action_type:
            row_format = success_format  # Action présente = succès
        else:
            row_format = normal_format   # Pas d'action = neutre

        # Écrire la ligne
        for col, data in enumerate(row_data):
            worksheet.write(row, col, data, row_format)

    # Ajuster la largeur des colonnes
    for i, header in enumerate(headers):
        if i == 0:  # Date/Heure
            worksheet.set_column(i, i, 18)
        elif i in [1, 4, 5]:  # Entreprise, Plans
            worksheet.set_column(i, i, 15)
        elif i in [8, 17, 19]:  # Résumé, détails
            worksheet.set_column(i, i, 25)
        else:
            worksheet.set_column(i, i, 12)

    workbook.close()
    output.seek(0)

    # Nom du fichier avec timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'audit_logs_stripe_{timestamp}.xlsx'

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@admin_bp.route('/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    """Supprimer un utilisateur et toutes ses données associées"""
    from models import User, EmailConfiguration, UserCompany, Notification
    from flask import flash, redirect, url_for
    from flask_wtf.csrf import validate_csrf
    from app import db

    try:
        validate_csrf(request.form.get('csrf_token'))
    except Exception as e:
        current_app.logger.warning(f"Token CSRF invalide pour suppression utilisateur: {str(e)}")
        flash('Token de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('admin.users'))

    user = User.query.get_or_404(user_id)
    user_email = user.email

    try:
        Notification.query.filter_by(user_id=user_id).delete()

        EmailConfiguration.query.filter_by(user_id=user_id).delete()

        UserCompany.query.filter_by(user_id=user_id).delete()

        db.session.delete(user)
        db.session.commit()

        log_action(AuditActions.ADMIN_USER_DELETED, entity_type=EntityTypes.USER,
                  entity_id=user_id, entity_name=user_email)

        current_app.logger.info(f"Utilisateur {user_email} (ID: {user_id}) supprimé par {current_user.email}")
        flash(f'Utilisateur {user_email} supprimé avec succès.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur suppression utilisateur {user_id}: {str(e)}")
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')

    return redirect(url_for('admin.users'))

@admin_bp.route('/delete-company/<int:company_id>', methods=['GET', 'POST'])
@login_required
def delete_company(company_id):
    """Route de suppression d'entreprise depuis panel admin"""
    from app import db
    from models import (Company, UserCompany, EmailConfiguration, Client, Invoice,
                      Notification, ClientContact, SubscriptionAuditLog)

    company = Company.query.get_or_404(company_id)
    from utils.secure_logging import sanitize_email_for_logs, sanitize_company_id_for_logs
    current_app.logger.info(f"Tentative d'accès à la suppression d'entreprise company_id={sanitize_company_id_for_logs(company_id)} par {sanitize_email_for_logs(current_user.email)}")

    if request.method == 'GET':
        # Rediriger vers la liste des entreprises - la suppression se fait directement depuis le template
        return redirect(url_for('admin.companies'))

    # POST method - proceed with deletion
    current_app.logger.info(f"Début du processus de suppression de {company.name} (ID: {company_id})")

    # Vérification CSRF avec Flask-WTF (standard)
    from flask_wtf.csrf import validate_csrf
    try:
        validate_csrf(request.form.get('csrf_token'))
    except Exception as e:
        current_app.logger.warning(f"Token CSRF invalide pour suppression de {company.name}: {str(e)}")
        flash('Token de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('admin.companies'))

    try:
        company_name = company.name
        current_app.logger.info(f"Admin suppression forcée entreprise: {company_name} (ID: {company_id})")

        # Annuler automatiquement l'abonnement Stripe
        if company.stripe_subscription_id:
            try:
                import stripe
                subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)
                if subscription.status not in ['canceled', 'incomplete_expired']:
                    stripe.Subscription.delete(company.stripe_subscription_id)
            except stripe.error.InvalidRequestError:
                # Abonnement déjà supprimé ou inexistant
                pass
            except Exception as stripe_error:
                current_app.logger.error(f"Erreur annulation Stripe pour {company_name}: {str(stripe_error)}")
                flash(f'Attention: Erreur lors de l\'annulation Stripe.', 'warning')

        # 🚀 OPTIMISATION MAXIMALE : SQL brut pour éviter OOM (Out of Memory)
        # SQLAlchemy charge les données en mémoire - SQL brut supprime directement

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

        # 2. Delete client-related data avec SQL brut
        db.session.execute(db.text("""
            DELETE FROM client_contacts
            WHERE client_id IN (
                SELECT id FROM clients WHERE company_id = :company_id
            )
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM communication_notes
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

        # 3. Delete other company data avec SQL brut
        db.session.execute(db.text("""
            DELETE FROM communication_notes WHERE company_id = :company_id
        """), {"company_id": company_id})

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

        db.session.execute(db.text("""
            DELETE FROM import_history WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM user_profiles WHERE company_id = :company_id
        """), {"company_id": company_id})

        db.session.execute(db.text("""
            DELETE FROM user_companies WHERE company_id = :company_id
        """), {"company_id": company_id})

        # 4. Finally delete the company
        db.session.delete(company)
        db.session.commit()

        log_action(AuditActions.ADMIN_COMPANY_DELETED, entity_type=EntityTypes.COMPANY,
                  entity_id=company_id, entity_name=company_name)

        flash(f'Entreprise "{company_name}" supprimée avec succès.', 'success')
        return redirect(url_for('admin.companies'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur suppression admin entreprise {company.name}: {str(e)}')
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')
        return redirect(url_for('admin.companies'))

@admin_bp.route('/system-email-configs')
@login_required
def system_email_configs():
    """Configuration emails système"""
    from models import SystemEmailConfiguration
    configs = SystemEmailConfiguration.query.all()
    return render_template('admin/system_email_configs.html', configs=configs)

@admin_bp.route('/create-user', methods=['GET', 'POST'])
@login_required
def create_user():
    """Créer utilisateur avec mot de passe temporaire"""
    from admin_forms import UserForm
    from models import User, UserCompany
    from werkzeug.security import generate_password_hash
    from app import db
    import secrets
    import string

    form = UserForm()
    action = "Créer"

    if form.validate_on_submit():
        try:
            # Vérifier si l'email existe déjà
            existing_user = User.query.filter_by(email=form.email.data).first()
            if existing_user:
                flash(f'Un utilisateur avec cet email existe déjà.', 'error')
                return render_template('admin/user_form.html', form=form, action=action)

            # Générer un mot de passe temporaire
            temp_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))

            # Créer le nouvel utilisateur
            user = User()
            user.email = form.email.data
            user.first_name = form.first_name.data
            user.last_name = form.last_name.data
            user.password_hash = generate_password_hash(temp_password)
            user.must_change_password = form.must_change_password.data if hasattr(form, 'must_change_password') else True

            if hasattr(form, 'is_superuser'):
                user.is_superuser = form.is_superuser.data if form.is_superuser.data is not None else False

            db.session.add(user)
            db.session.flush()  # Pour obtenir l'ID

            # Si une entreprise est sélectionnée, créer la relation UserCompany
            if hasattr(form, 'company_id') and form.company_id.data:
                role = form.role.data if hasattr(form, 'role') else 'employe'
                user_company = UserCompany(
                    user_id=user.id,
                    company_id=form.company_id.data,
                    role=role,
                    is_active=True
                )
                db.session.add(user_company)

            db.session.commit()

            # Envoyer l'email avec le mot de passe temporaire
            try:
                from email_fallback import send_email_via_system_config

                # Construire l'email de bienvenue
                subject = "FinovRelance - Votre compte a été créé"
                html_content = f"""
                <html>
                <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                    <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px;">
                        <h2 style="color: #8475EC; margin-bottom: 20px;">Bienvenue sur FinovRelance !</h2>

                        <p style="font-size: 16px; color: #333; margin-bottom: 15px;">
                            Bonjour {user.full_name},
                        </p>

                        <p style="font-size: 14px; color: #666; margin-bottom: 20px;">
                            Un compte a été créé pour vous sur la plateforme FinovRelance.
                        </p>

                        <div style="background-color: #fff; padding: 20px; border-radius: 8px; border: 1px solid #dee2e6; margin-bottom: 20px;">
                            <p style="margin: 0 0 10px 0;"><strong>Email :</strong> {user.email}</p>
                            <p style="margin: 0 0 10px 0;"><strong>Mot de passe temporaire :</strong></p>
                            <p style="font-size: 20px; font-family: monospace; background-color: #f1f3f4; padding: 10px; border-radius: 4px; text-align: center; margin: 0;">
                                {temp_password}
                            </p>
                        </div>

                        <p style="font-size: 14px; color: #dc3545; margin-bottom: 20px;">
                            <strong>Important :</strong> Vous devrez changer ce mot de passe lors de votre première connexion.
                        </p>

                        <a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/auth/login"
                           style="display: inline-block; background-color: #8475EC; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                            Se connecter
                        </a>

                        <p style="font-size: 12px; color: #666; margin-top: 30px;">
                            Équipe FinovRelance<br>
                            <a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}" style="color: #8475EC;">{os.environ.get('APP_URL', 'https://app.finov-relance.com').replace('https://', '')}</a>
                        </p>
                    </div>
                </body>
                </html>
                """

                send_email_via_system_config(user.email, subject, html_content)
                flash(f'Utilisateur "{user.full_name}" créé avec succès. Un email avec le mot de passe temporaire a été envoyé.', 'success')
            except Exception as email_error:
                current_app.logger.warning(f'Email non envoyé: {str(email_error)}')
                flash(f'Utilisateur "{user.full_name}" créé avec succès mais l\'email n\'a pas pu être envoyé. Veuillez réinitialiser le mot de passe manuellement.', 'warning')

            return redirect(url_for('admin.users'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur création utilisateur: {str(e)}')
            flash(f'Erreur lors de la création: {str(e)}', 'error')

    return render_template('admin/user_form.html', form=form, action=action)

@admin_bp.route('/edit-user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    """Modifier utilisateur"""
    from models import User
    from admin_forms import UserForm
    from app import db

    user = User.query.get_or_404(user_id)
    form = UserForm(obj=user)

    if form.validate_on_submit():
        try:
            # Mettre à jour les informations de l'utilisateur
            if hasattr(form, 'email') and form.email.data:
                user.email = form.email.data
            if hasattr(form, 'full_name') and form.full_name.data:
                user.full_name = form.full_name.data
            if hasattr(form, 'is_superuser') and form.is_superuser.data is not None:
                user.is_superuser = form.is_superuser.data
            if hasattr(form, 'is_active') and form.is_active.data is not None:
                user.is_active = form.is_active.data

            # Mettre à jour le mot de passe seulement s'il est fourni
            if hasattr(form, 'password') and form.password.data:
                from werkzeug.security import generate_password_hash
                user.password_hash = generate_password_hash(form.password.data)

            db.session.commit()
            flash(f'Utilisateur "{user.email}" modifié avec succès.', 'success')
            return redirect(url_for('admin.users'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur modification utilisateur: {str(e)}')
            flash(f'Erreur lors de la modification: {str(e)}', 'error')

    return render_template('admin/user_form.html', user=user, form=form, action="Modifier")

@admin_bp.route('/remove-user/<int:user_id>', methods=['POST'])
@login_required
def remove_user(user_id):
    """Supprimer utilisateur (Panel Admin - réservé aux superusers)"""
    from models import User, UserCompany
    from app import db

    # Vérifier que l'utilisateur courant est superuser (admin global)
    if not current_user.is_superuser:
        flash('Accès refusé. Seuls les super administrateurs peuvent supprimer des utilisateurs.', 'error')
        return redirect(url_for('admin.users'))

    user = User.query.get_or_404(user_id)

    # Empêcher l'auto-suppression
    if user.id == current_user.id:
        flash('Vous ne pouvez pas supprimer votre propre compte.', 'error')
        return redirect(url_for('admin.users'))

    try:
        user_name = user.full_name or user.email

        # Supprimer toutes les relations UserCompany
        UserCompany.query.filter_by(user_id=user.id).delete()

        # Supprimer l'utilisateur
        db.session.delete(user)
        db.session.commit()

        flash(f'Utilisateur "{user_name}" supprimé avec succès.', 'success')
        current_app.logger.info(f'Utilisateur {user_name} (ID: {user_id}) supprimé par {current_user.email}')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur suppression utilisateur {user_id}: {str(e)}')
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')

    return redirect(url_for('admin.users'))

@admin_bp.route('/create-company', methods=['GET', 'POST'])
@login_required
def create_company():
    """Créer entreprise"""
    from admin_forms import CompanyForm
    from models import Company, Plan
    from datetime import datetime
    from app import db

    form = CompanyForm()

    if form.validate_on_submit():
        try:
            # Créer l'entreprise
            company = Company()
            company.name = form.name.data
            company.email = form.email.data if hasattr(form, 'email') and form.email.data else None
            company.phone = form.phone.data if hasattr(form, 'phone') and form.phone.data else None
            company.address = form.address.data if hasattr(form, 'address') and form.address.data else None
            company.plan_id = form.plan_id.data if hasattr(form, 'plan_id') and form.plan_id.data and form.plan_id.data != 0 else None
            company.plan_status = form.plan_status.data if hasattr(form, 'plan_status') and form.plan_status.data else 'active'
            company.stripe_customer_id = form.stripe_customer_id.data if hasattr(form, 'stripe_customer_id') and form.stripe_customer_id.data else None
            company.stripe_subscription_id = form.stripe_subscription_id.data if hasattr(form, 'stripe_subscription_id') and form.stripe_subscription_id.data else None
            company.quantity_licenses = form.quantity_licenses.data if hasattr(form, 'quantity_licenses') and form.quantity_licenses.data else 1
            company.is_free_account = form.is_free_account.data if hasattr(form, 'is_free_account') else True
            company.client_limit = form.client_limit.data if hasattr(form, 'client_limit') and form.client_limit.data else 0
            company.registration_date = datetime.utcnow()

            # Définir le plan legacy pour compatibilité
            if company.plan_id:
                plan = Plan.query.get(company.plan_id)
                if plan:
                    company.plan = plan.name

            db.session.add(company)
            db.session.commit()

            flash(f'Entreprise "{company.name}" créée avec succès.', 'success')
            return redirect(url_for('admin.companies'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur création entreprise: {str(e)}')
            flash(f'Erreur lors de la création de l\'entreprise: {str(e)}', 'error')

    return render_template('admin/company_form.html', form=form, action="Créer")

@admin_bp.route('/create-plan', methods=['GET', 'POST'])
@login_required
def create_plan():
    """Créer plan"""
    from admin_forms import PlanForm
    from models import Plan
    from app import db
    from flask import flash, redirect, url_for, request

    form = PlanForm()

    if request.method == 'POST' and form.validate_on_submit():
        try:
            # Créer nouveau plan
            plan = Plan(
                name=form.name.data,
                display_name=form.display_name.data,
                description=form.description.data,
                plan_level=form.plan_level.data,
                is_active=form.is_active.data,
                is_free=form.is_free.data,
                stripe_product_id=form.stripe_product_id.data or None,
                stripe_price_id=form.stripe_price_id.data or None,
                max_clients=form.max_clients.data or 0,
                allows_email_sending=form.allows_email_sending.data,
                allows_email_connection=form.allows_email_connection.data,
                allows_accounting_connection=form.allows_accounting_connection.data,
                allows_team_management=form.allows_team_management.data,
                allows_email_templates=form.allows_email_templates.data
            )

            db.session.add(plan)
            db.session.commit()

            # Invalider le cache des plans apres creation
            from utils.plan_cache import invalidate_plan_cache
            invalidate_plan_cache()

            current_app.logger.info(f"Plan créé avec succès: {plan.display_name} (niveau {plan.plan_level})")
            flash(f'Plan "{plan.display_name}" créé avec succès.', 'success')
            return redirect(url_for('admin.plans'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur création plan: {str(e)}')
            flash(f'Erreur lors de la création du plan: {str(e)}', 'error')

    return render_template('admin/plan_form.html', form=form, action="Créer")

@admin_bp.route('/plans/delete/<int:plan_id>', methods=['POST'])
@login_required
def delete_plan(plan_id):
    """Supprimer plan"""
    from models import Plan
    from app import db
    from flask import flash, redirect, url_for

    plan = Plan.query.get_or_404(plan_id)
    plan_name = plan.display_name

    try:
        # Vérifier si le plan est utilisé par des entreprises
        from models import Company
        companies_using_plan = Company.query.filter_by(plan_id=plan_id).count()

        if companies_using_plan > 0:
            flash(f'Impossible de supprimer le plan "{plan_name}". {companies_using_plan} entreprise(s) utilisent encore ce plan.', 'error')
            return redirect(url_for('admin.plans'))

        # Supprimer le plan
        db.session.delete(plan)
        db.session.commit()

        # Invalider le cache des plans apres suppression
        from utils.plan_cache import invalidate_plan_cache
        invalidate_plan_cache(plan_id)

        current_app.logger.info(f"Plan supprimé avec succès: {plan_name} (ID: {plan_id})")
        flash(f'Plan "{plan_name}" supprimé avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur suppression plan {plan_name}: {str(e)}')
        flash(f'Erreur lors de la suppression du plan: {str(e)}', 'error')

    return redirect(url_for('admin.plans'))

@admin_bp.route('/system-email-configs/disconnect-oauth/<int:config_id>', methods=['POST'])
@login_required
def disconnect_system_email_oauth(config_id):
    """Déconnecter OAuth email système"""
    from models import SystemEmailConfiguration
    from app import db

    config = SystemEmailConfiguration.query.get_or_404(config_id)

    # Nettoyer les tokens OAuth
    config.outlook_oauth_access_token = None
    config.outlook_oauth_refresh_token = None
    config.outlook_oauth_token_expires = None
    config.outlook_oauth_connected_at = None

    db.session.commit()

    flash(f'OAuth déconnecté pour {config.config_name}', 'success')
    return redirect(url_for('admin.system_email_configs'))

@admin_bp.route('/system-email-configs/connect-oauth/<int:config_id>')
@login_required
def connect_system_email_oauth(config_id):
    """Connecter OAuth email système - Redirection vers Microsoft"""
    from models import SystemEmailConfiguration
    from microsoft_oauth import MicrosoftOAuthConnector

    config = SystemEmailConfiguration.query.get_or_404(config_id)

    # Stocker l'ID de config dans la session pour le callback
    session['system_email_config_id'] = config_id
    session['system_email_oauth_flow'] = True

    # Créer le connecteur OAuth et rediriger vers Microsoft
    oauth_connector = MicrosoftOAuthConnector()
    auth_url = oauth_connector.get_authorization_url()

    return redirect(auth_url)

@admin_bp.route('/system-email-configs/edit/<int:config_id>')
@login_required
def edit_system_email_config(config_id):
    """Modifier config email système"""
    from models import SystemEmailConfiguration

    config = SystemEmailConfiguration.query.get_or_404(config_id)

    # Simple form object to prevent template errors
    class SimpleForm:
        def hidden_tag(self):
            return ''

    form = SimpleForm()

    return render_template('admin/system_email_config_form.html', config=config, form=form)

@admin_bp.route('/system-email-configs/delete/<int:config_id>', methods=['POST'])
@login_required
def delete_system_email_config(config_id):
    """Supprimer config email système"""
    from app import db
    from models import SystemEmailConfiguration
    config = SystemEmailConfiguration.query.get_or_404(config_id)
    config_name = config.config_name
    db.session.delete(config)
    db.session.commit()
    flash(f'Configuration {config_name} supprimée avec succès.', 'success')
    return redirect(url_for('admin.system_email_configs'))


@admin_bp.route('/create-system-email-config')
@login_required
def create_system_email_config():
    """Créer config email"""

    # Simple form object to prevent template errors
    class SimpleForm:
        def hidden_tag(self):
            return ''

    form = SimpleForm()
    return render_template('admin/system_email_config_form.html', form=form)

@admin_bp.route('/user-companies')
@login_required
def user_companies():
    """Relations utilisateur-entreprise pour un utilisateur spécifique"""
    from models import UserCompany, User

    # Récupérer le user_id depuis les query parameters
    user_id = request.args.get('user_id', type=int)

    if not user_id:
        flash('Identifiant utilisateur manquant.', 'error')
        return redirect(url_for('admin.users'))

    # Récupérer l'utilisateur
    user = User.query.get_or_404(user_id)

    # Récupérer les relations de cet utilisateur avec les entreprises
    user_companies = UserCompany.query.filter_by(user_id=user_id).order_by(UserCompany.created_at.desc()).all()

    return render_template('admin/user_companies.html', user=user, user_companies=user_companies)

@admin_bp.route('/create-user-company', methods=['GET', 'POST'])
@login_required
def create_user_company():
    """Créer relation utilisateur-entreprise"""
    from admin_forms import UserCompanyForm
    from models import UserCompany, User, Company
    from app import db
    from werkzeug.security import check_password_hash

    form = UserCompanyForm()
    action = "Créer"

    if form.validate_on_submit():
        try:
            company = Company.query.get(form.company_id.data)
            user = User.query.get(form.user_id.data)

            if not company or not user:
                flash('Utilisateur ou entreprise introuvable.', 'error')
                return render_template('admin/user_company_form.html', form=form, action=action)

            # Vérifier si la relation existe déjà
            existing = UserCompany.query.filter_by(
                user_id=form.user_id.data,
                company_id=form.company_id.data
            ).first()

            if existing:
                flash('Cette association utilisateur-entreprise existe déjà.', 'warning')
                user_id = form.user_id.data
                if user_id:
                    return redirect(url_for('admin.user_companies', user_id=user_id))
                else:
                    return redirect(url_for('admin.users'))

            # Vérifier la limite de licences
            bypass_license = form.bypass_license.data and current_user.is_superuser

            if not bypass_license:
                # Compter les utilisateurs actifs dans l'entreprise
                active_users_count = UserCompany.query.filter_by(
                    company_id=company.id,
                    is_active=True
                ).count()

                license_limit = company.quantity_licenses or 1

                if active_users_count >= license_limit:
                    flash(f'Limite de licences atteinte ({active_users_count}/{license_limit}). '
                          f'Augmentez le nombre de licences ou utilisez l\'option "Ignorer la limite de licences".', 'error')
                    return render_template('admin/user_company_form.html', form=form, action=action)
            else:
                # Bypass demandé - vérifier le mot de passe
                admin_password = form.admin_password.data

                if not admin_password:
                    flash('Le mot de passe de confirmation est requis pour ignorer la limite de licences.', 'error')
                    return render_template('admin/user_company_form.html', form=form, action=action)

                if not check_password_hash(current_user.password_hash, admin_password):
                    flash('Mot de passe incorrect.', 'error')
                    current_app.logger.warning(f"Tentative de bypass licence avec mot de passe incorrect par {current_user.email}")
                    return render_template('admin/user_company_form.html', form=form, action=action)

            # Créer la nouvelle association
            user_company = UserCompany()
            user_company.user_id = form.user_id.data
            user_company.company_id = form.company_id.data
            user_company.role = form.role.data if hasattr(form, 'role') and form.role.data else 'employe'
            user_company.is_active = form.is_active.data

            db.session.add(user_company)
            db.session.commit()

            user_email = user.email if user else "Inconnu"
            company_name = company.name if company else "Inconnue"

            if bypass_license:
                current_app.logger.info(f"BYPASS LICENCE: {current_user.email} a ajouté {user_email} à {company_name} (support)")
                flash(f'Utilisateur "{user_email}" associé à l\'entreprise "{company_name}" avec succès (bypass licence - support).', 'success')
            else:
                flash(f'Utilisateur "{user_email}" associé à l\'entreprise "{company_name}" avec succès.', 'success')

            user_id = form.user_id.data
            if user_id:
                return redirect(url_for('admin.user_companies', user_id=user_id))
            else:
                return redirect(url_for('admin.users'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur création association utilisateur-entreprise: {str(e)}')
            flash(f'Erreur lors de la création de l\'association: {str(e)}', 'error')

    return render_template('admin/user_company_form.html', form=form, action=action)

@admin_bp.route('/remove-user-from-company/<int:user_company_id>', methods=['POST'])
@login_required
def remove_user_from_company(user_company_id):
    """Retirer un utilisateur d'une entreprise (Panel Admin - réservé aux superusers)"""
    from models import UserCompany, User
    from app import db
    from flask_wtf.csrf import validate_csrf

    # Vérifier que l'utilisateur courant est superuser (admin global)
    if not current_user.is_superuser:
        flash('Accès refusé. Seuls les super administrateurs peuvent retirer des utilisateurs.', 'error')
        return redirect(url_for('admin.users'))

    # Vérification CSRF
    try:
        validate_csrf(request.form.get('csrf_token'))
    except Exception as e:
        current_app.logger.warning(f"Token CSRF invalide pour retrait utilisateur: {str(e)}")
        flash('Token de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('admin.users'))

    user_company = UserCompany.query.get_or_404(user_company_id)
    user_id = user_company.user_id
    user = User.query.get(user_id)
    company_name = user_company.company.name if user_company.company else "Inconnue"
    user_email = user.email if user else "Inconnu"

    # Vérifier si c'est le dernier super_admin de l'entreprise (applicable pour tous, y compris auto-retrait)
    if user_company.role == 'super_admin':
        other_super_admins = UserCompany.query.filter(
            UserCompany.company_id == user_company.company_id,
            UserCompany.role == 'super_admin',
            UserCompany.id != user_company_id,
            UserCompany.is_active == True
        ).count()

        if other_super_admins == 0:
            flash(f'Veuillez assigner un autre Super Admin à l\'entreprise "{company_name}" avant de vous retirer.', 'error')
            return redirect(url_for('admin.user_companies', user_id=user_id))

    try:
        db.session.delete(user_company)
        db.session.commit()

        current_app.logger.info(f"Utilisateur {user_email} retiré de l'entreprise {company_name} par {current_user.email}")
        flash(f'Utilisateur "{user_email}" retiré de l\'entreprise "{company_name}" avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur lors du retrait utilisateur: {str(e)}')
        flash(f'Erreur lors du retrait de l\'utilisateur: {str(e)}', 'error')

    return redirect(url_for('admin.user_companies', user_id=user_id))

@admin_bp.route('/toggle-user-company/<int:user_company_id>', methods=['POST'])
@login_required
def toggle_user_company(user_company_id):
    """Activer/désactiver une association utilisateur-entreprise"""
    from models import UserCompany, User
    from app import db
    from flask_wtf.csrf import validate_csrf

    # Vérifier que l'utilisateur courant est superuser
    if not current_user.is_superuser:
        flash('Accès refusé. Seuls les super administrateurs peuvent modifier les associations.', 'error')
        return redirect(url_for('admin.users'))

    # Vérification CSRF
    try:
        validate_csrf(request.form.get('csrf_token'))
    except Exception as e:
        current_app.logger.warning(f"Token CSRF invalide pour toggle user_company: {str(e)}")
        flash('Token de sécurité invalide. Veuillez réessayer.', 'error')
        return redirect(url_for('admin.users'))

    user_company = UserCompany.query.get_or_404(user_company_id)
    user_id = user_company.user_id

    activate = request.form.get('activate') == 'true'
    company_name = user_company.company.name if user_company.company else "Inconnue"

    # Si désactivation d'un super_admin, vérifier qu'il reste d'autres super_admin actifs
    if not activate and user_company.role == 'super_admin':
        other_active_super_admins = UserCompany.query.filter(
            UserCompany.company_id == user_company.company_id,
            UserCompany.role == 'super_admin',
            UserCompany.id != user_company_id,
            UserCompany.is_active == True
        ).count()

        if other_active_super_admins == 0:
            flash(f'Impossible de désactiver le dernier super administrateur actif de l\'entreprise "{company_name}".', 'error')
            return redirect(url_for('admin.user_companies', user_id=user_id))

    try:
        user_company.is_active = activate
        db.session.commit()

        action = "activée" if activate else "désactivée"
        current_app.logger.info(f"Association user_company {user_company_id} {action} par {current_user.email}")
        flash(f'Association avec "{company_name}" {action} avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur lors du toggle user_company: {str(e)}')
        flash(f'Erreur: {str(e)}', 'error')

    return redirect(url_for('admin.user_companies', user_id=user_id))

@admin_bp.route('/company-users/<int:company_id>')
@login_required
def company_users(company_id):
    """Utilisateurs d'une entreprise"""
    from models import Company
    company = Company.query.get_or_404(company_id)
    return render_template('admin/company_users.html', company=company)

@admin_bp.route('/edit-company/<int:company_id>', methods=['GET', 'POST'])
@login_required
def edit_company(company_id):
    """Modifier entreprise"""
    from models import Company
    from admin_forms import CompanyForm
    from app import db
    from flask import flash, redirect, url_for
    from datetime import datetime

    company = Company.query.get_or_404(company_id)
    form = CompanyForm(obj=company)
    action = "Modifier"

    if form.validate_on_submit():
        try:
            # Mettre à jour les champs de base
            company.name = form.name.data
            company.email = form.email.data
            company.phone = form.phone.data
            company.address = form.address.data

            # Mettre à jour le plan (permet de supprimer le plan en sélectionnant "Aucun forfait")
            # CORRECTION: Synchroniser les champs plan_id ET plan de manière cohérente
            from models import Plan
            from sqlalchemy.orm.attributes import flag_modified
            if form.plan_id.data and form.plan_id.data != 0:
                plan_obj = Plan.query.get(form.plan_id.data)
                if plan_obj:
                    company.plan_id = plan_obj.id
                    company.plan = plan_obj.name
                    # Forcer SQLAlchemy à détecter les modifications
                    flag_modified(company, 'plan_id')
                    flag_modified(company, 'plan')
            else:
                # Si aucun plan sélectionné (0), réinitialiser à découverte (comme les webhooks)
                free_plan = Plan.query.filter_by(name='decouverte').first()
                if free_plan:
                    company.plan_id = free_plan.id
                    company.plan = 'decouverte'
                    flag_modified(company, 'plan_id')
                    flag_modified(company, 'plan')
            company.plan_status = form.plan_status.data

            # Mettre à jour les champs Stripe (convertir chaînes vides en NULL pour respecter contrainte unique)
            stripe_cust = form.stripe_customer_id.data
            stripe_sub = form.stripe_subscription_id.data
            company.stripe_customer_id = stripe_cust.strip() if stripe_cust and stripe_cust.strip() else None
            company.stripe_subscription_id = stripe_sub.strip() if stripe_sub and stripe_sub.strip() else None
            company.quantity_licenses = form.quantity_licenses.data or 1

            # Mettre à jour les overrides manuels
            company.client_limit = form.client_limit.data

            # Logique is_free_account : si on ajoute un abonnement Stripe, ce n'est plus un compte gratuit
            if form.stripe_subscription_id.data and form.stripe_subscription_id.data.strip():
                company.is_free_account = False
            else:
                company.is_free_account = form.is_free_account.data

            company.updated_at = datetime.utcnow()

            db.session.commit()
            flash(f'Entreprise "{company.name}" mise à jour avec succès.', 'success')
            return redirect(url_for('admin.companies'))

        except Exception as e:
            db.session.rollback()
            flash(f'Erreur lors de la mise à jour: {str(e)}', 'error')

    return render_template('admin/company_form.html', form=form, company=company, action=action)


@admin_bp.route('/companies/<int:company_id>/sync-stripe', methods=['POST'])
@login_required
def sync_stripe_subscription(company_id):
    """Synchroniser manuellement l'abonnement Stripe d'une entreprise"""
    from models import Company, Plan
    from app import db
    from flask import jsonify
    import stripe
    import os

    try:
        company = Company.query.get_or_404(company_id)

        if not company.stripe_subscription_id:
            return jsonify({
                'success': False,
                'message': 'Aucun abonnement Stripe associé à cette entreprise'
            }), 400

        # Récupérer l'abonnement depuis Stripe
        subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)

        # Extraire les informations
        items = subscription.get('items', {}).get('data', [])
        if not items:
            return jsonify({
                'success': False,
                'message': 'Aucun item trouvé dans l\'abonnement Stripe'
            }), 400

        # Récupérer le price_id et la quantité
        first_item = items[0]
        price_id = first_item.get('price', {}).get('id') if isinstance(first_item.get('price'), dict) else None
        quantity = first_item.get('quantity', 1)

        # Trouver le plan correspondant dans notre base
        plan = Plan.query.filter_by(stripe_price_id=price_id).first()

        # LOGGING DÉTAILLÉ pour debugging
        from flask import current_app
        current_app.logger.info(f"[SYNC STRIPE] price_id reçu de Stripe: {price_id}")
        current_app.logger.info(f"[SYNC STRIPE] Plan trouvé en DB: {plan.id if plan else 'AUCUN'} - {plan.name if plan else 'N/A'}")

        if not plan:
            return jsonify({
                'success': False,
                'message': f'Plan non trouvé pour le price_id Stripe: {price_id}'
            }), 400

        # Détecter les downgrades/modifications programmés
        schedule_id = subscription.get('schedule')
        pending_changes = None
        if schedule_id:
            try:
                schedule = stripe.SubscriptionSchedule.retrieve(schedule_id)
                pending_changes = {
                    'has_schedule': True,
                    'schedule_id': schedule_id,
                    'phases': len(schedule.get('phases', []))
                }
            except Exception:
                pending_changes = None

        # Mettre à jour la base de données
        old_plan_id = company.plan_id
        old_plan = company.plan
        old_quantity = company.quantity_licenses

        current_app.logger.info(f"[SYNC STRIPE] AVANT modification - plan_id: {old_plan_id}, plan: {old_plan}")

        # CRITIQUE: Forcer la détection des changements par SQLAlchemy
        from sqlalchemy.orm.attributes import flag_modified
        company.plan_id = plan.id
        company.plan = plan.name
        company.quantity_licenses = quantity
        company.stripe_customer_id = subscription.get('customer')

        current_app.logger.info(f"[SYNC STRIPE] APRÈS assignation - plan_id: {company.plan_id}, plan: {company.plan}")

        # Forcer SQLAlchemy à détecter les modifications (important pour les colonnes non-trackées)
        flag_modified(company, 'plan_id')
        flag_modified(company, 'plan')

        db.session.commit()

        # CRITIQUE : Mettre à jour les métadonnées Stripe avec les bonnes valeurs
        try:
            stripe.Subscription.modify(
                company.stripe_subscription_id,
                metadata={
                    'company_id': str(company.id),
                    'plan_id': str(plan.id),  # ✅ Mettre à jour avec le BON plan_id
                    'quantity_licenses': str(quantity)
                }
            )
            current_app.logger.info(f"[SYNC STRIPE] Métadonnées Stripe mises à jour: plan_id={plan.id}")
        except Exception as meta_error:
            current_app.logger.warning(f"[SYNC STRIPE] Impossible de mettre à jour métadonnées: {meta_error}")

        # Vérifier après commit
        db.session.refresh(company)
        current_app.logger.info(f"[SYNC STRIPE] APRÈS commit+refresh - plan_id: {company.plan_id}, plan: {company.plan}")

        return jsonify({
            'success': True,
            'message': 'Abonnement synchronisé avec succès',
            'data': {
                'old_plan_id': old_plan_id,
                'new_plan_id': plan.id,
                'plan_name': plan.display_name,
                'old_quantity': old_quantity,
                'new_quantity': quantity,
                'price_id': price_id,
                'pending_changes': pending_changes,
                'subscription_status': subscription.get('status')
            }
        })

    except stripe.error.StripeError as e:
        return jsonify({
            'success': False,
            'message': f'Erreur Stripe: {str(e)}'
        }), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'Erreur: {str(e)}'
        }), 500


@admin_bp.route('/companies/<int:company_id>/stop-sync', methods=['POST'])
@login_required
def stop_company_sync(company_id):
    """Arrêter manuellement les synchronisations en cours pour une entreprise"""
    from models import Company, AccountingConnection, SyncLog
    from app import db
    from datetime import datetime
    from flask import jsonify

    try:
        company = Company.query.get(company_id)
        if not company:
            return jsonify({
                'success': False,
                'message': 'Entreprise introuvable'
            }), 404

        # Trouver tous les SyncLog actifs pour cette entreprise
        active_syncs = db.session.query(SyncLog).join(
            AccountingConnection,
            SyncLog.connection_id == AccountingConnection.id
        ).filter(
            AccountingConnection.company_id == company_id,
            SyncLog.status == 'running'
        ).all()

        if not active_syncs:
            return jsonify({
                'success': False,
                'message': 'Aucune synchronisation en cours pour cette entreprise'
            }), 404

        # FORCE-STOP: Arrêter IMMÉDIATEMENT toutes les syncs actives
        # Même si le thread est bloqué/mort, ça libère l'utilisateur
        stopped_count = 0
        for sync_log in active_syncs:
            sync_log.manual_stop_requested_at = datetime.utcnow()
            sync_log.status = 'stopped'
            sync_log.error_message = f'Arrêté manuellement par admin ({current_user.email})'
            sync_log.completed_at = datetime.utcnow()
            stopped_count += 1

        db.session.commit()

        from utils.secure_logging import sanitize_email_for_logs, sanitize_company_id_for_logs
        current_app.logger.info(f"🛑 FORCE-STOP: Admin {sanitize_email_for_logs(current_user.email)} a arrêté {stopped_count} synchronisation(s) pour company_id={sanitize_company_id_for_logs(company.id)}")

        return jsonify({
            'success': True,
            'message': f'{stopped_count} synchronisation(s) arrêtée(s) immédiatement',
            'stopped_count': stopped_count
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur lors de l'arrêt des syncs: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Erreur: {str(e)}'
        }), 500


@admin_bp.route('/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    """Réinitialiser le mot de passe d'un utilisateur"""
    from models import User
    from werkzeug.security import generate_password_hash
    from flask import flash, redirect, url_for
    import secrets
    import string

    user = User.query.get_or_404(user_id)

    alphabet = string.ascii_letters + string.digits
    temp_password = ''.join(secrets.choice(alphabet) for i in range(12))

    user.password_hash = generate_password_hash(temp_password)
    user.must_change_password = True

    try:
        from app import db
        db.session.commit()

        try:
            from email_fallback import send_email_via_system_config

            # Construire l'email de réinitialisation
            subject = "FinovRelance - Réinitialisation de votre mot de passe"
            html_content = f"""
            <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px;">
                    <h2 style="color: #8475EC; margin-bottom: 20px;">Réinitialisation de mot de passe</h2>

                    <p style="font-size: 16px; color: #333; margin-bottom: 15px;">
                        Bonjour {user.first_name or user.full_name},
                    </p>

                    <p style="font-size: 14px; color: #666; margin-bottom: 20px;">
                        Votre mot de passe a été réinitialisé par un administrateur.
                    </p>

                    <div style="background-color: #fff; padding: 20px; border-radius: 8px; border: 1px solid #dee2e6; margin-bottom: 20px;">
                        <p style="margin: 0 0 10px 0;"><strong>Nouveau mot de passe temporaire :</strong></p>
                        <p style="font-size: 20px; font-family: monospace; background-color: #f1f3f4; padding: 10px; border-radius: 4px; text-align: center; margin: 0;">
                            {temp_password}
                        </p>
                    </div>

                    <p style="font-size: 14px; color: #dc3545; margin-bottom: 20px;">
                        <strong>Important :</strong> Vous devrez changer ce mot de passe lors de votre prochaine connexion.
                    </p>

                    <a href="{os.environ.get('APP_URL', 'https://app.finov-relance.com')}/auth/login"
                       style="display: inline-block; background-color: #8475EC; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                        Se connecter
                    </a>
                </div>
            </body>
            </html>
            """

            send_email_via_system_config(user.email, subject, html_content)
            flash(f'Mot de passe réinitialisé pour {user.email}. Un email contenant le nouveau mot de passe a été envoyé.', 'success')
        except Exception as email_error:
            current_app.logger.error(f"Erreur envoi email reset password: {email_error}")
            flash(f'Mot de passe réinitialisé pour {user.email} mais l\'email n\'a pas pu être envoyé. Veuillez contacter l\'utilisateur directement.', 'warning')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur réinitialisation mot de passe: {e}")
        flash('Erreur lors de la réinitialisation du mot de passe.', 'error')

    return redirect(url_for('admin.users'))

@admin_bp.route('/subscription/force-reactivate/<int:company_id>', methods=['POST'])
@login_required
def force_reactivate_subscription(company_id):
    """Réactiver de force un abonnement"""
    from models import Company
    from flask import flash, redirect, url_for

    company = Company.query.get_or_404(company_id)

    try:
        # REFONTE STRIPE V2 - Système simplifié
        company.subscription_status = 'active'
        # Note: can_reactivate et cancellation_date supprimés dans la refonte V2

        from app import db
        db.session.commit()
        flash(f'Abonnement de {company.name} réactivé avec succès.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de la réactivation: {str(e)}', 'error')

    return redirect(url_for('admin.subscription_management'))


@admin_bp.route('/consent-logs')
@login_required
def consent_logs():
    """Consulter l'historique des consentements RGPD/Loi 25"""
    from models import ConsentLog, User
    from sqlalchemy import desc

    # Récupérer tous les consentements, triés par date
    page = request.args.get('page', 1, type=int)
    user_id_filter = request.args.get('user_id', type=int)
    consent_type_filter = request.args.get('consent_type', type=str)

    query = ConsentLog.query

    # Filtres optionnels
    if user_id_filter:
        query = query.filter_by(user_id=user_id_filter)
    if consent_type_filter:
        query = query.filter_by(consent_type=consent_type_filter)

    # Pagination
    consents = query.order_by(desc(ConsentLog.created_at)).paginate(
        page=page, per_page=DEFAULT_PAGE_SIZE, error_out=False
    )

    # Récupérer la liste des utilisateurs pour le filtre
    users = User.query.order_by(User.email).all()

    return render_template('admin/consent_logs.html',
                         consents=consents,
                         users=users,
                         user_id_filter=user_id_filter,
                         consent_type_filter=consent_type_filter)


@admin_bp.route('/consent-logs/user/<int:user_id>')
@login_required
def user_consent_logs(user_id):
    """Consulter l'historique des consentements d'un utilisateur spécifique"""
    from models import ConsentLog, User
    from sqlalchemy import desc

    user = User.query.get_or_404(user_id)

    # Récupérer tous les consentements de l'utilisateur
    consents = ConsentLog.query.filter_by(user_id=user_id).order_by(
        desc(ConsentLog.created_at)
    ).all()

    # Regrouper par type de consentement
    consents_by_type = {
        'terms': [c for c in consents if c.consent_type == 'terms'],
        'privacy': [c for c in consents if c.consent_type == 'privacy'],
        'cookies': [c for c in consents if c.consent_type == 'cookies']
    }

    return render_template('admin/user_consent_logs.html',
                         user=user,
                         consents=consents,
                         consents_by_type=consents_by_type)


# =============================================================================
# GUIDE PAGES MANAGEMENT
# =============================================================================

@admin_bp.route('/guides')
@login_required
def guides():
    """Liste des pages de guide"""
    from models import GuidePage

    guides = GuidePage.query.order_by(GuidePage.order.asc(), GuidePage.created_at.desc()).all()
    return render_template('admin/guides.html', guides=guides)


@admin_bp.route('/guides/create', methods=['GET', 'POST'])
@login_required
def create_guide():
    """Créer une page de guide"""
    from admin_forms import GuidePageForm
    from models import GuidePage
    from app import db

    form = GuidePageForm()

    if form.validate_on_submit():
        try:
            guide = GuidePage(
                title=form.title.data,
                slug=form.slug.data,
                meta_description=form.meta_description.data,
                content=form.content.data,
                image_url=form.image_url.data,
                video_url=form.video_url.data,
                is_published=form.is_published.data,
                order=form.order.data or 0
            )

            db.session.add(guide)
            db.session.commit()

            current_app.logger.info(f"Page de guide créée: {guide.title} (slug: {guide.slug})")
            flash(f'Page de guide "{guide.title}" créée avec succès.', 'success')
            return redirect(url_for('admin.guides'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur création page de guide: {str(e)}')
            flash(f'Erreur lors de la création: {str(e)}', 'error')

    return render_template('admin/guide_form.html', form=form, action="Créer")


@admin_bp.route('/guides/<int:guide_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_guide(guide_id):
    """Modifier une page de guide"""
    from models import GuidePage
    from admin_forms import GuidePageForm
    from app import db

    guide = GuidePage.query.get_or_404(guide_id)
    form = GuidePageForm(obj=guide)

    if form.validate_on_submit():
        try:
            form.populate_obj(guide)

            if not guide.order:
                guide.order = 0

            db.session.commit()

            current_app.logger.info(f"Page de guide modifiée: {guide.title} (ID: {guide_id})")
            flash(f'Page de guide "{guide.title}" modifiée avec succès.', 'success')
            return redirect(url_for('admin.guides'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Erreur modification page de guide {guide.title}: {str(e)}')
            flash(f'Erreur lors de la modification: {str(e)}', 'error')

    return render_template('admin/guide_form.html', form=form, guide=guide, action="Modifier")


@admin_bp.route('/guides/<int:guide_id>/delete', methods=['POST'])
@login_required
def delete_guide(guide_id):
    """Supprimer une page de guide"""
    from models import GuidePage
    from app import db

    guide = GuidePage.query.get_or_404(guide_id)
    guide_title = guide.title

    try:
        db.session.delete(guide)
        db.session.commit()

        current_app.logger.info(f"Page de guide supprimée: {guide_title} (ID: {guide_id})")
        flash(f'Page de guide "{guide_title}" supprimée avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Erreur suppression page de guide {guide_title}: {str(e)}')
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')

    return redirect(url_for('admin.guides'))


@admin_bp.route('/guides/upload-image', methods=['POST'])
@login_required
def upload_guide_image():
    """Upload d'image pour les guides"""
    import os
    from datetime import datetime
    import uuid
    from flask_wtf.csrf import validate_csrf
    from wtforms import ValidationError

    # Validation CSRF explicite pour requêtes AJAX
    try:
        csrf_token = request.headers.get('X-CSRFToken')
        if not csrf_token:
            return {'error': 'Token CSRF manquant'}, 403
        validate_csrf(csrf_token)
    except ValidationError:
        return {'error': 'Token CSRF invalide'}, 403

    # Limite de taille côté serveur: 5MB
    MAX_FILE_SIZE = 5 * 1024 * 1024

    if 'file' not in request.files:
        return {'error': 'Aucun fichier fourni'}, 400

    file = request.files['file']

    if file.filename == '':
        return {'error': 'Nom de fichier vide'}, 400

    # Vérifier la taille du fichier
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)  # Reset position

    if file_size > MAX_FILE_SIZE:
        return {'error': f'Fichier trop volumineux. Taille maximale: 5MB'}, 400

    # Extensions autorisées
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

    def allowed_file(filename):
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

    if not allowed_file(file.filename):
        return {'error': 'Type de fichier non autorisé. Utilisez: PNG, JPG, JPEG, GIF, WEBP'}, 400

    try:
        import base64
        import imghdr
        from io import BytesIO
        from PIL import Image

        ext = file.filename.rsplit('.', 1)[1].lower()
        file_data = file.read()

        MAX_DIMENSION = 1920
        JPEG_QUALITY = 85

        img = Image.open(BytesIO(file_data))

        if img.mode == 'RGBA' and ext not in ('png', 'webp'):
            img = img.convert('RGB')

        width, height = img.size
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
            current_app.logger.info(f"Image redimensionnée de {width}x{height} à {img.size[0]}x{img.size[1]}")

        output = BytesIO()
        if ext in ('jpg', 'jpeg'):
            if img.mode == 'RGBA':
                img = img.convert('RGB')
            img.save(output, format='JPEG', quality=JPEG_QUALITY, optimize=True)
            mime_type = 'image/jpeg'
        elif ext == 'png':
            img.save(output, format='PNG', optimize=True)
            mime_type = 'image/png'
        elif ext == 'webp':
            img.save(output, format='WEBP', quality=JPEG_QUALITY, optimize=True)
            mime_type = 'image/webp'
        elif ext == 'gif':
            img.save(output, format='GIF')
            mime_type = 'image/gif'
        else:
            if img.mode == 'RGBA':
                img = img.convert('RGB')
            img.save(output, format='JPEG', quality=JPEG_QUALITY, optimize=True)
            mime_type = 'image/jpeg'

        optimized_data = output.getvalue()
        base64_encoded = base64.b64encode(optimized_data).decode('utf-8')
        data_uri = f"data:{mime_type};base64,{base64_encoded}"

        current_app.logger.info(f"Image de guide uploadée en Base64 ({mime_type}, {file_size} -> {len(optimized_data)} bytes)")

        return {'url': data_uri, 'filename': file.filename}, 200

    except Exception as e:
        current_app.logger.error(f'Erreur upload image guide: {str(e)}')
        return {'error': f'Erreur lors de l\'upload: {str(e)}'}, 500


@admin_bp.route('/guides/upload-video', methods=['POST'])
@login_required
def upload_guide_video():
    """Upload de vidéo pour les guides"""
    import os
    from datetime import datetime
    import uuid
    from flask_wtf.csrf import validate_csrf
    from wtforms import ValidationError

    # Validation CSRF explicite pour requêtes AJAX
    try:
        csrf_token = request.headers.get('X-CSRFToken')
        if not csrf_token:
            return {'error': 'Token CSRF manquant'}, 403
        validate_csrf(csrf_token)
    except ValidationError:
        return {'error': 'Token CSRF invalide'}, 403

    # Limite de taille côté serveur: 50MB pour vidéos
    MAX_FILE_SIZE = 50 * 1024 * 1024

    if 'file' not in request.files:
        return {'error': 'Aucun fichier fourni'}, 400

    file = request.files['file']

    if file.filename == '':
        return {'error': 'Nom de fichier vide'}, 400

    # Vérifier la taille du fichier
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)  # Reset position

    if file_size > MAX_FILE_SIZE:
        return {'error': f'Fichier trop volumineux. Taille maximale: 50MB'}, 400

    # Extensions autorisées pour vidéos
    ALLOWED_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov'}

    def allowed_file(filename):
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

    if not allowed_file(file.filename):
        return {'error': 'Type de fichier non autorisé. Utilisez: MP4, WEBM, OGG, MOV'}, 400

    try:
        import base64

        ext = file.filename.rsplit('.', 1)[1].lower()
        file_data = file.read()

        mime_map = {'mp4': 'video/mp4', 'webm': 'video/webm', 'ogg': 'video/ogg', 'mov': 'video/quicktime'}
        mime_type = mime_map.get(ext, f'video/{ext}')

        base64_encoded = base64.b64encode(file_data).decode('utf-8')
        data_uri = f"data:{mime_type};base64,{base64_encoded}"

        current_app.logger.info(f"Vidéo de guide uploadée en Base64 ({mime_type}, {file_size} bytes)")

        return {'url': data_uri, 'filename': file.filename}, 200

    except Exception as e:
        current_app.logger.error(f'Erreur upload vidéo guide: {str(e)}')
        return {'error': f'Erreur lors de l\'upload: {str(e)}'}, 500


@admin_bp.route('/migrate-logos-to-base64', methods=['POST'])
@login_required
def migrate_logos_to_base64():
    """
    Migration des logos existants vers Base64 (survie aux redéploiements)
    À exécuter UNE FOIS pour migrer les logos existants
    """
    from app import db
    from models import Company
    import os
    import base64
    import imghdr

    results = {
        'success': 0,
        'skipped': 0,
        'failed': 0,
        'errors': []
    }

    companies = Company.query.filter(Company.logo_path != None, Company.logo_path != '').all()

    for company in companies:
        try:
            # Skip si déjà migré
            if company.logo_base64:
                results['skipped'] += 1
                current_app.logger.info(f"Logo déjà migré pour {company.name}, passage")
                continue

            # Construire le chemin du fichier
            logo_file_path = os.path.join('static', 'uploads', 'logos', company.logo_path)

            if not os.path.exists(logo_file_path):
                results['failed'] += 1
                error_msg = f"{company.name}: Fichier introuvable ({logo_file_path})"
                results['errors'].append(error_msg)
                current_app.logger.warning(f"Fichier logo introuvable pour {company.name}: {logo_file_path}")
                continue

            # Lire le fichier
            with open(logo_file_path, 'rb') as f:
                logo_data = f.read()

            # Détecter le type MIME
            image_type = imghdr.what(None, h=logo_data)
            if image_type:
                mime_type = f"image/{image_type}"
            else:
                # Fallback basé sur l'extension
                ext = company.logo_path.lower().rsplit('.', 1)[-1]
                mime_mapping = {
                    'png': 'image/png',
                    'jpg': 'image/jpeg',
                    'jpeg': 'image/jpeg',
                    'gif': 'image/gif',
                    'webp': 'image/webp'
                }
                mime_type = mime_mapping.get(ext, 'image/png')

            # Encoder en Base64
            logo_base64_encoded = base64.b64encode(logo_data).decode('utf-8')
            company.logo_base64 = f"data:{mime_type};base64,{logo_base64_encoded}"

            results['success'] += 1
            current_app.logger.info(f"Logo migré vers Base64 pour {company.name} ({mime_type}, {len(logo_data)} bytes)")

        except Exception as e:
            results['failed'] += 1
            error_msg = f"{company.name}: {str(e)}"
            results['errors'].append(error_msg)
            current_app.logger.error(f"Erreur migration logo pour {company.name}: {str(e)}")

    # Sauvegarder toutes les modifications
    try:
        db.session.commit()

        # Message de succès détaillé
        success_msg = f'Migration logos terminée: {results["success"]} succès, {results["skipped"]} déjà migrés, {results["failed"]} échecs'
        if results['errors']:
            success_msg += f' (Erreurs: {"; ".join(results["errors"][:3])})'  # Montrer max 3 erreurs

        flash(success_msg, 'success' if results['failed'] == 0 else 'warning')
        current_app.logger.info(f'Migration logos Base64 terminée: {results}')

    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de la sauvegarde: {str(e)}', 'error')
        current_app.logger.error(f'Erreur commit migration logos: {str(e)}')

    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/migrate-guide-images-to-base64', methods=['POST'])
@login_required
def migrate_guide_images_to_base64():
    """
    Migration des images de guides existantes vers Base64 (survie aux redéploiements)
    Convertit les image_url, video_url fichiers locaux et les images inline dans content
    """
    from app import db
    from models import GuidePage
    import os
    import base64
    import imghdr
    import re

    results = {
        'success': 0,
        'skipped': 0,
        'failed': 0,
        'errors': []
    }

    guides = GuidePage.query.all()

    for guide in guides:
        try:
            changed = False

            if guide.image_url and guide.image_url.startswith('/static/'):
                file_path = os.path.join(current_app.root_path, guide.image_url.lstrip('/'))
                if os.path.exists(file_path):
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    image_type = imghdr.what(None, h=file_data)
                    mime_map = {'png': 'image/png', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp'}
                    ext = file_path.rsplit('.', 1)[-1].lower()
                    mime_type = mime_map.get(image_type, f'image/{ext}')
                    encoded = base64.b64encode(file_data).decode('utf-8')
                    guide.image_url = f"data:{mime_type};base64,{encoded}"
                    changed = True
                    current_app.logger.info(f"Image guide '{guide.title}' migrée vers Base64")
                else:
                    results['errors'].append(f"{guide.title}: image_url fichier introuvable ({file_path})")

            if guide.video_url and guide.video_url.startswith('/static/'):
                file_path = os.path.join(current_app.root_path, guide.video_url.lstrip('/'))
                if os.path.exists(file_path):
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    ext = file_path.rsplit('.', 1)[-1].lower()
                    mime_map = {'mp4': 'video/mp4', 'webm': 'video/webm', 'ogg': 'video/ogg', 'mov': 'video/quicktime'}
                    mime_type = mime_map.get(ext, f'video/{ext}')
                    encoded = base64.b64encode(file_data).decode('utf-8')
                    guide.video_url = f"data:{mime_type};base64,{encoded}"
                    changed = True
                    current_app.logger.info(f"Vidéo guide '{guide.title}' migrée vers Base64")
                else:
                    results['errors'].append(f"{guide.title}: video_url fichier introuvable ({file_path})")

            if guide.content:
                def replace_local_img(match):
                    src = match.group(1)
                    if not src.startswith('/static/'):
                        return match.group(0)
                    img_path = os.path.join(current_app.root_path, src.lstrip('/'))
                    if not os.path.exists(img_path):
                        return match.group(0)
                    try:
                        with open(img_path, 'rb') as f:
                            img_data = f.read()
                        img_type = imghdr.what(None, h=img_data)
                        img_mime_map = {'png': 'image/png', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp'}
                        img_ext = img_path.rsplit('.', 1)[-1].lower()
                        img_mime = img_mime_map.get(img_type, f'image/{img_ext}')
                        img_encoded = base64.b64encode(img_data).decode('utf-8')
                        return match.group(0).replace(src, f"data:{img_mime};base64,{img_encoded}")
                    except Exception:
                        return match.group(0)

                new_content = re.sub(r'<img[^>]+src=["\']([^"\']+)["\']', replace_local_img, guide.content)
                if new_content != guide.content:
                    guide.content = new_content
                    changed = True
                    current_app.logger.info(f"Images inline du guide '{guide.title}' migrées vers Base64")

            if changed:
                results['success'] += 1
            else:
                results['skipped'] += 1

        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"{guide.title}: {str(e)}")
            current_app.logger.error(f"Erreur migration guide '{guide.title}': {str(e)}")

    try:
        db.session.commit()
        success_msg = f'Migration guides terminée: {results["success"]} migrés, {results["skipped"]} déjà OK, {results["failed"]} échecs'
        if results['errors']:
            success_msg += f' (Erreurs: {"; ".join(results["errors"][:3])})'
        flash(success_msg, 'success' if results['failed'] == 0 else 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de la sauvegarde: {str(e)}', 'error')

    return redirect(url_for('admin.guides'))


@admin_bp.route('/migrate-signatures-to-base64', methods=['POST'])
@login_required
def migrate_signatures_to_base64():
    """
    Migration des signatures email existantes vers Base64 (survie aux redéploiements)
    Convertit toutes les images locales (<img src="/static/...">) en data URIs Base64
    À exécuter UNE FOIS pour migrer les signatures existantes
    """
    from app import db
    from models import EmailConfiguration
    from utils import convert_signature_images_to_base64

    results = {
        'success': 0,
        'skipped': 0,
        'failed': 0,
        'errors': []
    }

    # Récupérer toutes les configurations email avec signature non vide
    email_configs = EmailConfiguration.query.filter(
        EmailConfiguration.email_signature != None,
        EmailConfiguration.email_signature != ''
    ).all()

    current_app.logger.info(f"Démarrage migration signatures Base64 pour {len(email_configs)} configuration(s)")

    for email_config in email_configs:
        try:
            # Vérifier si la signature contient des images locales à convertir
            if '/static/' not in email_config.email_signature:
                results['skipped'] += 1
                current_app.logger.info(
                    f"Signature sans images locales pour User ID {email_config.user_id} / Company ID {email_config.company_id}, passage"
                )
                continue

            # Vérifier si déjà converti (si contient déjà des data URIs)
            if 'data:image/' in email_config.email_signature and '/static/' not in email_config.email_signature:
                results['skipped'] += 1
                current_app.logger.info(
                    f"Signature déjà migrée pour User ID {email_config.user_id} / Company ID {email_config.company_id}, passage"
                )
                continue

            # Convertir les images locales en Base64
            original_signature = email_config.email_signature
            converted_signature = convert_signature_images_to_base64(original_signature)

            # Mettre à jour si la conversion a eu lieu
            if converted_signature != original_signature:
                email_config.email_signature = converted_signature
                results['success'] += 1
                current_app.logger.info(
                    f"Signature migrée vers Base64 pour User ID {email_config.user_id} / Company ID {email_config.company_id}"
                )
            else:
                results['skipped'] += 1
                current_app.logger.info(
                    f"Aucune conversion nécessaire pour User ID {email_config.user_id} / Company ID {email_config.company_id}"
                )

        except Exception as e:
            results['failed'] += 1
            error_msg = f"User {email_config.user_id} / Company {email_config.company_id}: {str(e)}"
            results['errors'].append(error_msg)
            current_app.logger.error(
                f"Erreur migration signature pour User ID {email_config.user_id} / Company ID {email_config.company_id}: {str(e)}"
            )

    # Sauvegarder toutes les modifications
    try:
        db.session.commit()

        # Message de succès détaillé
        success_msg = f'Migration signatures terminée: {results["success"]} succès, {results["skipped"]} ignorés, {results["failed"]} échecs'
        if results['errors']:
            success_msg += f' (Erreurs: {"; ".join(results["errors"][:3])})'  # Montrer max 3 erreurs

        flash(success_msg, 'success' if results['failed'] == 0 else 'warning')
        current_app.logger.info(f'Migration signatures Base64 terminée: {results}')

    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de la sauvegarde: {str(e)}', 'error')
        current_app.logger.error(f'Erreur commit migration signatures: {str(e)}')

    return redirect(url_for('admin.dashboard'))


# =====================================================
# SYSTÈME D'AUDIT - Routes pour les logs
# =====================================================

@admin_bp.route('/user-audit-logs')
@login_required
def user_audit_logs():
    """Logs d'audit des actions utilisateurs"""
    from app import db
    from models import AuditLog, User, Company
    from datetime import datetime, timedelta

    page = request.args.get('page', 1, type=int)
    per_page = DEFAULT_PAGE_SIZE

    user_filter = request.args.get('user_id', type=int)
    company_filter = request.args.get('company_id', type=int)
    action_filter = request.args.get('action')
    entity_filter = request.args.get('entity_type')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    query = AuditLog.query

    if user_filter:
        query = query.filter(AuditLog.user_id == user_filter)
    if company_filter:
        query = query.filter(AuditLog.company_id == company_filter)
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    if entity_filter:
        query = query.filter(AuditLog.entity_type == entity_filter)
    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(AuditLog.created_at >= date_from_dt)
        except ValueError:
            pass
    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(AuditLog.created_at < date_to_dt)
        except ValueError:
            pass

    logs = query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    unique_actions = db.session.query(AuditLog.action).distinct().all()
    unique_actions = [a[0] for a in unique_actions if a[0]]

    unique_entity_types = db.session.query(AuditLog.entity_type).distinct().all()
    unique_entity_types = [e[0] for e in unique_entity_types if e[0]]

    users = User.query.order_by(User.email).all()
    companies = Company.query.order_by(Company.name).all()

    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)
    last_week = now - timedelta(days=7)

    stats = {
        'total': AuditLog.query.count(),
        'last_24h': AuditLog.query.filter(AuditLog.created_at >= yesterday).count(),
        'last_week': AuditLog.query.filter(AuditLog.created_at >= last_week).count(),
        'login_success': AuditLog.query.filter(AuditLog.action == 'login_success').count(),
        'login_failed': AuditLog.query.filter(AuditLog.action == 'login_failed').count()
    }

    return render_template('admin/user_audit_logs.html',
                         logs=logs,
                         unique_actions=unique_actions,
                         unique_entity_types=unique_entity_types,
                         users=users,
                         companies=companies,
                         stats=stats,
                         user_filter=user_filter,
                         company_filter=company_filter,
                         action_filter=action_filter,
                         entity_filter=entity_filter,
                         date_from=date_from,
                         date_to=date_to)


@admin_bp.route('/user-audit-logs/export-csv')
@login_required
@limiter.limit("10 per minute")
def export_user_audit_logs_csv():
    """Export des logs d'audit utilisateurs en CSV avec les mêmes filtres que l'affichage"""
    import io
    from flask import Response
    from models import AuditLog
    from datetime import datetime, timedelta
    import json

    user_filter = request.args.get('user_id', type=int)
    company_filter = request.args.get('company_id', type=int)
    action_filter = request.args.get('action')
    entity_filter = request.args.get('entity_type')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    query = AuditLog.query

    if user_filter:
        query = query.filter(AuditLog.user_id == user_filter)
    if company_filter:
        query = query.filter(AuditLog.company_id == company_filter)
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    if entity_filter:
        query = query.filter(AuditLog.entity_type == entity_filter)
    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(AuditLog.created_at >= date_from_dt)
        except ValueError:
            pass
    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(AuditLog.created_at < date_to_dt)
        except ValueError:
            pass

    logs = query.order_by(AuditLog.created_at.desc()).limit(10000).all()

    from models import Company, User
    company_ids = set(log.company_id for log in logs if log.company_id)
    user_ids = set(log.user_id for log in logs if log.user_id)

    companies_map = {c.id: c.name for c in Company.query.filter(Company.id.in_(company_ids)).all()} if company_ids else {}
    users_map = {u.id: u.full_name for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_ALL)

    writer.writerow([
        'Date/Heure', 'Utilisateur', 'Email', 'Entreprise', 'Action',
        'Type Entite', 'ID Entite', 'Nom Entite', 'Details', 'IP', 'User Agent'
    ])

    for log in logs:
        company_name = companies_map.get(log.company_id, '') if log.company_id else ''
        user_name = users_map.get(log.user_id, '') if log.user_id else ''

        details_str = ''
        if log.details:
            try:
                details_str = json.dumps(log.details, ensure_ascii=False)
            except Exception:
                details_str = str(log.details)

        writer.writerow([
            log.created_at.strftime('%Y-%m-%d %H:%M:%S') if log.created_at else '',
            user_name,
            log.user_email or '',
            company_name,
            log.action or '',
            log.entity_type or '',
            log.entity_id or '',
            log.entity_name or '',
            details_str,
            log.ip_address or '',
            (log.user_agent or '')[:100]
        ])

    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename=audit_logs_{timestamp}.csv',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )


@admin_bp.route('/cron-job-logs')
@login_required
def cron_job_logs():
    """Logs des exécutions de cron jobs"""
    from app import db
    from models import CronJobLog
    from datetime import datetime, timedelta

    page = request.args.get('page', 1, type=int)
    per_page = DEFAULT_PAGE_SIZE

    job_filter = request.args.get('job_name')
    status_filter = request.args.get('status')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    query = CronJobLog.query

    if job_filter:
        query = query.filter(CronJobLog.job_name == job_filter)
    if status_filter:
        query = query.filter(CronJobLog.status == status_filter)
    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(CronJobLog.created_at >= date_from_dt)
        except ValueError:
            pass
    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(CronJobLog.created_at < date_to_dt)
        except ValueError:
            pass

    logs = query.order_by(CronJobLog.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    unique_jobs = db.session.query(CronJobLog.job_name).distinct().all()
    unique_jobs = [j[0] for j in unique_jobs if j[0]]

    now = datetime.utcnow()
    last_24h = now - timedelta(days=1)
    last_week = now - timedelta(days=7)

    stats = {
        'total': CronJobLog.query.count(),
        'last_24h': CronJobLog.query.filter(CronJobLog.created_at >= last_24h).count(),
        'success': CronJobLog.query.filter(CronJobLog.status == 'success').count(),
        'failed': CronJobLog.query.filter(CronJobLog.status == 'failed').count(),
        'warning': CronJobLog.query.filter(CronJobLog.status == 'warning').count(),
        'running': CronJobLog.query.filter(CronJobLog.status == 'running').count()
    }

    job_stats = {}
    for job_name in unique_jobs:
        job_stats[job_name] = CronJobLog.get_job_stats(job_name, days=7)

    return render_template('admin/cron_job_logs.html',
                         logs=logs,
                         unique_jobs=unique_jobs,
                         stats=stats,
                         job_stats=job_stats,
                         job_filter=job_filter,
                         status_filter=status_filter,
                         date_from=date_from,
                         date_to=date_to)


@admin_bp.route('/cron-job-logs/<int:log_id>')
@login_required
def cron_job_log_detail(log_id):
    """Détail d'une exécution de cron job"""
    from models import CronJobLog

    log = CronJobLog.query.get_or_404(log_id)

    recent_logs = CronJobLog.query.filter(
        CronJobLog.job_name == log.job_name
    ).order_by(CronJobLog.created_at.desc()).limit(10).all()

    return render_template('admin/cron_job_log_detail.html', log=log, recent_logs=recent_logs)


@admin_bp.route('/seed-demo-data', methods=['POST'])
@login_required
def seed_demo_data_route():
    """Route protégée pour générer les données de démonstration dans l'Entreprise Demo"""
    from seed_demo_data import seed_demo_data
    try:
        success, message = seed_demo_data()
        if success:
            flash(f'Données de démonstration créées avec succès : {message}', 'success')
        else:
            flash(f'Erreur lors de la création des données : {message}', 'error')
    except Exception as e:
        from app import db
        db.session.rollback()
        current_app.logger.error(f"Erreur seed demo: {str(e)}")
        flash(f'Erreur inattendue : {str(e)}', 'error')
    return redirect(url_for('admin.dashboard'))
