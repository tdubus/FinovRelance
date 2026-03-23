"""
REFONTE STRIPE V2 - Module Checkout et sélection de plans
Architecture simplifiée avec redirection directe vers Stripe Checkout
"""
import os
import stripe
from flask import Blueprint, request, redirect, flash, url_for, render_template, current_app, session
from flask_login import login_required, current_user
# CSRF exempt pas nécessaire pour cette route
from models import Plan, Company, db
# from utils import require_company_access  # Will create a simple inline version

stripe_checkout_v2_bp = Blueprint('stripe_checkout_v2', __name__, url_prefix='/stripe/v2/checkout')

# Blueprint séparé pour les routes portal (hors préfixe checkout)
stripe_portal_bp = Blueprint('stripe_portal', __name__, url_prefix='/stripe/v2')

def require_company_access(f):
    """Simple decorator to check if user has access to selected company"""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import session, redirect, url_for, flash

        if not session.get('selected_company_id'):
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('auth.logout'))
        return f(*args, **kwargs)
    return decorated_function

def setup_stripe():
    """Verify Stripe API key is configured (set centrally in app.py bootstrap)"""
    if not stripe.api_key:
        current_app.logger.error("STRIPE_SECRET_KEY manquant dans l'environnement")
        return False
    return True

@stripe_checkout_v2_bp.route('/plan-selection')
@login_required
@require_company_access
def plan_selection():
    """Page de sélection de plan pour nouvelle inscription ou upgrade"""
    try:
        company = Company.query.get(session.get('selected_company_id'))
        if not company:
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('auth.logout'))

        # NOUVEAU: Si l'entreprise a un plan payant avec statut "pending",
        # rediriger automatiquement vers Stripe Checkout
        if (company.plan_status == 'pending' and
            company.plan_id and
            not company.is_free_account):

            plan = Plan.query.get(company.plan_id)
            if plan and not plan.is_free and plan.stripe_price_id:
                current_app.logger.info(f"Auto-redirection vers Stripe Checkout pour {company.name} - Plan {plan.name} pending")

                # Rediriger automatiquement vers la création de session Stripe avec les paramètres de l'entreprise
                try:
                    if not setup_stripe():
                        flash('Configuration Stripe manquante.', 'error')
                        return redirect(url_for('company.settings'))

                    # URL de retour - Utilise APP_URL
                    YOUR_DOMAIN = os.environ.get('APP_URL', 'https://app.finov-relance.com').replace('https://', '').replace('http://', '')

                    # Créer la session Stripe Checkout directement AVEC quantité ajustable
                    checkout_session = stripe.checkout.Session.create(
                        customer=company.stripe_customer_id,
                        line_items=[{
                            'price': plan.stripe_price_id,
                            'quantity': company.quantity_licenses or 1,
                            'adjustable_quantity': {
                                'enabled': True,
                                'minimum': 1,
                                'maximum': 50  # Limite raisonnable
                            }
                        }],
                        mode='subscription',
                        automatic_tax={'enabled': True},
                        customer_update={
                            'shipping': 'auto',
                            'address': 'auto'
                        },
                        success_url=f'https://{YOUR_DOMAIN}/company/settings?checkout=success&session_id={{CHECKOUT_SESSION_ID}}',
                        cancel_url=f'https://{YOUR_DOMAIN}/company/settings?checkout=cancel',
                        metadata={
                            'company_id': str(company.id),
                            'plan_id': str(plan.id),
                            'quantity_licenses': str(company.quantity_licenses or 1),
                            'user_id': str(current_user.id),
                            'auto_checkout': 'true'
                        },
                        subscription_data={
                            **({'trial_period_days': 14} if not company.stripe_subscription_id else {}),
                            'metadata': {
                                'company_id': str(company.id),
                                'plan_id': str(plan.id),
                                'quantity_licenses': str(company.quantity_licenses or 1)
                            }
                        }
                    )

                    current_app.logger.info(f"Auto-redirection Stripe Checkout pour {company.name}: {checkout_session.id}")
                    if checkout_session.url:
                        current_app.logger.info(f"Auto-redirection vers Stripe Checkout: {checkout_session.url}")
                        return render_template('stripe/v2/redirect_to_checkout.html', checkout_url=checkout_session.url)
                    else:
                        flash('Erreur: URL de paiement non disponible.', 'error')
                        return redirect(url_for('company.settings'))

                except Exception as e:
                    current_app.logger.error(f"Erreur auto-redirection Stripe: {str(e)}")
                    flash('Erreur lors de la redirection vers le paiement.', 'error')
                    return redirect(url_for('company.settings'))

        # Récupérer tous les plans actifs
        active_plans = Plan.query.filter_by(is_active=True).order_by(Plan.plan_level.asc()).all()

        # Enrichir les plans avec les informations de prix depuis Stripe
        enriched_plans = []
        for plan in active_plans:
            plan_data = {
                'id': plan.id,
                'name': plan.name,
                'display_name': plan.display_name,
                'description': plan.description,
                'is_free': plan.is_free,
                'max_clients': plan.max_clients,
                'allows_email_sending': plan.allows_email_sending,
                'allows_email_connection': plan.allows_email_connection,
                'allows_accounting_connection': plan.allows_accounting_connection,
                'allows_team_management': plan.allows_team_management,
                'allows_email_templates': plan.allows_email_templates,
                'pricing_info': plan.get_pricing_info()
            }
            enriched_plans.append(plan_data)

        current_app.logger.info(f"Affichage sélection plan pour entreprise {company.name} - {len(enriched_plans)} plans disponibles")

        # Ne considérer le plan comme "actuel" que si l'entreprise a un abonnement Stripe actif
        # Cela permet de sélectionner le même plan pour l'onboarding Stripe ultérieur
        has_active_subscription = bool(company.stripe_subscription_id)
        effective_current_plan_id = company.plan_id if has_active_subscription else None

        return render_template('stripe/v2/plan_selection.html',
                             plans=enriched_plans,
                             company=company,
                             current_plan_id=effective_current_plan_id,
                             has_active_subscription=has_active_subscription)

    except Exception as e:
        current_app.logger.error(f"Erreur affichage sélection plan: {str(e)}")
        flash('Erreur lors du chargement des plans disponibles.', 'error')
        return redirect(url_for('company.settings'))

