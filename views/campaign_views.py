"""
Campaign Views Module
Routes pour la gestion des campagnes d'envoi de courriels en masse
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user
from datetime import datetime, date
from decimal import Decimal
import json
from concurrent.futures import ThreadPoolExecutor

from app import db, limiter
from models import (Campaign, CampaignEmail, CampaignStatus,
                    CampaignEmailStatus, Client, Invoice, ClientContact,
                    EmailTemplate, UserCompany, User, CommunicationNote)
from utils.audit_service import log_action, AuditActions, EntityTypes
from constants import DEFAULT_PAGE_SIZE

campaign_bp = Blueprint('campaign', __name__, url_prefix='/campaigns')

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="campaign_gen")


def resume_interrupted_campaigns():
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    RÉSILIENCE: Reprend les campagnes interrompues par un redémarrage du serveur.
    Appelée au démarrage de l'application.

    Gère deux types de campagnes interrompues:
    1. PROCESSING (génération) → Reprise automatique
    2. IN_PROGRESS (envoi) → Passage en STOPPED + notification à l'utilisateur

    IMPORTANT: Utilise get_campaign_target_clients pour respecter les filtres
    originaux de la campagne (collecteur, représentant, âge, parent/enfant).
    """
    from app import app
    from models import Company
    from notification_system import send_notification

    with app.app_context():
        try:
            # === GESTION DES ENVOIS INTERROMPUS (IN_PROGRESS) ===
            # Ces campagnes nécessitent une relance manuelle car le token OAuth
            # de l'utilisateur pourrait être expiré
            sending_campaigns = Campaign.query.filter_by(
                status=CampaignStatus.IN_PROGRESS).all()

            for campaign in sending_campaigns:
                # Compter les emails déjà envoyés vs restants
                sent_count = CampaignEmail.query.filter(
                    CampaignEmail.campaign_id == campaign.id,
                    CampaignEmail.status.in_([
                        CampaignEmailStatus.SENT,
                        CampaignEmailStatus.SENT_MANUALLY
                    ])).count()
                remaining_count = CampaignEmail.query.filter_by(
                    campaign_id=campaign.id,
                    status=CampaignEmailStatus.GENERATED).count()

                # Passer la campagne en STOPPED
                campaign.status = CampaignStatus.STOPPED
                db.session.commit()

                app.logger.warning(
                    f"ENVOI INTERROMPU: Campagne {campaign.id} ({campaign.name}) - "
                    f"{sent_count} envoyés, {remaining_count} restants. Notification envoyée."
                )

                # Envoyer une notification à l'utilisateur pour qu'il relance
                try:
                    send_notification(
                        user_id=campaign.created_by or 1,
                        company_id=campaign.company_id,
                        type='warning',
                        title='Envoi interrompu',
                        message=
                        f'La campagne "{campaign.name}" a été interrompue par un redémarrage du serveur. '
                        f'{sent_count} courriels envoyés, {remaining_count} restants. '
                        f'Cliquez sur "Reprendre l\'envoi" pour continuer.',
                        data={
                            'campaign_id': campaign.id,
                            'sent': sent_count,
                            'remaining': remaining_count
                        })
                except Exception as notif_error:
                    app.logger.error(
                        f"Erreur notification campagne {campaign.id}: {str(notif_error)}"
                    )

            # === GESTION DES GÉNÉRATIONS INTERROMPUES (PROCESSING) ===
            # Ces campagnes peuvent reprendre automatiquement
            stuck_campaigns = Campaign.query.filter_by(
                status=CampaignStatus.PROCESSING).all()

            for campaign in stuck_campaigns:
                # Calculer les clients déjà traités
                existing_client_ids = db.session.query(
                    CampaignEmail.client_id).filter(
                        CampaignEmail.campaign_id == campaign.id).all()
                existing_client_ids = {c[0] for c in existing_client_ids}

                # Récupérer la company pour utiliser get_campaign_target_clients
                company = Company.query.get(campaign.company_id)
                if not company:
                    app.logger.error(
                        f"REPRISE: Company {campaign.company_id} introuvable pour campagne {campaign.id}"
                    )
                    continue

                # CORRECTION: Utiliser get_campaign_target_clients pour respecter
                # tous les filtres originaux (collecteur, représentant, âge, parent/enfant)
                all_target_clients = get_campaign_target_clients(
                    company, campaign)

                # Filtrer pour ne garder que les clients non encore traités
                remaining_clients = [
                    client for client in all_target_clients
                    if client['id'] not in existing_client_ids
                ]

                if remaining_clients:
                    app.logger.info(
                        f"REPRISE: Campagne {campaign.id} ({campaign.name}) - "
                        f"{len(existing_client_ids)} déjà traités, {len(remaining_clients)} restants "
                        f"(sur {len(all_target_clients)} clients cibles)")
                    # Reprendre la génération pour les clients restants
                    start_campaign_generation(
                        campaign.id,
                        campaign.company_id,
                        campaign.created_by
                        or 1,  # Fallback si pas de créateur
                        remaining_clients)
                else:
                    # Tous les clients ont été traités, finaliser la campagne
                    total_emails = CampaignEmail.query.filter_by(
                        campaign_id=campaign.id).count()
                    campaign.status = CampaignStatus.READY
                    campaign.total_emails = total_emails
                    campaign.processing_completed_at = datetime.utcnow()
                    db.session.commit()
                    app.logger.info(
                        f"REPRISE: Campagne {campaign.id} ({campaign.name}) finalisée - {total_emails} emails"
                    )

        except Exception as e:
            app.logger.error(f"Erreur reprise campagnes: {str(e)}")


def can_access_campaigns(user, company):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    SÉCURITÉ: Vérifie si l'utilisateur peut accéder aux campagnes.
    Seuls les super_admin OU les utilisateurs avec can_create_campaigns=True
    (délégation explicite par super_admin) ont accès.

    Cette fonction est le point central de vérification de sécurité pour les campagnes.
    """
    if not user or not company:
        return False

    user_role = user.get_role_in_company(company.id)

    # Super admin a toujours accès
    if user_role == 'super_admin':
        return True

    # Vérifier la délégation explicite
    user_company = UserCompany.query.filter_by(user_id=user.id,
                                               company_id=company.id,
                                               is_active=True).first()

    return user_company and user_company.can_create_campaigns


def can_create_campaign(user, company):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Vérifie si l'utilisateur peut créer des campagnes (alias pour cohérence)"""
    return can_access_campaigns(user, company)


def can_view_campaigns(user, company):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    SÉCURITÉ: Vérifie si l'utilisateur peut voir les campagnes.
    Même restriction que can_access_campaigns - pas de visualisation sans autorisation.
    """
    return can_access_campaigns(user, company)


def get_valid_client_ids_for_campaign(client,
                                      campaign,
                                      company_id,
                                      preloaded_children=None):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    HELPER PARTAGÉ: Construit la liste des client_ids valides pour un email de campagne.

    Cette fonction est utilisée par:
    - generate_campaign_emails() : pour invoice_client_ids (avec preloaded_children)
    - validate_email_before_send() : pour vérifier la sécurité
    - generate_attachments_on_demand() : pour valider les factures

    Args:
        client: Client principal
        campaign: Campagne en cours
        company_id: ID de la company pour isolation
        preloaded_children: Liste optionnelle des enfants pré-chargés (évite requête DB)

    Retourne: set contenant client.id + IDs des enfants si applicable

    RÈGLE: Si include_children_in_parent_report est True, on inclut TOUS les enfants
    du client, pas seulement ceux avec factures. Ceci garantit la cohérence.
    """
    valid_ids = {client.id}

    if campaign.include_children_in_parent_report:
        if preloaded_children is not None:
            # Utiliser les enfants pré-chargés (évite requête DB dans boucle serrée)
            valid_ids.update(c.id if hasattr(c, 'id') else c
                             for c in preloaded_children)
        else:
            # Fallback: requête DB (pour validation/génération à la demande)
            children = Client.query.filter(
                Client.parent_client_id == client.id,
                Client.company_id == company_id).all()
            valid_ids.update(c.id for c in children)

    return valid_ids


