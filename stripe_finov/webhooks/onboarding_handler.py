import logging
import os
import stripe
from datetime import datetime
from app import db
from models import User, Company, UserCompany, Plan, SubscriptionAuditLog
from onboarding_models import PasswordSetupToken
from utils.onboarding_email import send_password_setup_email, send_new_company_confirmation_email
from werkzeug.security import generate_password_hash
import secrets

logger = logging.getLogger(__name__)


def _get_base_domain():
    app_url = os.environ.get('APP_URL')
    if app_url:
        return app_url.rstrip('/')
    if os.environ.get('FLASK_ENV') == 'development':
        return 'http://localhost:5000'
    return 'https://app.finov-relance.com'


def process_onboarding_session(session_data, event_id=None):
    """
    Fonction principale de traitement d'une session onboarding.
    Appelable depuis:
    - Le webhook (checkout.session.completed)
    - La route checkout_success (traitement direct)
    
    Retourne un dict avec le résultat:
    {
        'success': bool,
        'is_new_user': bool,
        'user_id': int,
        'company_id': int,
        'email': str,
        'error': str (si success=False)
    }
    """
    metadata = session_data.get("metadata", {})
    custom_fields = session_data.get("custom_fields", [])

    customer_id = session_data.get("customer")
    subscription_id = session_data.get("subscription")
    customer_details = session_data.get("customer_details", {})

    if isinstance(customer_details, dict):
        email = customer_details.get("email", "").strip().lower()
        name = customer_details.get("name", "")
    else:
        email = getattr(customer_details, 'email', '') or ''
        email = email.strip().lower()
        name = getattr(customer_details, 'name', '') or ''

    plan_id_str = metadata.get("plan_id")
    company_name = _extract_company_name(custom_fields, metadata)

    if not email or not plan_id_str:
        logger.error(f"Onboarding checkout missing required fields: email={email}, plan_id={plan_id_str}")
        return {'success': False, 'error': 'missing_fields'}

    existing_company = Company.query.filter_by(stripe_customer_id=customer_id).first()
    if not existing_company and subscription_id:
        existing_company = Company.query.filter_by(stripe_subscription_id=subscription_id).first()
    if existing_company:
        logger.info(f"Idempotent: company already exists for customer {customer_id}")
        _ensure_user_company_relation(existing_company, email)
        db.session.commit()
        user = User.query.filter_by(email=email).first()
        return {
            'success': True,
            'is_new_user': False,
            'user_id': user.id if user else None,
            'company_id': existing_company.id,
            'email': email,
            'already_exists': True
        }

    try:
        plan_id = int(plan_id_str)
    except (ValueError, TypeError):
        logger.error(f"Invalid plan_id: {plan_id_str}")
        return {'success': False, 'error': 'invalid_plan'}

    plan = Plan.query.get(plan_id)
    if not plan:
        logger.error(f"Plan {plan_id} not found")
        return {'success': False, 'error': 'plan_not_found'}

    subscription = None
    quantity = 1
    subscription_status = "active"

    if subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(
                subscription_id,
                expand=["items.data.price.product"]
            )
            subscription_status = subscription.get("status", "active")
            items_data = subscription.get("items", {})
            if isinstance(items_data, dict):
                item_list = items_data.get("data", [])
            else:
                item_list = getattr(items_data, 'data', []) if items_data else []
            if item_list:
                quantity = item_list[0].get("quantity", 1) or 1
        except Exception as e:
            logger.error(f"Error retrieving subscription {subscription_id}: {e}")
            subscription_status = "active"

    first_name = _extract_custom_field(custom_fields, 'first_name')
    last_name = _extract_custom_field(custom_fields, 'last_name')
    if not first_name and not last_name:
        first_name, last_name = _parse_name(name)

    existing_user = User.query.filter_by(email=email).first()
    is_new_user = existing_user is None

    trial_should_be_cancelled = False

    if not is_new_user:
        if not _check_trial_eligibility(existing_user):
            trial_should_be_cancelled = True
            logger.info(f"Existing user {email} NOT eligible for trial (has prior Stripe data)")

    if not trial_should_be_cancelled and customer_id:
        existing_by_customer = Company.query.filter_by(stripe_customer_id=customer_id).first()
        if existing_by_customer:
            trial_should_be_cancelled = True
            logger.info(f"Customer {customer_id} already linked to company {existing_by_customer.id}, cancelling trial")

    if trial_should_be_cancelled and subscription_id:
        _cancel_trial_on_subscription(subscription_id)
        if subscription_status == "trialing":
            subscription_status = "active"

    if is_new_user:
        user = User()
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.password_hash = generate_password_hash(secrets.token_urlsafe(32))
        user.must_change_password = True
        user.terms_accepted_at = datetime.utcnow()
        user.terms_version_accepted = '1.0'
        db.session.add(user)
        db.session.flush()
        logger.info(f"New user created: {email} (id={user.id})")
    else:
        user = existing_user
        logger.info(f"Existing user found: {email} (id={user.id})")

    company = Company()
    company.name = company_name or f"Entreprise de {first_name}"
    company.email = email
    company.plan_id = plan.id
    company.plan = plan.name
    company.plan_status = 'active'
    company.is_free_account = False
    company.quantity_licenses = quantity
    valid_statuses = {'active', 'pending_cancellation', 'pending_downgrade', 'managed_by_schedule', 'canceled', 'past_due', 'unpaid', 'expired'}
    company.status = subscription_status if subscription_status in valid_statuses else 'active'
    company.subscription_status = subscription_status
    company.stripe_customer_id = customer_id
    company.stripe_subscription_id = subscription_id
    company.created_at = datetime.utcnow()
    company.created_by_user_id = user.id
    db.session.add(company)
    db.session.flush()
    logger.info(f"New company created: {company.name} (id={company.id})")

    user_company = UserCompany()
    user_company.user_id = user.id
    user_company.company_id = company.id
    user_company.role = 'super_admin'
    user_company.is_active = True
    user_company.created_at = datetime.utcnow()
    db.session.add(user_company)

    if event_id:
        _log_audit(company.id, event_id, {
            "registration_type": "stripe_onboarding",
            "is_new_user": is_new_user,
            "email": email,
            "company_name": company.name,
            "plan": plan.name,
            "quantity": quantity
        })

    db.session.commit()
    logger.info(f"Onboarding completed: user={user.id}, company={company.id}, new_user={is_new_user}")

    try:
        if is_new_user:
            raw_token, token_record = PasswordSetupToken.create_token(user.id, company.id)
            db.session.commit()
            base_domain = _get_base_domain()
            setup_url = f"{base_domain}/onboarding/setup-password/{raw_token}"
            send_password_setup_email(email, first_name, company.name, setup_url)
            logger.info(f"Password setup email sent to {email} (base: {base_domain})")
        else:
            base_domain = _get_base_domain()
            login_url = f"{base_domain}/auth/login"
            has_trial = (subscription_status == "trialing")
            send_new_company_confirmation_email(email, user.first_name, company.name, login_url, has_trial=has_trial)
            logger.info(f"Confirmation email sent to existing user {email}")
    except Exception as e:
        logger.error(f"Error sending onboarding email to {email}: {e}")

    return {
        'success': True,
        'is_new_user': is_new_user,
        'user_id': user.id,
        'company_id': company.id,
        'company_name': company.name,
        'email': email
    }