@stripe_checkout_v2_bp.route('/create-session', methods=['POST'])
@login_required
@require_company_access
def create_checkout_session():
    """Créer une session Stripe Checkout pour le plan sélectionné"""
    try:
        if not setup_stripe():
            flash('Configuration Stripe manquante.', 'error')
            return redirect(url_for('stripe_checkout_v2.plan_selection'))

        company = Company.query.get(session.get('selected_company_id'))
        if not company:
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('auth.logout'))

        # Récupérer les données du formulaire
        plan_id = request.form.get('plan_id')
        try:
            quantity_licenses = int(request.form.get('quantity_licenses', 1))
        except (ValueError, TypeError):
            quantity_licenses = 1
        quantity_licenses = max(1, min(500, quantity_licenses))

        if not plan_id:
            flash('Aucun plan sélectionné.', 'error')
            return redirect(url_for('stripe_checkout_v2.plan_selection'))

        # Récupérer le plan depuis la base de données
        plan = Plan.query.get_or_404(plan_id)

        if not plan.is_active:
            flash('Le plan sélectionné n\'est plus disponible.', 'error')
            return redirect(url_for('stripe_checkout_v2.plan_selection'))

        # Plan gratuit - pas besoin de Stripe Checkout
        if plan.is_free:
            # Mettre à jour directement l'entreprise
            company.plan_id = plan.id
            company.plan = plan.name
            company.plan_status = 'active'
            company.quantity_licenses = 1  # Plan gratuit = 1 licence
            company.is_free_account = True

            db.session.commit()
            current_app.logger.info(f"Plan gratuit {plan.name} activé pour {company.name}")
            flash(f'Plan "{plan.display_name}" activé avec succès !', 'success')
            return redirect(url_for('company.settings'))

        # Plan payant - redirection vers Stripe Checkout
        if not plan.stripe_price_id:
            flash('Plan non configuré correctement dans Stripe.', 'error')
            return redirect(url_for('stripe_checkout_v2.plan_selection'))

        is_eligible_for_trial = not company.stripe_customer_id and not company.stripe_subscription_id

        # Créer le client Stripe si nécessaire
        if not company.stripe_customer_id:
            try:
                from stripe_integration import create_stripe_customer
                customer = create_stripe_customer(company, user=current_user)
                company.stripe_customer_id = customer.id
                db.session.commit()
                current_app.logger.info(f"Client Stripe créé pour {company.name}: {customer.id}")
            except Exception as e:
                current_app.logger.error(f"Erreur création client Stripe: {str(e)}")
                flash('Erreur lors de la configuration du paiement.', 'error')
                return redirect(url_for('stripe_checkout_v2.plan_selection'))

        # URL de retour
        YOUR_DOMAIN = os.environ.get('APP_URL', 'https://app.finov-relance.com').replace('https://', '').replace('http://', '')

        # Créer la session Stripe Checkout AVEC quantité ajustable
        try:
            checkout_session = stripe.checkout.Session.create(
                customer=company.stripe_customer_id,
                line_items=[{
                    'price': plan.stripe_price_id,
                    'quantity': quantity_licenses,
                    'adjustable_quantity': {
                        'enabled': True,
                        'minimum': 1,
                        'maximum': 50  # Limite raisonnable
                    }
                }],
                mode='subscription',
                automatic_tax={'enabled': True},
                customer_update={
                    'shipping': 'auto',
                    'address': 'auto'
                },
                success_url=f'https://{YOUR_DOMAIN}/company/settings?checkout=success&session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'https://{YOUR_DOMAIN}/company/settings?checkout=cancel',
                billing_address_collection='required',
                shipping_address_collection={
                    'allowed_countries': ['FR', 'CA', 'US', 'GB', 'DE', 'ES', 'IT', 'BE', 'CH', 'NL']
                },
                allow_promotion_codes=True,
                custom_text={
                    'submit': {
                        'message': 'Important : Veuillez indiquer le nom de votre entreprise dans "Nom du titulaire de la carte" pour que les factures soient établies au nom de votre société.'
                    }
                },
                metadata={
                    'company_id': str(company.id),
                    'plan_id': str(plan.id),
                    'quantity_licenses': str(quantity_licenses),
                    'user_id': str(current_user.id)
                },
                subscription_data={
                    **({'trial_period_days': 14} if is_eligible_for_trial else {}),
                    'metadata': {
                        'company_id': str(company.id),
                        'plan_id': str(plan.id),
                        'quantity_licenses': str(quantity_licenses)
                    }
                }
            )

            current_app.logger.info(f"Session Stripe Checkout créée pour {company.name}: {checkout_session.id}")
            if checkout_session.url:
                current_app.logger.info(f"Redirection vers Stripe Checkout: {checkout_session.url}")
                return render_template('stripe/v2/redirect_to_checkout.html', checkout_url=checkout_session.url)
            else:
                flash('Erreur: URL de paiement non disponible.', 'error')
                return redirect(url_for('stripe_checkout_v2.plan_selection'))

        except Exception as e:
            current_app.logger.error(f"Erreur création session Checkout: {str(e)}")
            flash('Erreur lors de la création de la session de paiement.', 'error')
            return redirect(url_for('stripe_checkout_v2.plan_selection'))

    except Exception as e:
        current_app.logger.error(f"Erreur générale create_checkout_session: {str(e)}")
        flash('Erreur inattendue lors de la création de la session de paiement.', 'error')
        return redirect(url_for('stripe_checkout_v2.plan_selection'))