def generate_attachments_on_demand(campaign_email, campaign, company):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    LAZY GENERATION: Génère les pièces jointes PDF/Excel à la demande.

    SÉCURITÉ TRIPLE VÉRIFICATION:
    1. Les factures sont chargées UNIQUEMENT depuis invoice_ids_snapshot (figé à la création)
    2. Chaque facture est vérifiée comme appartenant à valid_client_ids
    3. valid_client_ids est TOUJOURS reconstruit (cohérent avec validate_email_before_send)

    Retourne: dict avec 'pdf_data' et 'excel_data' (bytes ou None)
    """
    from utils import generate_statement_pdf_reportlab, prepare_logo_cache, cleanup_logo_cache

    result = {'pdf_data': None, 'excel_data': None}

    # Récupérer le snapshot des IDs de factures
    invoice_ids = campaign_email.get_invoice_ids_snapshot_list()
    if not invoice_ids:
        current_app.logger.warning(
            f"LAZY GEN: Aucun invoice_ids_snapshot pour email {campaign_email.id}"
        )
        return result

    # SÉCURITÉ: Utiliser le helper partagé pour garantir la cohérence
    client = campaign_email.client
    if not client:
        current_app.logger.error(
            f"SECURITE: Client introuvable pour email {campaign_email.id}")
        return result

    valid_client_ids = get_valid_client_ids_for_campaign(
        client, campaign, company.id)

    # Charger les factures UNIQUEMENT par leurs IDs figés
    invoices = Invoice.query.filter(Invoice.id.in_(invoice_ids)).order_by(
        Invoice.invoice_date.asc()).all()

    if not invoices:
        current_app.logger.warning(
            f"LAZY GEN: Aucune facture trouvée pour IDs {invoice_ids}")
        return result

    # SÉCURITÉ: Vérifier que CHAQUE facture appartient aux clients autorisés
    verified_invoices = []
    for inv in invoices:
        if inv.client_id not in valid_client_ids:
            current_app.logger.critical(
                f"SECURITE VIOLATION LAZY GEN: Facture {inv.id} client_id={inv.client_id} "
                f"non dans valid_client_ids={valid_client_ids} pour email {campaign_email.id}"
            )
            continue
        verified_invoices.append(inv)

    if not verified_invoices:
        current_app.logger.error(
            f"SECURITE: Toutes les factures rejetées pour email {campaign_email.id}"
        )
        return result

    # Enrichir les factures avec client_name et client_code pour PDF/Excel
    client = campaign_email.client
    is_multi_client = len(valid_client_ids) > 1

    for inv in verified_invoices:
        if not hasattr(inv, 'client_name') or inv.client_name is None:
            inv.client_name = inv.client.name if inv.client else client.name
        if not hasattr(inv, 'client_code') or inv.client_code is None:
            inv.client_code = inv.client.code_client if inv.client else client.code_client

    # Calculer aged_balances à partir des factures chargées
    def calculate_aged_balances_from_invoices(invoices_list,
                                              calculation_method='invoice_date'
                                              ):
        from datetime import date
        today = date.today()
        balances = {
            'current': 0,
            '30_days': 0,
            '60_days': 0,
            '90_days': 0,
            'over_90_days': 0
        }

        for invoice in invoices_list:
            if invoice.is_paid:
                continue
            if not invoice.amount:
                continue

            amount = float(invoice.amount)

            if not invoice.is_overdue():
                balances['current'] += amount
            else:
                calc_date = invoice.invoice_date if calculation_method == 'invoice_date' else invoice.due_date
                if calc_date:
                    days_old = (today - calc_date).days
                    if days_old <= 30:
                        balances['30_days'] += amount
                    elif days_old <= 60:
                        balances['60_days'] += amount
                    elif days_old <= 90:
                        balances['90_days'] += amount
                    else:
                        balances['over_90_days'] += amount
                else:
                    balances['over_90_days'] += amount

        return balances

    # Générer PDF si demandé par la campagne
    if campaign.attach_pdf_statement and verified_invoices:
        try:
            logo_cache = prepare_logo_cache(company)
            aged_balances = calculate_aged_balances_from_invoices(
                verified_invoices, company.aging_calculation_method)

            pdf_buffer = generate_statement_pdf_reportlab(
                client,
                verified_invoices,
                company,
                aged_balances,
                campaign.attachment_language,
                logo_cache=logo_cache)

            if pdf_buffer:
                result['pdf_data'] = pdf_buffer.getvalue()

            if logo_cache:
                cleanup_logo_cache(logo_cache)
        except Exception as e:
            current_app.logger.error(
                f"LAZY GEN: Erreur génération PDF pour email {campaign_email.id}: {str(e)}"
            )

    # Générer Excel si demandé par la campagne
    if campaign.attach_excel_statement and verified_invoices:
        try:
            excel_buffer = generate_excel_from_invoices(verified_invoices)
            if excel_buffer:
                result['excel_data'] = excel_buffer.getvalue()
        except Exception as e:
            current_app.logger.error(
                f"LAZY GEN: Erreur génération Excel pour email {campaign_email.id}: {str(e)}"
            )

    return result


@campaign_bp.route('/')
@login_required
def list_campaigns():
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Liste des campagnes"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not can_view_campaigns(current_user, company):
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    status_filter = request.args.get('status', 'all')

    query = Campaign.query.filter_by(company_id=company.id)

    if status_filter != 'all':
        try:
            status_enum = CampaignStatus(status_filter)
            query = query.filter_by(status=status_enum)
        except ValueError:
            pass

    campaigns = query.order_by(Campaign.created_at.desc()).all()

    user_role = current_user.get_role_in_company(company.id)
    can_create = can_create_campaign(current_user, company)

    return render_template('campaigns/list.html',
                           campaigns=campaigns,
                           company=company,
                           user_role=user_role,
                           can_create=can_create,
                           status_filter=status_filter,
                           CampaignStatus=CampaignStatus)


@campaign_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create_campaign():
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Créer une nouvelle campagne - Étape 1: Configuration de base"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if not can_create_campaign(current_user, company):
        flash(
            'Accès refusé. Vous n\'avez pas la permission de créer des campagnes.',
            'error')
        return redirect(url_for('campaign.list_campaigns'))

    collectors = User.query.join(UserCompany).filter(
        UserCompany.company_id == company.id, UserCompany.is_active == True,
        UserCompany.role.in_(['super_admin', 'admin', 'employe'])).all()

    representatives = db.session.query(Client.representative_name).filter(
        Client.company_id == company.id,
        Client.representative_name.isnot(None), Client.representative_name
        != '').distinct().all()
    representatives = [r[0] for r in representatives]

    templates = EmailTemplate.query.filter_by(company_id=company.id,
                                              is_active=True).order_by(
                                                  EmailTemplate.name).all()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Le nom de la campagne est obligatoire.', 'error')
            return render_template('campaigns/create.html',
                                   company=company,
                                   collectors=collectors,
                                   representatives=representatives,
                                   templates=templates)

        collector_value = request.form.get('filter_collector_id', '')
        is_unassigned = collector_value == 'unassigned'

        campaign = Campaign(
            company_id=company.id,
            created_by=current_user.id,
            name=name,
            status=CampaignStatus.DRAFT,
            filter_collector_id=None if is_unassigned or not collector_value else int(collector_value),
            filter_unassigned_collector=is_unassigned,
            filter_age_days=int(request.form.get('filter_age_days', 0)),
            filter_representative=request.form.get('filter_representative')
            or None,
            filter_contact_language=request.form.get('filter_contact_language')
            or None,
            filter_without_notes=request.form.get('filter_without_notes') == 'on',
            include_children_in_parent_report=request.form.get(
                'include_children') == 'on',
            recipient_type=request.form.get('recipient_type', 'primary'),
            attach_pdf_statement=request.form.get('attach_pdf') == 'on',
            attach_excel_statement=request.form.get('attach_excel') == 'on')

        db.session.add(campaign)
        db.session.commit()

        log_action(AuditActions.CAMPAIGN_CREATED, entity_type=EntityTypes.CAMPAIGN,
                  entity_id=campaign.id, entity_name=campaign.name)

        # Étape 2: Sélection des clients
        return redirect(
            url_for('campaign.preview_clients', campaign_id=campaign.id))

    return render_template('campaigns/create.html',
                           company=company,
                           collectors=collectors,
                           representatives=representatives,
                           templates=templates)


@campaign_bp.route('/<int:campaign_id>/email', methods=['GET', 'POST'])
@login_required
def configure_email(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Configurer le contenu de l'email - Étape 3 (après sélection des clients)"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'accès configure_email par user {current_user.id} sans permission"
        )
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        flash('Campagne non trouvée.', 'error')
        return redirect(url_for('campaign.list_campaigns'))

    if not campaign.can_be_edited():
        flash('Cette campagne ne peut plus être modifiée.', 'error')
        return redirect(
            url_for('campaign.view_campaign', campaign_id=campaign.id))

    # Vérifier que des clients ont été sélectionnés
    if not campaign.selected_client_ids:
        flash('Veuillez d\'abord sélectionner les clients.', 'warning')
        return redirect(
            url_for('campaign.preview_clients', campaign_id=campaign.id))

    selected_ids = json.loads(campaign.selected_client_ids)
    selected_count = len(selected_ids)

    templates = EmailTemplate.query.filter_by(company_id=company.id,
                                              is_active=True).order_by(
                                                  EmailTemplate.name).all()

    if request.method == 'POST':
        campaign.email_subject = request.form.get('email_subject', '').strip()
        campaign.email_content = request.form.get('email_content', '').strip()
        campaign.email_template_id = request.form.get('template_id') or None

        if not campaign.email_subject or not campaign.email_content:
            flash('Le sujet et le contenu du courriel sont obligatoires.',
                  'error')
            return render_template('campaigns/configure_email.html',
                                   campaign=campaign,
                                   company=company,
                                   templates=templates,
                                   selected_count=selected_count)

        # Récupérer les clients sélectionnés et lancer la génération
        all_clients = get_all_clients_with_balance(company)
        final_clients = [c for c in all_clients if c['id'] in selected_ids]

        campaign.total_emails = len(final_clients)
        db.session.commit()

        start_campaign_generation(campaign.id, company.id, current_user.id,
                                  final_clients)

        flash(
            'La génération des courriels a démarré. Vous recevrez une notification lorsque la campagne sera prête.',
            'info')
        return redirect(url_for('campaign.list_campaigns'))

    return render_template('campaigns/configure_email.html',
                           campaign=campaign,
                           company=company,
                           templates=templates,
                           selected_count=selected_count)


@campaign_bp.route('/<int:campaign_id>/clients', methods=['GET', 'POST'])
@login_required
def preview_clients(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Sélectionner les clients cibles - Étape 2"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'accès preview_clients par user {current_user.id} sans permission"
        )
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        flash('Campagne non trouvée.', 'error')
        return redirect(url_for('campaign.list_campaigns'))

    if not campaign.can_be_edited():
        flash('Cette campagne ne peut plus être modifiée.', 'error')
        return redirect(
            url_for('campaign.view_campaign', campaign_id=campaign.id))

    filtered_clients = get_campaign_target_clients(company, campaign)
    filtered_client_ids = {c['id'] for c in filtered_clients}

    added_ids = request.args.getlist('add', type=int)
    excluded_ids = request.args.getlist('exclude', type=int)

    # Charger les clients déjà sélectionnés si retour en arrière
    previously_selected = []
    if campaign.selected_client_ids:
        try:
            previously_selected = json.loads(campaign.selected_client_ids)
        except (json.JSONDecodeError, TypeError):
            previously_selected = []

    if request.method == 'POST':
        included_ids = request.form.getlist('include_clients', type=int)

        if not included_ids:
            flash('Aucun client sélectionné pour la campagne.', 'error')
            return render_template('campaigns/preview_clients.html',
                                   campaign=campaign,
                                   company=company,
                                   clients=filtered_clients,
                                   added_clients=[],
                                   excluded_ids=excluded_ids,
                                   previously_selected=previously_selected)

        # Sauvegarder les IDs des clients sélectionnés
        campaign.selected_client_ids = json.dumps(included_ids)
        db.session.commit()

        # Étape 3: Configuration du courriel
        return redirect(
            url_for('campaign.configure_email', campaign_id=campaign.id))

    # Charger les clients ajoutés manuellement
    added_clients = []
    if added_ids:
        for client_id in added_ids:
            if client_id not in filtered_client_ids:
                client = Client.query.filter_by(id=client_id,
                                                company_id=company.id).first()
                if client:
                    balance = db.session.query(
                        db.func.sum(Invoice.amount)).filter(
                            Invoice.client_id == client_id, Invoice.is_paid
                            == False).scalar() or 0

                    added_clients.append({
                        'id': client.id,
                        'code_client': client.code_client,
                        'name': client.name,
                        'email': client.email,
                        'balance': float(balance),
                        'manually_added': True
                    })

    # Si on revient en arrière, charger les clients ajoutés manuellement depuis la sélection
    if previously_selected:
        for client_id in previously_selected:
            if client_id not in filtered_client_ids and client_id not in [
                    c['id'] for c in added_clients
            ]:
                client = Client.query.filter_by(id=client_id,
                                                company_id=company.id).first()
                if client:
                    balance = db.session.query(
                        db.func.sum(Invoice.amount)).filter(
                            Invoice.client_id == client_id, Invoice.is_paid
                            == False).scalar() or 0

                    added_clients.append({
                        'id': client.id,
                        'code_client': client.code_client,
                        'name': client.name,
                        'email': client.email,
                        'balance': float(balance),
                        'manually_added': True
                    })

    return render_template('campaigns/preview_clients.html',
                           campaign=campaign,
                           company=company,
                           clients=filtered_clients,
                           added_clients=added_clients,
                           excluded_ids=excluded_ids,
                           previously_selected=previously_selected)


def get_campaign_target_clients(company, campaign):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Récupère les clients cibles pour une campagne selon les filtres.

    Logique de groupement parent/enfant :
    - Si 'include_children_in_parent_report' est coché :
      - Les enfants sont EXCLUS de la liste (ils seront dans le relevé du parent)
      - Les parents reçoivent un courriel si leur solde groupé (parent + enfants) > 0
      - Si tout le groupe est à 0, le groupe entier est exclu
    - Sinon :
      - Seuls les clients avec un solde > 0 sont inclus (comportement normal)
    """

    # Requête de base pour récupérer les clients avec leur solde
    # Note: is_parent est une propriété Python, pas une colonne - on utilise parent_client_id pour déduire
    base_query = db.session.query(
        Client.id, Client.code_client, Client.name, Client.email,
        Client.parent_client_id, Client.language,
        db.func.coalesce(db.func.sum(
            Invoice.amount), 0).label('total_balance')).outerjoin(
                Invoice,
                db.and_(Invoice.client_id == Client.id,
                        Invoice.is_paid == False)).filter(
                            Client.company_id == company.id).group_by(
                                Client.id)

    # Appliquer les filtres de collecteur et représentant
    if campaign.filter_unassigned_collector:
        base_query = base_query.filter(Client.collector_id.is_(None))
    elif campaign.filter_collector_id:
        base_query = base_query.filter(
            Client.collector_id == campaign.filter_collector_id)

    if campaign.filter_representative:
        base_query = base_query.filter(
            Client.representative_name == campaign.filter_representative)

    # Filtre "Sans note" : exclure les clients ayant des notes de communication
    if campaign.filter_without_notes:
        clients_with_notes_subq = db.session.query(CommunicationNote.client_id).filter(
            CommunicationNote.company_id == company.id
        ).distinct()
        base_query = base_query.filter(~Client.id.in_(clients_with_notes_subq))

    # Si PAS de groupement parent/enfant, appliquer le filtre solde > 0 directement
    # (comportement normal, comme avant)
    if not campaign.include_children_in_parent_report:
        base_query = base_query.having(db.func.sum(Invoice.amount) > 0)

    all_results = base_query.all()

    # Créer des maps pour accès rapide
    client_by_id = {r.id: r for r in all_results}
    balance_by_id = {
        r.id: float(r.total_balance) if r.total_balance else 0
        for r in all_results
    }

    # Identifier les enfants et calculer les soldes groupés
    child_client_ids = set()
    grouped_balance_by_parent = {}
    all_children = []

    if campaign.include_children_in_parent_report:
        # Récupérer les IDs des parents potentiels (clients sans parent dans les résultats)
        potential_parent_ids = [
            r.id for r in all_results if r.parent_client_id is None
        ]

        if potential_parent_ids:
            # Récupérer TOUS les enfants de ces parents (sans filtres collecteur/représentant)
            # pour calculer correctement le solde groupé
            all_children_query = db.session.query(
                Client.id, Client.parent_client_id,
                db.func.coalesce(db.func.sum(
                    Invoice.amount), 0).label('total_balance')).outerjoin(
                        Invoice,
                        db.and_(Invoice.client_id == Client.id,
                                Invoice.is_paid == False)).filter(
                                    Client.company_id == company.id,
                                    Client.parent_client_id.in_(
                                        potential_parent_ids)).group_by(
                                            Client.id)

            all_children = all_children_query.all()

            # Tous ces enfants doivent être exclus de la liste finale
            for child in all_children:
                child_client_ids.add(child.id)
                parent_id = child.parent_client_id
                child_balance = float(
                    child.total_balance) if child.total_balance else 0

                # Initialiser le solde groupé du parent avec son propre solde
                if parent_id not in grouped_balance_by_parent:
                    grouped_balance_by_parent[parent_id] = balance_by_id.get(
                        parent_id, 0)

                # Ajouter le solde de l'enfant
                grouped_balance_by_parent[parent_id] += child_balance

        # Pour les parents sans enfants, leur solde groupé = leur solde
        for result in all_results:
            if result.parent_client_id is None and result.id not in grouped_balance_by_parent:
                grouped_balance_by_parent[result.id] = balance_by_id.get(
                    result.id, 0)

    # Si filtre de langue, pré-calculer quels clients ont des contacts dans cette langue
    clients_with_lang_contacts = set()
    if campaign.filter_contact_language:
        contacts_query = ClientContact.query.filter(
            ClientContact.campaign_allowed == True,
            ClientContact.language == campaign.filter_contact_language,
            ClientContact.email.isnot(None), ClientContact.email
            != '').with_entities(ClientContact.client_id).distinct()
        clients_with_lang_contacts = {
            c.client_id
            for c in contacts_query.all()
        }

    # ========== OPTIMISATION: Précharger les dates les plus anciennes EN UNE SEULE REQUÊTE ==========
    oldest_invoice_by_client = {}
    children_by_parent = {}  # Pour éviter la boucle imbriquée

    # Construire le mapping enfants -> parent (inclut TOUS les enfants, pas seulement ceux dans all_results)
    if campaign.include_children_in_parent_report:
        # Utiliser les enfants déjà récupérés dans all_children (qui inclut tous les enfants)
        for child_id in child_client_ids:
            # Trouver le parent de cet enfant
            for child in all_children:
                if child.id == child_id:
                    parent_id = child.parent_client_id
                    if parent_id not in children_by_parent:
                        children_by_parent[parent_id] = []
                    children_by_parent[parent_id].append(child_id)
                    break

    if campaign.filter_age_days > 0:
        # Récupérer la date de facture la plus ancienne pour TOUS les clients + enfants en une requête
        all_client_ids = [r.id for r in all_results]
        # Ajouter aussi les IDs des enfants (qui peuvent être hors des filtres collecteur/représentant)
        all_client_ids.extend(child_client_ids)
        all_client_ids = list(set(all_client_ids))  # Éliminer les doublons

        oldest_dates_query = db.session.query(
            Invoice.client_id,
            db.func.min(Invoice.due_date).label('oldest_date')).filter(
                Invoice.client_id.in_(all_client_ids),
                Invoice.is_paid == False).group_by(Invoice.client_id).all()

        oldest_invoice_by_client = {
            r.client_id: r.oldest_date
            for r in oldest_dates_query
        }

    clients_data = []
    today = date.today()

    for result in all_results:
        # Exclure les enfants si l'option de groupement est activée
        if result.id in child_client_ids:
            continue

        # Déterminer le solde à utiliser (groupé ou individuel)
        if campaign.include_children_in_parent_report:
            effective_balance = grouped_balance_by_parent.get(
                result.id, balance_by_id.get(result.id, 0))
        else:
            effective_balance = balance_by_id.get(result.id, 0)

        # Exclure si le solde effectif est <= 0
        if effective_balance <= 0:
            continue

        # Filtre d'âge OPTIMISÉ (utilise données préchargées, ZÉRO requête SQL)
        if campaign.filter_age_days > 0:
            # Récupérer les IDs à vérifier (parent + enfants)
            client_ids_to_check = [result.id]
            if campaign.include_children_in_parent_report:
                client_ids_to_check.extend(
                    children_by_parent.get(result.id, []))

            # Trouver la date la plus ancienne parmi le parent et ses enfants
            oldest_date = None
            for cid in client_ids_to_check:
                inv_date = oldest_invoice_by_client.get(cid)
                if inv_date and (oldest_date is None
                                 or inv_date < oldest_date):
                    oldest_date = inv_date

            if oldest_date:
                days_old = (today - oldest_date).days
                if days_old < campaign.filter_age_days:
                    continue

        # Filtre de langue des contacts
        if campaign.filter_contact_language:
            has_matching_contact = result.id in clients_with_lang_contacts
            client_lang_matches = (result.language
                                   or 'fr') == campaign.filter_contact_language

            if campaign.recipient_type == 'primary':
                if not client_lang_matches:
                    continue
            elif campaign.recipient_type == 'campaign_contacts':
                if not has_matching_contact:
                    continue
            else:  # 'both'
                if not has_matching_contact and not client_lang_matches:
                    continue

        clients_data.append({
            'id': result.id,
            'code_client': result.code_client,
            'name': result.name,
            'email': result.email,
            'balance': effective_balance  # Utiliser le solde groupé
        })

    clients_data.sort(key=lambda x: x['balance'], reverse=True)

    return clients_data


