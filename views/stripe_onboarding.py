import os
import re
import stripe
from flask import Blueprint, request, redirect, flash, url_for, render_template, current_app, session
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from app import db, limiter
from models import Plan, Company, User

onboarding_bp = Blueprint('onboarding', __name__, url_prefix='/onboarding')


def _get_base_url():
    from flask import request as req
    host = req.host_url.rstrip('/')
    if host.startswith('http://') and 'localhost' not in host:
        host = host.replace('http://', 'https://', 1)
    return host


@onboarding_bp.route('/start-trial/<int:plan_id>')
def start_trial(plan_id):
    plan = Plan.query.get_or_404(plan_id)

    if not plan.is_active or plan.is_free or not plan.stripe_price_id:
        flash('Ce plan n\'est pas disponible pour l\'essai gratuit.', 'error')
        return redirect(url_for('marketing.tarifs'))

    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{
                'price': plan.stripe_price_id,
                'quantity': 1,
                'adjustable_quantity': {
                    'enabled': True,
                    'minimum': 1,
                    'maximum': 50
                }
            }],
            mode='subscription',
            automatic_tax={'enabled': True},
            billing_address_collection='required',
            shipping_address_collection={
                'allowed_countries': ['FR', 'CA', 'US', 'GB', 'DE', 'ES', 'IT', 'BE', 'CH', 'NL']
            },
            custom_fields=[
                {
                    'key': 'first_name',
                    'label': {'type': 'custom', 'custom': 'Prénom'},
                    'type': 'text',
                },
                {
                    'key': 'last_name',
                    'label': {'type': 'custom', 'custom': 'Nom'},
                    'type': 'text',
                },
                {
                    'key': 'company_name',
                    'label': {'type': 'custom', 'custom': 'Nom de votre entreprise'},
                    'type': 'text',
                },
            ],
            custom_text={
                'submit': {
                    'message': 'Votre essai gratuit de 14 jours commence maintenant. Vous ne serez facturé qu\'à la fin de la période d\'essai.'
                }
            },
            allow_promotion_codes=True,
            subscription_data={
                'trial_period_days': 14,
                'metadata': {
                    'registration_type': 'stripe_onboarding',
                    'plan_id': str(plan.id)
                }
            },
            success_url=f'{_get_base_url()}/onboarding/checkout-success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{_get_base_url()}/tarifs',
            metadata={
                'registration_type': 'stripe_onboarding',
                'plan_id': str(plan.id),
                'visitor': request.cookies.get('visitor', '')
            }
        )

        current_app.logger.info(f"Onboarding checkout session created for plan {plan.name}: {checkout_session.id}")
        if checkout_session.url:
            return redirect(checkout_session.url)
        else:
            flash('Erreur lors de la création de la session de paiement.', 'error')
            return redirect(url_for('marketing.tarifs'))

    except stripe.error.StripeError as e:
        current_app.logger.error(f"Stripe error in start_trial: {e}")
        flash('Erreur lors de la connexion au système de paiement. Veuillez réessayer.', 'error')
        return redirect(url_for('marketing.tarifs'))
    except Exception as e:
        current_app.logger.error(f"Error in start_trial: {e}")
        flash('Erreur inattendue. Veuillez réessayer.', 'error')
        return redirect(url_for('marketing.tarifs'))


@onboarding_bp.route('/add-company/plan-selection')
@login_required
def add_company_plan_selection():
    plans = Plan.query.filter_by(is_active=True).filter(Plan.is_free == False).order_by(Plan.plan_level.asc()).all()

    enriched_plans = []
    for plan in plans:
        enriched_plans.append({
            'id': plan.id,
            'name': plan.name,
            'display_name': plan.display_name,
            'description': plan.description,
            'max_clients': plan.max_clients,
            'allows_email_sending': plan.allows_email_sending,
            'allows_email_connection': plan.allows_email_connection,
            'allows_accounting_connection': plan.allows_accounting_connection,
            'allows_team_management': plan.allows_team_management,
            'allows_email_templates': plan.allows_email_templates,
            'pricing_info': plan.get_pricing_info()
        })

    is_eligible_for_trial = _check_trial_eligibility_for_user(current_user)

    return render_template('onboarding/plan_selection.html', plans=enriched_plans, is_eligible_for_trial=is_eligible_for_trial)