@stripe_checkout_v2_bp.route('/success')
@login_required
def company_checkout_success():
    """Page de succès après paiement Stripe"""
    try:
        session_id = request.args.get('session_id')
        if not session_id:
            flash('Session de paiement invalide.', 'error')
            return redirect(url_for('company.settings'))

        if not setup_stripe():
            flash('Configuration Stripe manquante.', 'error')
            return redirect(url_for('company.settings'))

        # Récupérer les détails de la session
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            current_app.logger.info(f"Paiement réussi - Session: {session_id}")

            # Rechercher la compagnie créée/mise à jour via le customer_id
            customer_id = checkout_session.get('customer')
            if customer_id:
                from models import Company
                company = Company.query.filter_by(stripe_customer_id=customer_id).first()
                if company:
                    # Mettre à jour la session pour sélectionner cette compagnie
                    session['selected_company_id'] = company.id
                    current_app.logger.info(f"Session mise à jour vers compagnie: {company.name} (ID: {company.id})")
                    flash(f'Paiement effectué avec succès ! Bienvenue chez {company.name}.', 'success')
                else:
                    flash('Paiement effectué avec succès ! Votre abonnement est maintenant actif.', 'success')
            else:
                flash('Paiement effectué avec succès ! Votre abonnement est maintenant actif.', 'success')

            return redirect(url_for('company.settings'))

        except Exception as e:
            current_app.logger.error(f"Erreur récupération session Stripe: {str(e)}")
            flash('Paiement effectué mais erreur de synchronisation. Contactez le support si nécessaire.', 'warning')
            return redirect(url_for('company.settings'))

    except Exception as e:
        current_app.logger.error(f"Erreur checkout_success: {str(e)}")
        flash('Erreur lors de la confirmation du paiement.', 'error')
        return redirect(url_for('company.settings'))