def get_all_clients_with_balance(company):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Récupère tous les clients avec leur solde (pour validation finale)"""

    results = db.session.query(
        Client.id, Client.code_client, Client.name, Client.email,
        db.func.coalesce(db.func.sum(
            Invoice.amount), 0).label('total_balance')).outerjoin(
                Invoice,
                db.and_(Invoice.client_id == Client.id,
                        Invoice.is_paid == False)).filter(
                            Client.company_id == company.id).group_by(
                                Client.id).all()

    clients_data = []
    for result in results:
        clients_data.append({
            'id':
            result.id,
            'code_client':
            result.code_client,
            'name':
            result.name,
            'email':
            result.email,
            'balance':
            float(result.total_balance) if result.total_balance else 0
        })

    return clients_data


@campaign_bp.route('/<int:campaign_id>/search-clients')
@login_required
def search_clients_for_campaign(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Recherche de clients pour ajouter à la campagne"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'accès search-clients par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    search_term = request.args.get('q', '').strip()
    if len(search_term) < 2:
        return jsonify({'clients': []})

    clients = Client.query.filter(
        Client.company_id == company.id,
        db.or_(Client.name.ilike(f'%{search_term}%'),
               Client.code_client.ilike(f'%{search_term}%'))).limit(20).all()

    # Batch query pour les balances (evite N+1: 1 requete au lieu de 20)
    client_ids = [c.id for c in clients]
    balances = {}
    if client_ids:
        balance_rows = db.session.query(
            Invoice.client_id, db.func.sum(Invoice.amount)
        ).filter(
            Invoice.client_id.in_(client_ids),
            Invoice.is_paid == False
        ).group_by(Invoice.client_id).all()
        balances = {row[0]: float(row[1] or 0) for row in balance_rows}

    results = [{
        'id': client.id,
        'code_client': client.code_client,
        'name': client.name,
        'balance': balances.get(client.id, 0)
    } for client in clients]

    return jsonify({'clients': results})


def start_campaign_generation(campaign_id, company_id, user_id, clients_list):
    """
    Fonction vérifiée par MDF le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Démarre la génération asynchrone des courriels de campagne"""
    from app import app

    def run_generation():
        with app.app_context():
            try:
                generate_campaign_emails(campaign_id, company_id, user_id,
                                         clients_list)
            except Exception as e:
                current_app.logger.error(
                    f"Erreur génération campagne {campaign_id}: {str(e)}")
                campaign = Campaign.query.filter_by(id=campaign_id, company_id=company_id).first()
                if campaign:
                    campaign.status = CampaignStatus.DRAFT
                    db.session.commit()

                from notification_system import send_notification
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='error',
                    title='Erreur de campagne',
                    message=
                    f'Une erreur est survenue lors de la génération de la campagne "{campaign.name if campaign else ""}": {str(e)}'
                )

    executor.submit(run_generation)


def generate_campaign_emails(campaign_id, company_id, user_id, clients_list):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    Génère les courriels pour chaque client de la campagne.

    LAZY GENERATION v3:
    - Préchargement des données (clients, factures, contacts) - évite N+1 queries
    - PLUS de génération PDF/Excel à la création (gain massif ~15-20 min)
    - Stockage du snapshot invoice_ids pour génération à la demande
    - Création quasi-instantanée (<30 secondes pour 500+ clients)
    """
    campaign = Campaign.query.filter_by(id=campaign_id, company_id=company_id).first()
    if not campaign:
        current_app.logger.error(
            f"SECURITE: Campagne {campaign_id} introuvable ou company_id {company_id} incorrect")
        return

    campaign.status = CampaignStatus.PROCESSING
    campaign.processing_started_at = datetime.utcnow()
    db.session.commit()

    company = campaign.company
    lang_filter = campaign.filter_contact_language

    # ========== OPTIMISATION 1: Préchargement de toutes les données ==========
    client_ids = [c['id'] for c in clients_list]

    # Précharger tous les clients avec leurs relations
    clients = Client.query.filter(Client.id.in_(client_ids),
                                  Client.company_id == company_id).all()
    clients_map = {c.id: c for c in clients}

    # Précharger toutes les factures impayées pour ces clients
    all_invoices = Invoice.query.filter(Invoice.client_id.in_(client_ids),
                                        Invoice.is_paid == False).order_by(
                                            Invoice.client_id,
                                            Invoice.invoice_date.asc()).all()

    invoices_by_client = {}
    for inv in all_invoices:
        if inv.client_id not in invoices_by_client:
            invoices_by_client[inv.client_id] = []
        invoices_by_client[inv.client_id].append(inv)

    # Précharger les contacts campaign_allowed
    contacts_query = ClientContact.query.filter(
        ClientContact.client_id.in_(client_ids),
        ClientContact.campaign_allowed == True)
    if lang_filter:
        contacts_query = contacts_query.filter(
            ClientContact.language == lang_filter)
    all_contacts = contacts_query.all()

    contacts_by_client = {}
    for contact in all_contacts:
        if contact.client_id not in contacts_by_client:
            contacts_by_client[contact.client_id] = []
        contacts_by_client[contact.client_id].append(contact)

    # Précharger les enfants si nécessaire
    children_invoices_by_parent = {}
    children_by_parent = {
    }  # IMPORTANT: Toujours défini pour éviter NameError dans process_single_client
    if campaign.include_children_in_parent_report:
        parent_clients = [c for c in clients if c.is_parent]
        if parent_clients:
            # Récupérer tous les enfants
            parent_ids = [c.id for c in parent_clients]
            child_clients = Client.query.filter(
                Client.parent_client_id.in_(parent_ids),
                Client.company_id == company_id).all()
            child_ids = []
            for child in child_clients:
                if child.parent_client_id not in children_by_parent:
                    children_by_parent[child.parent_client_id] = []
                children_by_parent[child.parent_client_id].append(child)
                child_ids.append(child.id)

            # Récupérer les factures des enfants
            if child_ids:
                child_invoices = Invoice.query.filter(
                    Invoice.client_id.in_(child_ids),
                    Invoice.is_paid == False).order_by(
                        Invoice.invoice_date.asc()).all()

                child_inv_by_client = {}
                for inv in child_invoices:
                    if inv.client_id not in child_inv_by_client:
                        child_inv_by_client[inv.client_id] = []
                    child_inv_by_client[inv.client_id].append(inv)

                # Mapper les factures enfants vers le parent
                for parent_id, children in children_by_parent.items():
                    children_invoices_by_parent[parent_id] = []
                    for child in children:
                        children_invoices_by_parent[parent_id].extend(
                            child_inv_by_client.get(child.id, []))

    current_app.logger.info(
        f"Campagne {campaign_id}: Données préchargées - {len(clients)} clients, {len(all_invoices)} factures, {len(all_contacts)} contacts"
    )

    # ========== LAZY GENERATION: Fonction de génération métadonnées pour un client ==========
    def process_single_client(client_id):
        """Traite un client individuellement (thread-safe pour les données préchargées)"""
        try:
            client = clients_map.get(client_id)
            if not client:
                return None

            # Récupérer les destinataires depuis les données préchargées
            to_emails = []
            if campaign.recipient_type in ['primary', 'both']:
                if client.email:
                    if lang_filter:
                        client_lang = client.language or 'fr'
                        if client_lang == lang_filter:
                            to_emails.append(client.email)
                    else:
                        to_emails.append(client.email)

            if campaign.recipient_type in ['campaign_contacts', 'both']:
                for contact in contacts_by_client.get(client.id, []):
                    if contact.email and contact.email not in to_emails:
                        to_emails.append(contact.email)

            if not to_emails:
                return None

            # Récupérer les factures du client parent
            client_invoices = invoices_by_client.get(client.id, [])

            # Générer les factures pour ce client (inclure enfants si applicable)
            invoices_for_attachments = list(client_invoices)
            is_parent_with_children = campaign.include_children_in_parent_report and client.is_parent

            if is_parent_with_children:
                children_invs = children_invoices_by_parent.get(client.id, [])
                invoices_for_attachments.extend(children_invs)

            # Ajouter client_name et client_code à chaque facture pour PDF/Excel
            for inv in invoices_for_attachments:
                if not hasattr(inv, 'client_name') or inv.client_name is None:
                    inv.client_name = inv.client.name if inv.client else client.name
                if not hasattr(inv, 'client_code') or inv.client_code is None:
                    inv.client_code = inv.client.code_client if inv.client else client.code_client

            # Calculer le solde total APRÈS avoir ajouté les factures enfants
            total_outstanding = sum(
                float(inv.amount) for inv in invoices_for_attachments
                if inv.amount)

            # Remplacer les variables
            subject = replace_campaign_variables_optimized(
                campaign.email_subject, client, company, total_outstanding)
            content = replace_campaign_variables_optimized(
                campaign.email_content, client, company, total_outstanding)

            # UTILISER LE HELPER PARTAGÉ pour construire invoice_client_ids
            # Garantit la cohérence avec validate_email_before_send() et generate_attachments_on_demand()
            children_for_parent = children_by_parent.get(client.id, [])
            valid_client_ids_set = get_valid_client_ids_for_campaign(
                client,
                campaign,
                company_id,
                preloaded_children=children_for_parent)
            invoice_client_ids = list(valid_client_ids_set)

            # LAZY GENERATION: Snapshot des IDs de factures pour génération différée
            invoice_ids_snapshot = [inv.id for inv in invoices_for_attachments]

            # VALIDATION CRÉATION: Si pièces jointes requises mais aucune facture, skip ce client
            # Évite de créer des emails qui échoueront à la validation/génération
            requires_attachments = campaign.attach_pdf_statement or campaign.attach_excel_statement
            if requires_attachments and not invoice_ids_snapshot:
                current_app.logger.warning(
                    f"Campagne {campaign_id}: Client {client.id} ({client.name}) ignoré - "
                    f"pièces jointes requises mais aucune facture impayée")
                return None

            # Préparer les données du courriel (SANS PDF/Excel - génération différée)
            email_data = {
                'campaign_id': campaign_id,
                'client_id': client.id,
                'status': CampaignEmailStatus.GENERATED,
                'email_subject': subject,
                'email_content': content,
                'client_code': client.code_client,
                'client_name': client.name,
                'client_balance': Decimal(str(total_outstanding)),
                'to_emails': to_emails,
                'invoice_client_ids': invoice_client_ids,
                'invoice_ids_snapshot': invoice_ids_snapshot
            }

            return email_data

        except Exception as e:
            current_app.logger.error(
                f"Erreur traitement client {client_id}: {str(e)}")
            return None

    # ========== LAZY GENERATION: TRAITEMENT SÉQUENTIEL RAPIDE ==========
    # PLUS de génération PDF/Excel ici - gain massif de temps
    batch_size = 100
    total_processed = 0
    total_generated = 0

    for i in range(0, len(client_ids), batch_size):
        batch_ids = client_ids[i:i + batch_size]
        campaign_emails = []

        for client_id in batch_ids:
            try:
                result = process_single_client(client_id)
                if result:
                    campaign_email = CampaignEmail(
                        campaign_id=result['campaign_id'],
                        client_id=result['client_id'],
                        status=result['status'],
                        email_subject=result['email_subject'],
                        email_content=result['email_content'],
                        client_code=result['client_code'],
                        client_name=result['client_name'],
                        client_balance=result['client_balance'])
                    campaign_email.set_to_emails_list(result['to_emails'])
                    campaign_email.set_invoice_client_ids_list(
                        result.get('invoice_client_ids', []))
                    campaign_email.set_invoice_ids_snapshot_list(
                        result.get('invoice_ids_snapshot', []))

                    # LAZY GENERATION: PDF/Excel seront générés à la demande (preview ou envoi)
                    # pdf_attachment_data et excel_attachment_data restent NULL

                    campaign_emails.append(campaign_email)
                    total_generated += 1
            except Exception as e:
                current_app.logger.error(
                    f"Erreur traitement client {client_id}: {str(e)}")

        # Sauvegarder le lot
        if campaign_emails:
            db.session.bulk_save_objects(campaign_emails)
            db.session.commit()

        total_processed += len(batch_ids)
        current_app.logger.info(
            f"Campagne {campaign_id}: {total_processed}/{len(client_ids)} clients traités, {total_generated} générés"
        )

    # LAZY GENERATION: Plus de cache logo à nettoyer - PDF générés à la demande

    # Finaliser la campagne
    # IMPORTANT: Compter le TOTAL réel d'emails dans la DB (pas seulement ce batch)
    # car en cas de reprise après redémarrage, total_generated ne contient que les emails de cette session
    actual_total_emails = CampaignEmail.query.filter_by(
        campaign_id=campaign_id).count()

    campaign.status = CampaignStatus.READY
    campaign.processing_completed_at = datetime.utcnow()
    campaign.total_emails = actual_total_emails
    db.session.commit()

    from notification_system import send_notification
    send_notification(
        user_id=user_id,
        company_id=company_id,
        type='success',
        title='Campagne prête',
        message=
        f'La campagne "{campaign.name}" est prête. {campaign.total_emails} courriels ont été générés.',
        data={'campaign_id': campaign_id})