def handle_onboarding_checkout_completed(event):
    """Wrapper webhook : appelle process_onboarding_session avec les données de l'event."""
    session_data = event["data"]["object"]
    event_id = event["id"]
    
    result = process_onboarding_session(session_data, event_id=event_id)
    
    if result.get('success'):
        return "ok", 200
    else:
        error = result.get('error', 'unknown')
        if error == 'missing_fields':
            return "missing fields", 400
        elif error == 'invalid_plan':
            return "invalid plan", 400
        elif error == 'plan_not_found':
            return "plan not found", 404
        return "error", 500


def _extract_custom_field(custom_fields, key):
    for field in custom_fields:
        if field.get("key") == key:
            text = field.get("text", {})
            if isinstance(text, dict):
                return text.get("value", "").strip()
            return str(text).strip() if text else ""
    return ""


def _extract_company_name(custom_fields, metadata):
    value = _extract_custom_field(custom_fields, 'company_name')
    return value if value else metadata.get("company_name", "")


def _parse_name(full_name):
    if not full_name:
        return "", ""
    parts = full_name.strip().split(" ", 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""
    return first_name, last_name


def _ensure_user_company_relation(company, email):
    user = User.query.filter_by(email=email).first()
    if not user:
        return
    existing_relation = UserCompany.query.filter_by(
        user_id=user.id, company_id=company.id
    ).first()
    if not existing_relation:
        uc = UserCompany()
        uc.user_id = user.id
        uc.company_id = company.id
        uc.role = 'super_admin'
        uc.is_active = True
        uc.created_at = datetime.utcnow()
        db.session.add(uc)
        logger.info(f"Created missing UserCompany for user {user.id} and company {company.id}")


def _check_trial_eligibility(user):
    user_companies = UserCompany.query.filter_by(user_id=user.id).all()
    for uc in user_companies:
        company = Company.query.get(uc.company_id)
        if company and (company.stripe_customer_id or company.stripe_subscription_id):
            logger.info(f"User {user.email} NOT eligible for trial (existing company {company.id} has Stripe data)")
            return False
    return True


def _cancel_trial_on_subscription(subscription_id):
    try:
        stripe.Subscription.modify(
            subscription_id,
            trial_end='now'
        )
        logger.info(f"Trial cancelled on subscription {subscription_id} (existing user, not eligible)")
    except Exception as e:
        logger.error(f"Error cancelling trial on subscription {subscription_id}: {e}")


def _log_audit(company_id, event_id, details):
    try:
        audit = SubscriptionAuditLog(
            company_id=company_id,
            event_type='onboarding_checkout_completed',
            stripe_event_id=event_id,
            before_json={},
            after_json=details,
            created_at=datetime.utcnow()
        )
        db.session.add(audit)
    except Exception as e:
        logger.error(f"Error logging audit: {e}")