@stripe_checkout_v2_bp.route('/cancel')
@login_required
def checkout_cancel():
    """Page d'annulation du paiement"""
    current_app.logger.info(f"Paiement annulé par l'utilisateur {current_user.id}")
    flash('Paiement annulé. Vous pouvez recommencer quand vous le souhaitez.', 'info')
    return redirect(url_for('stripe_checkout_v2.plan_selection'))

# ===== ROUTES PORTAL STRIPE =====

@stripe_portal_bp.route('/customer-portal', methods=['POST'])
@stripe_portal_bp.route('/create-portal-session', methods=['GET', 'POST'])
@login_required
def create_portal_session():
    """Créer une session portail Stripe pour gérer l'abonnement"""
    try:
        # Récupérer l'entreprise sélectionnée (avec fallback vers entreprise primaire)
        company = current_user.get_selected_company()
        if not company:
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('company.settings'))

        # S'assurer que la session est synchronisée avec l'entreprise récupérée
        if session.get('selected_company_id') != company.id:
            session['selected_company_id'] = company.id
            current_app.logger.info(f"Session synchronisée pour portail Stripe: {company.name} (ID: {company.id})")

        # Vérifier que l'utilisateur a accès à cette entreprise
        from models import UserCompany
        user_company = UserCompany.query.filter_by(
            user_id=current_user.id,
            company_id=company.id
        ).first()

        if not user_company:
            flash('Accès non autorisé à cette entreprise.', 'error')
            return redirect(url_for('company.settings'))

        # URL de retour
        return_url = url_for('company.settings', _external=True)

        # Créer la session portal Stripe
        from stripe_integration import create_stripe_portal_session
        portal_url = create_stripe_portal_session(company, return_url)

        if not portal_url:
            flash('Impossible de créer la session de gestion d\'abonnement. Contactez le support.', 'error')
            return redirect(url_for('company.settings'))

        # Rediriger vers Stripe Portal
        current_app.logger.info(f"Redirection vers Stripe Portal pour {company.name}: {portal_url}")
        return redirect(portal_url)

    except Exception as e:
        current_app.logger.error(f"Erreur création session portal: {str(e)}")
        flash('Erreur lors de la création de la session de gestion.', 'error')
        return redirect(url_for('company.settings'))