def replace_campaign_variables_optimized(text, client, company,
                                         total_outstanding):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Version optimisée de replace_campaign_variables qui accepte le total précalculé"""
    if not text:
        return text

    from utils import format_currency
    variables = {
        '{client_name}':
        client.name or '',
        '{client_code}':
        client.code_client or '',
        '{client_email}':
        client.email or '',
        '{client_phone}':
        client.phone or '',
        '{client_payment_terms}':
        client.payment_terms or '',
        '{client_total_outstanding}':
        format_currency(total_outstanding, company.currency or 'CAD'),
        '{company_name}':
        company.name or '',
        '{company_email}':
        company.email or '',
        '{today_date}':
        datetime.now().strftime('%d/%m/%Y')
    }

    for var, value in variables.items():
        text = text.replace(var, str(value))

    return text


def generate_excel_from_invoices(invoices):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Génère un Excel directement depuis une liste de factures (sans requête DB)"""
    import xlsxwriter
    import io

    if not invoices:
        return None

    # Vérifier si des factures ont un montant original
    has_original_amount = any(
        hasattr(inv, 'original_amount') and inv.original_amount is not None
        for inv in invoices)

    # Vérifier si c'est un relevé parent+enfants (factures avec client_code)
    is_multi_client = any(
        hasattr(inv, 'client_code') and inv.client_code is not None
        for inv in invoices)

    excel_buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(excel_buffer)
    worksheet = workbook.add_worksheet('Relevé')

    header_format = workbook.add_format({
        'bold': True,
        'bg_color': '#4472C4',
        'font_color': 'white',
        'border': 1
    })

    # Get company currency for Excel formatting
    company_currency = company.currency if hasattr(company, 'currency') and company.currency else 'CAD'
    EXCEL_CURRENCY_FORMATS = {
        'CAD': '# ##0,00 $',
        'USD': '$#,##0.00',
        'EUR': '# ##0,00 €',
        'GBP': '£#,##0.00',
        'CHF': '# ##0,00 CHF'
    }
    currency_num_format = EXCEL_CURRENCY_FORMATS.get(company_currency, '# ##0,00 $')

    money_format = workbook.add_format({'num_format': currency_num_format})
    date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})

    # En-têtes dynamiques selon la présence de code client et montant original
    headers = []
    if is_multi_client:
        headers.append('Code client')
    headers.extend(['No Facture', 'Date', 'Échéance'])
    if has_original_amount:
        headers.append('Montant Original')
    headers.extend(['Montant', 'Solde'])

    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)

    row = 1
    total = 0
    for inv in invoices:
        col = 0

        # Ajouter Code client si relevé multi-client
        if is_multi_client:
            client_code = getattr(inv, 'client_code', '') or ''
            worksheet.write(row, col, client_code)
            col += 1

        worksheet.write(row, col, inv.invoice_number or '')
        col += 1
        worksheet.write(
            row, col,
            inv.invoice_date.strftime('%Y-%m-%d') if inv.invoice_date else '',
            date_format)
        col += 1
        worksheet.write(
            row, col,
            inv.due_date.strftime('%Y-%m-%d') if inv.due_date else '',
            date_format)
        col += 1

        # Ajouter Montant Original si disponible
        if has_original_amount:
            original_amt = float(
                inv.original_amount) if inv.original_amount is not None else ''
            worksheet.write(row, col, original_amt,
                            money_format if original_amt != '' else None)
            col += 1

        worksheet.write(row, col,
                        float(inv.amount) if inv.amount else 0, money_format)
        col += 1
        worksheet.write(row, col,
                        float(inv.amount) if inv.amount else 0, money_format)
        total += float(inv.amount) if inv.amount else 0
        row += 1

    total_format = workbook.add_format({
        'bold': True,
        'num_format': currency_num_format
    })

    # Calculer la position du total selon les colonnes
    total_col = len(headers) - 2  # Avant-dernière colonne (Montant)
    worksheet.write(row, total_col, 'Total:', header_format)
    worksheet.write(row, total_col + 1, total, total_format)

    # Ajuster les largeurs de colonnes
    col_idx = 0
    if is_multi_client:
        worksheet.set_column(col_idx, col_idx, 12)  # Code client
        col_idx += 1
    worksheet.set_column(col_idx, col_idx, 15)  # No Facture
    col_idx += 1
    worksheet.set_column(col_idx, col_idx + 1, 12)  # Date, Échéance
    col_idx += 2
    if has_original_amount:
        worksheet.set_column(col_idx, col_idx, 15)  # Montant Original
        col_idx += 1
    worksheet.set_column(col_idx, col_idx + 1, 15)  # Montant, Solde

    workbook.close()
    excel_buffer.seek(0)

    return excel_buffer