@onboarding_bp.route('/add-company/start/<int:plan_id>')
@login_required
def add_company_start(plan_id):
    plan = Plan.query.get_or_404(plan_id)

    if not plan.is_active or plan.is_free or not plan.stripe_price_id:
        flash('Ce plan n\'est pas disponible.', 'error')
        return redirect(url_for('onboarding.add_company_plan_selection'))

    is_eligible_for_trial = _check_trial_eligibility_for_user(current_user)

    subscription_data = {
        'metadata': {
            'registration_type': 'stripe_onboarding',
            'plan_id': str(plan.id)
        }
    }
    if is_eligible_for_trial:
        subscription_data['trial_period_days'] = 14
        trial_message = 'Votre essai gratuit de 14 jours commence maintenant. Vous ne serez facturé qu\'à la fin de la période d\'essai.'
    else:
        trial_message = 'La facturation commencera immédiatement après la confirmation.'

    try:
        checkout_params = {
            'customer_email': current_user.email,
            'line_items': [{
                'price': plan.stripe_price_id,
                'quantity': 1,
                'adjustable_quantity': {
                    'enabled': True,
                    'minimum': 1,
                    'maximum': 50
                }
            }],
            'mode': 'subscription',
            'automatic_tax': {'enabled': True},
            'billing_address_collection': 'required',
            'shipping_address_collection': {
                'allowed_countries': ['FR', 'CA', 'US', 'GB', 'DE', 'ES', 'IT', 'BE', 'CH', 'NL']
            },
            'custom_fields': [
                {
                    'key': 'first_name',
                    'label': {'type': 'custom', 'custom': 'Prénom'},
                    'type': 'text',
                },
                {
                    'key': 'last_name',
                    'label': {'type': 'custom', 'custom': 'Nom'},
                    'type': 'text',
                },
                {
                    'key': 'company_name',
                    'label': {'type': 'custom', 'custom': 'Nom de votre entreprise'},
                    'type': 'text',
                },
            ],
            'custom_text': {
                'submit': {
                    'message': trial_message
                }
            },
            'allow_promotion_codes': True,
            'subscription_data': subscription_data,
            'success_url': f'{_get_base_url()}/onboarding/checkout-success?session_id={{CHECKOUT_SESSION_ID}}',
            'cancel_url': f'{_get_base_url()}/onboarding/add-company/plan-selection',
            'metadata': {
                'registration_type': 'stripe_onboarding',
                'plan_id': str(plan.id),
                'visitor': request.cookies.get('visitor', '')
            }
        }

        checkout_session = stripe.checkout.Session.create(**checkout_params)

        current_app.logger.info(f"Add company checkout for user {current_user.email}, plan {plan.name}: {checkout_session.id}")
        if checkout_session.url:
            return redirect(checkout_session.url)
        else:
            flash('Erreur lors de la création de la session de paiement.', 'error')
            return redirect(url_for('onboarding.add_company_plan_selection'))

    except Exception as e:
        current_app.logger.error(f"Error in add_company_start: {e}")
        flash('Erreur lors de la connexion au système de paiement.', 'error')
        return redirect(url_for('onboarding.add_company_plan_selection'))