def get_recipient_emails(client, campaign):
    """
    Fonction vérifiée par MDF le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Récupère les emails destinataires selon le type de destinataire configuré et le filtre de langue"""
    emails = []

    # Filtre de langue optionnel
    lang_filter = campaign.filter_contact_language

    if campaign.recipient_type in ['primary', 'both']:
        if client.email:
            # Si filtre de langue, vérifier la langue du client
            if lang_filter:
                client_lang = client.language or 'fr'
                if client_lang == lang_filter:
                    emails.append(client.email)
            else:
                emails.append(client.email)

    if campaign.recipient_type in ['campaign_contacts', 'both']:
        # Construire la requête des contacts avec filtre de langue si applicable
        contacts_query = ClientContact.query.filter_by(client_id=client.id,
                                                       campaign_allowed=True)

        if lang_filter:
            contacts_query = contacts_query.filter(
                ClientContact.language == lang_filter)

        campaign_contacts = contacts_query.all()
        for contact in campaign_contacts:
            if contact.email and contact.email not in emails:
                emails.append(contact.email)

    return emails


@campaign_bp.route('/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    """
    Fonction vérifiée par MDF le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Voir les détails d'une campagne"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'accès view_campaign par user {current_user.id} sans permission"
        )
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        flash('Campagne non trouvée.', 'error')
        return redirect(url_for('campaign.list_campaigns'))

    page = request.args.get('page', 1, type=int)
    per_page = DEFAULT_PAGE_SIZE

    status_filter = request.args.get('email_status', 'all')

    query = CampaignEmail.query.filter_by(campaign_id=campaign_id)

    if status_filter != 'all':
        try:
            status_enum = CampaignEmailStatus(status_filter)
            query = query.filter_by(status=status_enum)
        except ValueError:
            pass

    emails = query.order_by(CampaignEmail.client_name).paginate(
        page=page, per_page=per_page, error_out=False)

    user_role = current_user.get_role_in_company(company.id)

    return render_template('campaigns/view.html',
                           campaign=campaign,
                           emails=emails,
                           company=company,
                           user_role=user_role,
                           status_filter=status_filter,
                           CampaignEmailStatus=CampaignEmailStatus)


@campaign_bp.route('/<int:campaign_id>/email/<int:email_id>')
@login_required
def preview_email(campaign_id, email_id):
    """
    Fonction vérifiée par MDF le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Prévisualiser un courriel de campagne"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'accès preview_email par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    campaign_email = CampaignEmail.query.filter_by(
        id=email_id, campaign_id=campaign_id).first()
    if not campaign_email:
        return jsonify({'error': 'Courriel non trouvé'}), 404

    # LAZY GENERATION: has_pdf/has_excel indique si un PDF/Excel SERA disponible
    # (basé sur config campagne), pas nécessairement déjà généré
    has_pdf = campaign.attach_pdf_statement and bool(
        campaign_email.get_invoice_ids_snapshot_list())
    has_excel = campaign.attach_excel_statement and bool(
        campaign_email.get_invoice_ids_snapshot_list())

    return jsonify({
        'id':
        campaign_email.id,
        'client_code':
        campaign_email.client_code,
        'client_name':
        campaign_email.client_name,
        'to_emails':
        campaign_email.get_to_emails_list(),
        'subject':
        campaign_email.email_subject,
        'content':
        campaign_email.email_content,
        'balance':
        float(campaign_email.client_balance)
        if campaign_email.client_balance else 0,
        'has_pdf':
        has_pdf,
        'has_excel':
        has_excel,
        'status':
        campaign_email.status.value,
        'status_display':
        campaign_email.status_display
    })


@campaign_bp.route(
    '/<int:campaign_id>/email/<int:email_id>/attachment/<attachment_type>')
@login_required
def download_attachment(campaign_id, email_id, attachment_type):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    Télécharger une pièce jointe de courriel de campagne.

    LAZY GENERATION: Si le binaire n'existe pas encore, le générer à la volée
    à partir du snapshot invoice_ids_snapshot et le sauvegarder en cache.
    """
    from flask import make_response

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'accès download_attachment par user {current_user.id} sans permission"
        )
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        flash('Campagne non trouvée.', 'error')
        return redirect(url_for('campaign.list_campaigns'))

    campaign_email = CampaignEmail.query.filter_by(
        id=email_id, campaign_id=campaign_id).first()
    if not campaign_email:
        flash('Courriel non trouvé.', 'error')
        return redirect(
            url_for('campaign.view_campaign', campaign_id=campaign_id))

    if attachment_type == 'pdf':
        # LAZY GENERATION: Générer à la volée si absent
        if not campaign_email.pdf_attachment_data:
            if not campaign.attach_pdf_statement:
                flash('Cette campagne n\'inclut pas de PDF.', 'error')
                return redirect(
                    url_for('campaign.view_campaign', campaign_id=campaign_id))

            current_app.logger.info(
                f"LAZY GEN: Génération PDF à la demande pour email {email_id}")
            attachments = generate_attachments_on_demand(
                campaign_email, campaign, company)

            if attachments['pdf_data']:
                campaign_email.pdf_attachment_data = attachments['pdf_data']
                db.session.commit()
                current_app.logger.info(
                    f"LAZY GEN: PDF sauvegardé en cache pour email {email_id}")
            else:
                flash('Impossible de générer le PDF.', 'error')
                return redirect(
                    url_for('campaign.view_campaign', campaign_id=campaign_id))

        response = make_response(campaign_email.pdf_attachment_data)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers[
            'Content-Disposition'] = f'inline; filename=releve_{campaign_email.client_code}.pdf'
        return response

    elif attachment_type == 'excel':
        # LAZY GENERATION: Générer à la volée si absent
        if not campaign_email.excel_attachment_data:
            if not campaign.attach_excel_statement:
                flash('Cette campagne n\'inclut pas de Excel.', 'error')
                return redirect(
                    url_for('campaign.view_campaign', campaign_id=campaign_id))

            current_app.logger.info(
                f"LAZY GEN: Génération Excel à la demande pour email {email_id}"
            )
            attachments = generate_attachments_on_demand(
                campaign_email, campaign, company)

            if attachments['excel_data']:
                campaign_email.excel_attachment_data = attachments[
                    'excel_data']
                db.session.commit()
                current_app.logger.info(
                    f"LAZY GEN: Excel sauvegardé en cache pour email {email_id}"
                )
            else:
                flash('Impossible de générer le Excel.', 'error')
                return redirect(
                    url_for('campaign.view_campaign', campaign_id=campaign_id))

        response = make_response(campaign_email.excel_attachment_data)
        response.headers[
            'Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers[
            'Content-Disposition'] = f'attachment; filename=releve_{campaign_email.client_code}.xlsx'
        return response

    flash('Type de pièce jointe invalide.', 'error')
    return redirect(url_for('campaign.view_campaign', campaign_id=campaign_id))