@onboarding_bp.route('/checkout-success')
def checkout_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash('Session invalide.', 'error')
        return redirect(url_for('auth.login'))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)

        if checkout_session.status != 'complete':
            current_app.logger.warning(f"Checkout session {session_id} not complete: {checkout_session.status}")
            return render_template('onboarding/checkout_pending.html')

        metadata = checkout_session.get('metadata', {})
        registration_type = metadata.get('registration_type')

        if registration_type != 'stripe_onboarding':
            flash('Session de paiement non reconnue.', 'error')
            return redirect(url_for('auth.login'))

        customer_email = None
        customer_name = None
        if checkout_session.customer_details:
            customer_email = getattr(checkout_session.customer_details, 'email', None)
            customer_name = getattr(checkout_session.customer_details, 'name', None)

        if not customer_email and checkout_session.customer:
            try:
                stripe_customer = stripe.Customer.retrieve(checkout_session.customer)
                customer_email = stripe_customer.email
                customer_name = customer_name or stripe_customer.name
                current_app.logger.info(f"Retrieved email from Stripe customer: {customer_email}")
            except Exception as e:
                current_app.logger.warning(f"Could not retrieve Stripe customer: {e}")

        if not customer_email:
            current_app.logger.error("Checkout session has no customer email")
            return render_template('onboarding/checkout_pending.html')

        session_dict = {
            'id': checkout_session.id,
            'customer': checkout_session.customer,
            'subscription': checkout_session.subscription,
            'customer_details': {
                'email': customer_email,
                'name': customer_name,
            },
            'metadata': dict(metadata) if metadata else {},
            'custom_fields': []
        }

        if checkout_session.custom_fields:
            for field in checkout_session.custom_fields:
                try:
                    field_key = getattr(field, 'key', '') or ''
                    field_type = getattr(field, 'type', '') or ''
                    field_dict = {'key': field_key, 'type': field_type}
                    text_obj = getattr(field, 'text', None)
                    if text_obj:
                        text_value = getattr(text_obj, 'value', '') or ''
                        field_dict['text'] = {'value': text_value}
                    session_dict['custom_fields'].append(field_dict)
                except Exception as e:
                    current_app.logger.warning(f"Error parsing custom field: {e}")

        from stripe_finov.webhooks.onboarding_handler import process_onboarding_session
        result = process_onboarding_session(session_dict, event_id=f"checkout_success_{session_id}")

        if result.get('success'):
            if result.get('already_exists'):
                user = User.query.get(result['user_id'])
                if user and not user.must_change_password:
                    flash('Votre entreprise est déjà configurée. Connectez-vous pour y accéder.', 'success')
                    return redirect(url_for('auth.login', email=result['email']))
                else:
                    return render_template('onboarding/checkout_pending.html', email=result['email'])

            if result.get('is_new_user'):
                return render_template('onboarding/checkout_pending.html',
                                       email=result['email'],
                                       account_created=True,
                                       company_name=result.get('company_name', ''))
            else:
                flash(f'Votre nouvelle entreprise a été ajoutée avec succès !', 'success')
                return redirect(url_for('auth.login', email=result['email']))
        else:
            current_app.logger.error(f"Onboarding processing failed: {result.get('error')}")
            customer_email = checkout_session.customer_details.email if checkout_session.customer_details else None
            return render_template('onboarding/checkout_pending.html', email=customer_email)

    except Exception as e:
        current_app.logger.error(f"Error in checkout_success: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return render_template('onboarding/checkout_pending.html')


@onboarding_bp.route('/add-company/success')
@login_required
def add_company_success():
    flash('Votre nouvelle entreprise a été ajoutée avec succès ! Elle sera disponible sous quelques instants.', 'success')
    return redirect(url_for('main.dashboard'))


@onboarding_bp.route('/setup-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def setup_password(token):
    from onboarding_models import PasswordSetupToken

    token_record, status = PasswordSetupToken.verify_token(token)

    if status == 'expired':
        return render_template('onboarding/token_expired.html')
    if status == 'invalid':
        return render_template('onboarding/token_expired.html', invalid=True)

    user = User.query.get(token_record.user_id)
    company = Company.query.get(token_record.company_id)

    if not user:
        flash('Compte utilisateur introuvable.', 'error')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        if len(password) < 8:
            flash('Le mot de passe doit contenir au moins 8 caractères.', 'error')
            return render_template('onboarding/setup_password.html',
                                   user=user, company=company, token=token)

        if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', password):
            flash('Le mot de passe doit contenir au moins une minuscule, une majuscule et un chiffre.', 'error')
            return render_template('onboarding/setup_password.html',
                                   user=user, company=company, token=token)

        if password != password2:
            flash('Les mots de passe ne correspondent pas.', 'error')
            return render_template('onboarding/setup_password.html',
                                   user=user, company=company, token=token)

        user.password_hash = generate_password_hash(password)
        user.must_change_password = False
        token_record.mark_used()
        db.session.commit()

        current_app.logger.info(f"Password set for user {user.email} via onboarding token")

        flash('Votre mot de passe a ete configure avec succes. Veuillez vous connecter.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('onboarding/setup_password.html',
                           user=user, company=company, token=token)


def _check_trial_eligibility_for_user(user):
    from stripe_finov.webhooks.onboarding_handler import _check_trial_eligibility
    return _check_trial_eligibility(user)


@onboarding_bp.route('/register-free', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def register_free():
    """Inscription directe au Plan Découverte (gratuit) sans Stripe."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        company_name = request.form.get('company_name', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        errors = []
        if not first_name:
            errors.append('Le prénom est requis.')
        if not last_name:
            errors.append('Le nom est requis.')
        if not email:
            errors.append('L\'adresse email est requise.')
        if not company_name:
            errors.append('Le nom de votre entreprise est requis.')
        if len(password) < 8:
            errors.append('Le mot de passe doit contenir au moins 8 caractères.')
        elif not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', password):
            errors.append('Le mot de passe doit contenir au moins une minuscule, une majuscule et un chiffre.')
        if password != password2:
            errors.append('Les mots de passe ne correspondent pas.')

        if not errors:
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                errors.append('Un compte existe déjà avec cette adresse email. Veuillez vous connecter.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('onboarding/register_free.html',
                                   form_data=request.form)

        free_plan = Plan.query.filter_by(is_free=True, is_active=True).first()
        if not free_plan:
            flash('Le plan Découverte n\'est pas disponible pour le moment. Veuillez réessayer plus tard.', 'error')
            return redirect(url_for('marketing.tarifs'))

        try:
            from datetime import datetime
            from models import UserCompany

            user = User()
            user.first_name = first_name
            user.last_name = last_name
            user.email = email
            user.password_hash = generate_password_hash(password)
            user.must_change_password = False
            user.terms_accepted_at = datetime.utcnow()
            user.terms_version_accepted = '1.0'
            db.session.add(user)
            db.session.flush()

            company = Company()
            company.name = company_name
            company.email = email
            company.plan_id = free_plan.id
            company.plan = free_plan.name
            company.plan_status = 'active'
            company.is_free_account = True
            company.quantity_licenses = 1
            company.status = 'active'
            company.subscription_status = 'active'
            company.created_at = datetime.utcnow()
            company.created_by_user_id = user.id
            db.session.add(company)
            db.session.flush()

            user_company = UserCompany()
            user_company.user_id = user.id
            user_company.company_id = company.id
            user_company.role = 'super_admin'
            user_company.is_active = True
            user_company.created_at = datetime.utcnow()
            db.session.add(user_company)

            db.session.commit()

            current_app.logger.info(f"Free plan registration: user={user.id}, company={company.id}, email={email}")

            flash(f'Bienvenue {first_name} ! Votre compte a ete cree avec succes. Veuillez vous connecter.', 'success')
            return redirect(url_for('auth.login'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error in register_free: {e}")
            import traceback
            current_app.logger.error(traceback.format_exc())
            flash('Une erreur est survenue lors de la création du compte. Veuillez réessayer.', 'error')
            return render_template('onboarding/register_free.html', form_data=request.form)

    return render_template('onboarding/register_free.html', form_data={})