@campaign_bp.route('/<int:campaign_id>/send', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def send_emails(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Envoyer des courriels sélectionnés ou toute la campagne"""
    from models import EmailConfiguration
    from email_fallback import refresh_user_oauth_token

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative d'envoi campagne par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    if not campaign.can_be_sent():
        return jsonify({'error':
                        'Cette campagne ne peut pas être envoyée'}), 400

    # VÉRIFICATION DU TOKEN EMAIL AVANT L'ENVOI
    user_email_config = EmailConfiguration.query.filter_by(
        user_id=current_user.id, company_id=company.id).first()

    if not user_email_config or not user_email_config.is_outlook_connected():
        return jsonify({
            'error':
            'Configuration email manquante',
            'needs_reconnect':
            True,
            'message':
            'Veuillez connecter votre compte Outlook dans les paramètres avant d\'envoyer une campagne.'
        }), 400

    # Tester si le token est valide en essayant de le rafraîchir
    if user_email_config.needs_token_refresh():
        try:
            refresh_user_oauth_token(user_email_config)
        except Exception as e:
            current_app.logger.warning(
                f"Token refresh failed for user {current_user.id}: {str(e)}")
            return jsonify({
                'error':
                'Token expiré',
                'needs_reconnect':
                True,
                'message':
                'Votre connexion Outlook a expiré. Veuillez vous reconnecter dans les paramètres de l\'entreprise.'
            }), 400

    # Vérifier que le token fonctionne vraiment avec un appel test
    try:
        from microsoft_oauth import MicrosoftOAuthConnector
        connector = MicrosoftOAuthConnector()
        access_token = user_email_config.outlook_oauth_access_token
        if not access_token or not connector.test_connection(access_token):
            return jsonify({
                'error':
                'Connexion email invalide',
                'needs_reconnect':
                True,
                'message':
                'La connexion à votre compte Outlook ne fonctionne plus. Veuillez vous reconnecter dans les paramètres.'
            }), 400
    except Exception as e:
        current_app.logger.warning(
            f"Token test failed for user {current_user.id}: {str(e)}")
        return jsonify({
            'error':
            'Connexion email invalide',
            'needs_reconnect':
            True,
            'message':
            'Impossible de vérifier votre connexion Outlook. Veuillez vous reconnecter dans les paramètres.'
        }), 400

    data = request.json or {}
    email_ids = data.get('email_ids', [])
    send_all = data.get('send_all', False)

    if send_all:
        emails_to_send = CampaignEmail.query.filter_by(
            campaign_id=campaign_id,
            status=CampaignEmailStatus.GENERATED).all()
    else:
        emails_to_send = CampaignEmail.query.filter(
            CampaignEmail.id.in_(email_ids),
            CampaignEmail.campaign_id == campaign_id,
            CampaignEmail.status == CampaignEmailStatus.GENERATED).all()

    if not emails_to_send:
        return jsonify({'error': 'Aucun courriel à envoyer'}), 400

    # Envoi manuel = tout ce qui n'est pas "Envoyer tous" (sélection individuelle ou multiple)
    # Seul "send_all" met la campagne en IN_PROGRESS avec bouton d'arrêt d'urgence
    is_manual = not send_all

    start_campaign_sending(campaign.id,
                           company.id,
                           current_user.id, [e.id for e in emails_to_send],
                           is_manual=is_manual)

    return jsonify({
        'success': True,
        'message': f'Envoi de {len(emails_to_send)} courriels démarré',
        'count': len(emails_to_send)
    })


def start_campaign_sending(campaign_id,
                           company_id,
                           user_id,
                           email_ids,
                           is_manual=False):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Démarre l'envoi asynchrone des courriels de campagne"""
    from app import app

    def run_sending():
        with app.app_context():
            try:
                send_campaign_emails(campaign_id,
                                     company_id,
                                     user_id,
                                     email_ids,
                                     is_manual=is_manual)
            except Exception as e:
                current_app.logger.error(
                    f"Erreur envoi campagne {campaign_id}: {str(e)}")

                from notification_system import send_notification
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='error',
                    title='Erreur d\'envoi',
                    message=
                    f'Une erreur est survenue lors de l\'envoi de la campagne: {str(e)}'
                )

    executor.submit(run_sending)


def validate_email_before_send(campaign_email, campaign, company_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    SÉCURITÉ: Valide un email avant envoi - 3 niveaux de vérification
    Retourne (True, None) si valide, (False, error_message) sinon

    LAZY GENERATION: Vérifie aussi invoice_ids_snapshot même si pièces jointes pas encore générées
    """

    # Niveau 1: Vérifier que le client appartient à la company
    client = Client.query.filter_by(id=campaign_email.client_id, company_id=company_id).first()
    if not client:
        return False, f"Client {campaign_email.client_id} n'appartient pas à la company {company_id}"

    # Utiliser le helper partagé pour garantir la cohérence avec generate_attachments_on_demand
    valid_client_ids = get_valid_client_ids_for_campaign(
        client, campaign, company_id)

    # Niveau 2: Vérifier invoice_client_ids (clients dont les factures sont incluses)
    # LAZY GENERATION: La validation principale se fait via invoice_ids_snapshot
    has_attachments = campaign_email.pdf_attachment_data or campaign_email.excel_attachment_data
    will_have_attachments = campaign.attach_pdf_statement or campaign.attach_excel_statement

    if has_attachments or will_have_attachments:
        # Validation optionnelle de invoice_client_ids (peut être vide pour rétrocompatibilité)
        invoice_client_ids = campaign_email.get_invoice_client_ids_list()
        if invoice_client_ids:
            for inv_client_id in invoice_client_ids:
                if inv_client_id not in valid_client_ids:
                    return False, f"Facture de client {inv_client_id} non autorisé pour client {client.id}"

        # VALIDATION PRINCIPALE: Vérifier cohérence du snapshot invoice_ids
        # C'est la validation la plus fiable car elle vérifie les vraies factures
        invoice_ids_snapshot = campaign_email.get_invoice_ids_snapshot_list()
        if invoice_ids_snapshot:
            snapshot_invoices = Invoice.query.filter(
                Invoice.id.in_(invoice_ids_snapshot)).all()
            for inv in snapshot_invoices:
                if inv.client_id not in valid_client_ids:
                    return False, f"Snapshot facture {inv.id} client_id={inv.client_id} non autorisé"
        elif will_have_attachments:
            # Snapshot vide + pièces jointes attendues = erreur
            # La création aurait dû rejeter ce client (voir process_single_client)
            # Si on arrive ici, c'est une incohérence de données
            current_app.logger.error(
                f"VALIDATION FAIL: Email {campaign_email.id} client {campaign_email.client_id} - "
                f"Pièces jointes requises mais snapshot vide (incohérence données)"
            )
            return False, "Pièces jointes requises mais aucune facture dans le snapshot"

    # Niveau 3: Vérifier que les destinataires appartiennent au client ou contacts autorisés
    to_emails = campaign_email.get_to_emails_list()
    if to_emails:
        valid_emails = set()

        # Email principal du client
        if client.email:
            valid_emails.add(client.email.lower())

        # Ajouter les emails des enfants (valid_client_ids déjà construit au niveau 1)
        if campaign.include_children_in_parent_report:
            for child_id in valid_client_ids:
                if child_id != client.id:
                    child = Client.query.filter_by(id=child_id, company_id=company_id).first()
                    if child and child.email:
                        valid_emails.add(child.email.lower())

        # Contacts autorisés pour campagne
        contacts = ClientContact.query.filter(
            ClientContact.client_id.in_(valid_client_ids),
            ClientContact.campaign_allowed == True,
            ClientContact.email.isnot(None)).all()
        for contact in contacts:
            valid_emails.add(contact.email.lower())

        for email in to_emails:
            if email.lower() not in valid_emails:
                return False, f"Destinataire {email} non autorisé pour client {client.id}"

    return True, None


def send_campaign_emails(campaign_id,
                         company_id,
                         user_id,
                         email_ids,
                         is_manual=False):
    """
    Fonction vérifiée par MDF le 23/12/2025 et le 30/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Envoie les courriels de campagne avec isolation stricte par company_id et client_id"""
    from models import EmailConfiguration
    from microsoft_oauth import MicrosoftOAuthConnector
    from email_fallback import refresh_user_oauth_token

    campaign = Campaign.query.filter_by(id=campaign_id, company_id=company_id).first()
    if not campaign:
        current_app.logger.error(
            f"SECURITE: Campagne {campaign_id} introuvable ou company_id {company_id} incorrect")
        return

    # IMPORTANT: Ne pas changer le statut à IN_PROGRESS pour les envois manuels (individuels)
    # Le bouton "Arrêt d'urgence" n'a pas de sens pour un seul email
    if not is_manual and campaign.status in (CampaignStatus.READY,
                                             CampaignStatus.STOPPED):
        campaign.status = CampaignStatus.IN_PROGRESS
        campaign.sending_started_at = datetime.utcnow()
        # Réinitialiser le flag d'arrêt pour permettre la reprise
        campaign.stop_requested = False
        campaign.stop_requested_at = None
        campaign.stop_requested_by = None
        db.session.commit()

        log_action(AuditActions.CAMPAIGN_STARTED, entity_type=EntityTypes.CAMPAIGN,
                  entity_id=campaign.id, entity_name=campaign.name)

    user = User.query.get(user_id)
    company = campaign.company

    user_email_config = EmailConfiguration.query.filter_by(
        user_id=user_id, company_id=company_id).first()

    if not user_email_config or not user_email_config.is_outlook_connected():
        from notification_system import send_notification
        send_notification(
            user_id=user_id,
            company_id=company_id,
            type='error',
            title='Configuration email manquante',
            message=
            'Veuillez configurer votre connexion Outlook avant d\'envoyer une campagne.'
        )
        return

    if user_email_config.needs_token_refresh():
        try:
            refresh_user_oauth_token(user_email_config)
        except Exception as e:
            current_app.logger.error(f"Erreur refresh token: {str(e)}")
            from notification_system import send_notification
            send_notification(
                user_id=user_id,
                company_id=company_id,
                type='error',
                title='Erreur de token',
                message=
                'Impossible de rafraîchir le token Outlook. Veuillez vous reconnecter.'
            )
            return

    ms_connector = MicrosoftOAuthConnector()

    sent_count = 0
    failed_count = 0
    stopped_by_user = False

    for email_id in email_ids:
        # ARRÊT D'URGENCE: Recharger la campagne depuis la DB pour voir les modifications
        # (nécessaire car le flag peut être modifié par un autre thread/requête)
        db.session.expire(campaign)
        campaign = Campaign.query.filter_by(id=campaign_id,
                                            company_id=company_id).first()
        if not campaign:
            current_app.logger.error(
                f"SECURITE: Campagne {campaign_id} introuvable lors du refresh"
            )
            break

        if campaign.stop_requested:
            current_app.logger.warning(
                f"ARRÊT D'URGENCE: Campagne {campaign_id} arrêt détecté, "
                f"arrêt après {sent_count} envois, {failed_count} échecs")
            stopped_by_user = True
            break

        campaign_email = CampaignEmail.query.filter_by(id=email_id, campaign_id=campaign_id).first()

        # SECURITE CRITIQUE: Vérification multi-niveaux
        if not campaign_email:
            current_app.logger.warning(
                f"SECURITE: CampaignEmail {email_id} introuvable ou n'appartient pas a campagne {campaign_id}")
            continue

        # SECURITE CRITIQUE: Vérifier que le client appartient à la company
        client = Client.query.filter_by(id=campaign_email.client_id, company_id=company_id).first()
        if not client:
            current_app.logger.error(
                f"SECURITE VIOLATION: Client {campaign_email.client_id} n'appartient pas à company {company_id}"
            )
            campaign_email.status = CampaignEmailStatus.FAILED
            campaign_email.error_message = "Erreur de sécurité: client invalide"
            db.session.commit()
            failed_count += 1
            continue

        # SECURITE: Vérifier cohérence des données client stockées vs réelles
        if campaign_email.client_code != client.code_client:
            current_app.logger.error(
                f"SECURITE VIOLATION: Code client stocké ({campaign_email.client_code}) différent du réel ({client.code_client})"
            )
            campaign_email.status = CampaignEmailStatus.FAILED
            campaign_email.error_message = "Erreur de sécurité: incohérence données client"
            db.session.commit()
            failed_count += 1
            continue

        # SECURITE NIVEAU 2-3: Validation complète avant envoi (pièces jointes + destinataires)
        is_valid, validation_error = validate_email_before_send(
            campaign_email, campaign, company_id)
        if not is_valid:
            current_app.logger.critical(
                f"SECURITE VALIDATION ECHOUEE: {validation_error} - Email {email_id}"
            )
            campaign_email.status = CampaignEmailStatus.FAILED
            campaign_email.error_message = f"Validation sécurité: {validation_error}"
            db.session.commit()
            failed_count += 1
            continue

        if campaign_email.status != CampaignEmailStatus.GENERATED:
            continue

        campaign_email.status = CampaignEmailStatus.SENDING
        db.session.commit()

        try:
            to_emails = campaign_email.get_to_emails_list()
            if not to_emails:
                campaign_email.status = CampaignEmailStatus.SKIPPED
                campaign_email.error_message = "Aucun destinataire"
                db.session.commit()
                continue

            # LAZY GENERATION: Générer les pièces jointes si absentes
            needs_pdf = campaign.attach_pdf_statement and not campaign_email.pdf_attachment_data
            needs_excel = campaign.attach_excel_statement and not campaign_email.excel_attachment_data

            if needs_pdf or needs_excel:
                current_app.logger.info(
                    f"LAZY GEN ENVOI: Génération pièces jointes pour email {email_id}"
                )
                generated_attachments = generate_attachments_on_demand(
                    campaign_email, campaign, company)

                # Vérifier que la génération a réussi
                gen_pdf_ok = not needs_pdf or generated_attachments['pdf_data']
                gen_excel_ok = not needs_excel or generated_attachments[
                    'excel_data']

                if not (gen_pdf_ok and gen_excel_ok):
                    current_app.logger.error(
                        f"LAZY GEN ECHEC: email {email_id} - PDF:{bool(generated_attachments['pdf_data'])} Excel:{bool(generated_attachments['excel_data'])}"
                    )
                    campaign_email.status = CampaignEmailStatus.FAILED
                    campaign_email.error_message = "Échec génération pièces jointes"
                    db.session.commit()
                    failed_count += 1
                    continue

                if needs_pdf and generated_attachments['pdf_data']:
                    campaign_email.pdf_attachment_data = generated_attachments[
                        'pdf_data']
                if needs_excel and generated_attachments['excel_data']:
                    campaign_email.excel_attachment_data = generated_attachments[
                        'excel_data']

                db.session.commit()

            attachments = []

            if campaign_email.pdf_attachment_data:
                attachments.append({
                    'filename': f'releve_{campaign_email.client_code}.pdf',
                    'content': campaign_email.pdf_attachment_data,
                    'content_type': 'application/pdf'
                })

            if campaign_email.excel_attachment_data:
                attachments.append({
                    'filename':
                    f'releve_{campaign_email.client_code}.xlsx',
                    'content':
                    campaign_email.excel_attachment_data,
                    'content_type':
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                })

            # Ajouter la signature de l'utilisateur si configurée
            email_body = campaign_email.email_content
            if user_email_config.email_signature:
                email_body = email_body + "\n\n" + user_email_config.email_signature

            # Envoyer avec return_message_ids pour récupérer les IDs pour la note
            send_result = ms_connector.send_email(
                access_token=user_email_config.outlook_oauth_access_token,
                to_emails=to_emails,
                subject=campaign_email.email_subject,
                body=email_body,
                cc_list=None,
                attachments=attachments if attachments else None,
                return_message_ids=True)

            # Gérer le résultat (peut être bool ou dict)
            if isinstance(send_result, dict):
                success = send_result.get('success', False)
                outlook_message_id = send_result.get('message_id')
                outlook_conversation_id = send_result.get('conversation_id')
            else:
                success = bool(send_result)
                outlook_message_id = None
                outlook_conversation_id = None

            if success:
                # Utiliser SENT_MANUALLY pour les envois individuels
                campaign_email.status = CampaignEmailStatus.SENT_MANUALLY if is_manual else CampaignEmailStatus.SENT
                campaign_email.sent_at = datetime.utcnow()
                sent_count += 1

                # Mettre à jour le compteur en temps réel
                campaign.emails_sent += 1

                # Créer une note de communication comme pour un courriel traditionnel
                try:
                    sender_email = user_email_config.outlook_email or user.email

                    # Préparer les données des pièces jointes pour la note
                    attachments_data = []
                    if campaign_email.pdf_attachment_data:
                        attachments_data.append({
                            'filename':
                            f'releve_{campaign_email.client_code}.pdf',
                            'size':
                            len(campaign_email.pdf_attachment_data)
                        })
                    if campaign_email.excel_attachment_data:
                        attachments_data.append({
                            'filename':
                            f'releve_{campaign_email.client_code}.xlsx',
                            'size':
                            len(campaign_email.excel_attachment_data)
                        })

                    note = CommunicationNote(
                        client_id=campaign_email.client_id,
                        user_id=user_id,
                        company_id=company_id,
                        note_type='email',
                        note_text=
                        f'Email de campagne envoyé par {sender_email} (Campagne: {campaign.name})',
                        email_from=sender_email,
                        email_to=", ".join(to_emails),
                        email_subject=campaign_email.email_subject,
                        email_body=email_body,
                        attachments=attachments_data
                        if attachments_data else None,
                        outlook_message_id=outlook_message_id,
                        conversation_id=outlook_conversation_id,
                        email_direction='sent',
                        is_conversation_active=True)
                    db.session.add(note)
                except Exception as note_error:
                    current_app.logger.error(
                        f"Erreur création note pour email {email_id}: {str(note_error)}"
                    )
            else:
                campaign_email.status = CampaignEmailStatus.FAILED
                campaign_email.error_message = "Échec d'envoi"
                failed_count += 1
                campaign.emails_failed += 1

        except Exception as e:
            current_app.logger.error(
                f"Erreur envoi email {email_id}: {str(e)}")
            campaign_email.status = CampaignEmailStatus.FAILED
            campaign_email.error_message = str(e)[:500]
            failed_count += 1
            campaign.emails_failed += 1

        db.session.commit()

    remaining = CampaignEmail.query.filter_by(
        campaign_id=campaign_id, status=CampaignEmailStatus.GENERATED).count()

    from notification_system import send_notification

    # Pour les envois manuels (individuels), vérifier aussi si campagne terminée
    if is_manual:
        # Vérifier si tous les courriels ont été traités (campagne complète)
        if remaining == 0 and campaign.status in (CampaignStatus.READY,
                                                  CampaignStatus.STOPPED):
            campaign.status = CampaignStatus.COMPLETED
            campaign.sending_completed_at = datetime.utcnow()
            db.session.commit()

            send_notification(
                user_id=user_id,
                company_id=company_id,
                type='success',
                title='Campagne terminée',
                message=
                f'Tous les courriels de la campagne "{campaign.name}" ont été envoyés.',
                data={'campaign_id': campaign_id})
        else:
            db.session.commit()
            if sent_count > 0:
                send_notification(user_id=user_id,
                                  company_id=company_id,
                                  type='success',
                                  title='Courriel envoyé',
                                  message=f'Courriel envoyé avec succès.',
                                  data={'campaign_id': campaign_id})
            elif failed_count > 0:
                send_notification(
                    user_id=user_id,
                    company_id=company_id,
                    type='error',
                    title='Échec d\'envoi',
                    message=f'Le courriel n\'a pas pu être envoyé.',
                    data={'campaign_id': campaign_id})
        return

    # Gérer les différents cas de fin d'envoi (envois en masse uniquement)
    if stopped_by_user:
        # Arrêt d'urgence demandé
        campaign.status = CampaignStatus.STOPPED
        campaign.sending_completed_at = datetime.utcnow()
        db.session.commit()

        send_notification(
            user_id=user_id,
            company_id=company_id,
            type='warning',
            title='Campagne arrêtée',
            message=
            f'Campagne "{campaign.name}" arrêtée d\'urgence: {sent_count} envoyés, {remaining} restants.',
            data={
                'campaign_id': campaign_id,
                'sent': sent_count,
                'failed': failed_count,
                'remaining': remaining
            })
    elif remaining == 0:
        # Tous les courriels ont été traités
        campaign.status = CampaignStatus.COMPLETED
        campaign.sending_completed_at = datetime.utcnow()
        db.session.commit()

        send_notification(
            user_id=user_id,
            company_id=company_id,
            type='success' if failed_count == 0 else 'warning',
            title='Envoi terminé',
            message=
            f'Campagne "{campaign.name}": {sent_count} envoyés, {failed_count} échecs.',
            data={
                'campaign_id': campaign_id,
                'sent': sent_count,
                'failed': failed_count
            })
    else:
        # Encore des courriels à envoyer (campagne en cours)
        db.session.commit()

        send_notification(
            user_id=user_id,
            company_id=company_id,
            type='info',
            title='Envoi en cours',
            message=
            f'Campagne "{campaign.name}": {sent_count} envoyés, {remaining} restants.',
            data={
                'campaign_id': campaign_id,
                'sent': sent_count,
                'failed': failed_count,
                'remaining': remaining
            })


@campaign_bp.route('/<int:campaign_id>/status', methods=['GET'])
@login_required
@limiter.limit("120 per minute")  # Limite genereuse pour le polling toutes les 2 secondes
def get_campaign_status(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Récupérer le statut de la campagne en temps réel pour le polling AJAX"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative accès statut campagne par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    status_value = campaign.status.value if hasattr(
        campaign.status, 'value') else str(campaign.status)

    # Compteurs détaillés depuis la base
    total = campaign.total_emails or 0
    sent = campaign.emails_sent or 0
    failed = campaign.emails_failed or 0

    # Calculer la progression de génération
    generation_count = 0
    generation_percent = 0
    if campaign.status == CampaignStatus.PROCESSING:
        generation_count = CampaignEmail.query.filter_by(
            campaign_id=campaign_id).count()
        generation_percent = round(
            (generation_count / total * 100), 1) if total > 0 else 0
    elif campaign.status == CampaignStatus.READY:
        # Génération terminée = 100%
        generation_count = total
        generation_percent = 100

    # Calculer la progression d'envoi
    processed = sent + failed
    progress_percent = round((processed / total * 100), 1) if total > 0 else 0

    # Déterminer si la génération est terminée (transition vers READY)
    is_generation_complete = campaign.status == CampaignStatus.READY

    return jsonify({
        'status':
        status_value,
        'total_emails':
        total,
        'emails_sent':
        sent,
        'emails_failed':
        failed,
        'processed':
        processed,
        'remaining':
        max(0, total - processed),
        'progress_percent':
        progress_percent,
        'generation_count':
        generation_count,
        'generation_percent':
        generation_percent,
        'is_generating':
        campaign.status == CampaignStatus.PROCESSING,
        'is_generation_complete':
        is_generation_complete,
        'stop_requested':
        campaign.stop_requested,
        'is_completed':
        campaign.status in (CampaignStatus.COMPLETED, CampaignStatus.STOPPED)
    })


@campaign_bp.route('/<int:campaign_id>/skip', methods=['POST'])
@login_required
def skip_emails(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Marquer des courriels comme ignorés"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative skip campagne par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    data = request.json or {}
    email_ids = data.get('email_ids', [])

    if not email_ids:
        return jsonify({'error': 'Aucun courriel sélectionné'}), 400

    updated = CampaignEmail.query.filter(
        CampaignEmail.id.in_(email_ids),
        CampaignEmail.campaign_id == campaign_id,
        CampaignEmail.status == CampaignEmailStatus.GENERATED).update(
            {'status': CampaignEmailStatus.SKIPPED}, synchronize_session=False)

    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'{updated} courriels marqués comme ignorés'
    })


@campaign_bp.route('/<int:campaign_id>/stop', methods=['POST'])
@login_required
def stop_campaign(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """
    ARRÊT D'URGENCE: Stoppe l'envoi d'une campagne en cours.
    Les courriels déjà envoyés restent SENT, les non-traités restent GENERATED.
    La campagne passe au statut STOPPED et peut être relancée plus tard.
    """
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative arrêt d'urgence campagne par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    # SÉCURITÉ: Vérifier que la campagne appartient à l'entreprise
    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    # Vérifier que la campagne est en cours d'envoi
    if campaign.status != CampaignStatus.IN_PROGRESS:
        return jsonify({
            'error':
            'Seule une campagne en cours d\'envoi peut être arrêtée'
        }), 400

    # Marquer la demande d'arrêt (le worker vérifiera ce flag)
    campaign.stop_requested = True
    campaign.stop_requested_at = datetime.utcnow()
    campaign.stop_requested_by = current_user.id

    db.session.commit()

    log_action(AuditActions.CAMPAIGN_STOPPED, entity_type=EntityTypes.CAMPAIGN,
              entity_id=campaign.id, entity_name=campaign.name)

    current_app.logger.warning(
        f"ARRÊT D'URGENCE: Campagne {campaign_id} arrêt demandé par user {current_user.id} "
        f"(company_id={company.id})")

    return jsonify({
        'success':
        True,
        'message':
        'Arrêt d\'urgence demandé. Les envois en cours vont s\'arrêter.'
    })


@campaign_bp.route('/<int:campaign_id>/cancel', methods=['POST'])
@login_required
def cancel_campaign(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Annuler une campagne"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative cancel campagne par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    if campaign.status in [CampaignStatus.COMPLETED, CampaignStatus.CANCELLED]:
        return jsonify({'error':
                        'Cette campagne ne peut pas être annulée'}), 400

    campaign.status = CampaignStatus.CANCELLED
    db.session.commit()

    return jsonify({'success': True, 'message': 'Campagne annulée'})


@campaign_bp.route('/<int:campaign_id>/delete', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    """
    Fonction vérifiée par MDF le 23/12/2025.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Supprimer une campagne"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # SÉCURITÉ: Vérifier l'accès aux campagnes (super_admin ou délégué avec can_create_campaigns)
    if not can_access_campaigns(current_user, company):
        current_app.logger.warning(
            f"SECURITE: Tentative delete campagne par user {current_user.id} sans permission"
        )
        return jsonify({'error': 'Accès refusé'}), 403

    campaign = Campaign.query.filter_by(id=campaign_id,
                                        company_id=company.id).first()
    if not campaign:
        return jsonify({'error': 'Campagne non trouvée'}), 404

    if campaign.status == CampaignStatus.IN_PROGRESS:
        return jsonify({
            'error':
            'Impossible de supprimer une campagne en cours d\'envoi'
        }), 400

    try:
        campaign_name = campaign.name
        emails_count = campaign.emails.count()

        db.session.delete(campaign)
        db.session.commit()

        log_action(AuditActions.CAMPAIGN_DELETED, entity_type=EntityTypes.CAMPAIGN,
                  entity_id=campaign_id, entity_name=campaign_name,
                  details={'emails_count': emails_count})

        current_app.logger.info(
            f"Campagne '{campaign_name}' (ID: {campaign_id}) supprimée avec {emails_count} courriels"
        )
        flash('Campagne supprimée avec succès.', 'success')
        return jsonify({
            'success': True,
            'redirect': url_for('campaign.list_campaigns')
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Erreur suppression campagne {campaign_id}: {str(e)}")
        return jsonify({'error':
                        f'Erreur lors de la suppression: {str(e)}'}), 500
