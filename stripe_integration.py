"""
REFONTE STRIPE 2.0 - Intégration simplifiée avec une seule licence
Architecture ultra-simplifiée : portail Stripe uniquement + synchronisation webhooks
"""
import os
import stripe
from datetime import datetime
from flask import current_app

def create_stripe_customer(company, user=None):
    """Create a Stripe customer for a company. Returns the Stripe Customer object.

    Args:
        company: Company model instance (must have .email, .name, .id)
        user: Optional User model instance for additional metadata
    """
    metadata = {
        'company_id': str(company.id),
        'company_name': company.name
    }
    if user:
        metadata['user_id'] = str(user.id)

    customer = stripe.Customer.create(
        email=company.email,
        name=company.name,
        metadata=metadata
    )
    return customer


def create_stripe_portal_session(company, return_url):
    """Créer une session portail Stripe - SEULE interface de gestion d'abonnement"""
    try:
        if not company.stripe_customer_id:
            current_app.logger.error(f"Aucun customer Stripe pour {company.name}")
            return None

        # Créer session avec configuration complète pour permettre toutes les actions
        session = stripe.billing_portal.Session.create(
            customer=company.stripe_customer_id,
            return_url=return_url,
            configuration=os.environ.get('STRIPE_BILLING_PORTAL_CONFIG_ID', 'bpc_1MsCUtCGTJ1mCmT6kIq5js3Q')
        )

        current_app.logger.info(f"Session portail créée pour {company.name}: {session.url}")
        return session.url

    except Exception as e:
        current_app.logger.error(f"Erreur création session portail pour {company.name}: {str(e)}")
        return None

def create_stripe_customer_and_subscription(company, plan, quantity_licenses=1):
    """Créer un customer et abonnement Stripe pour une nouvelle entreprise"""
    from app import db

    try:
        # Créer le customer Stripe
        customer = create_stripe_customer(company)

        is_eligible_for_trial = not company.stripe_subscription_id and not company.stripe_customer_id
        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{
                'price': plan.stripe_price_id,
                'quantity': quantity_licenses
            }],
            **({"trial_period_days": 14} if is_eligible_for_trial else {}),
            metadata={
                'company_id': company.id,
                'company_name': company.name
            }
        )

        # Mettre à jour la base de données
        company.stripe_customer_id = customer.id
        company.stripe_subscription_id = subscription.id
        company.plan_id = plan.id
        company.quantity_licenses = quantity_licenses
        company.subscription_status = subscription.status

        db.session.commit()

        current_app.logger.info(f"Abonnement Stripe créé pour {company.name}: {quantity_licenses} licences")
        return {
            'success': True,
            'customer_id': customer.id,
            'subscription_id': subscription.id
        }

    except Exception as e:
        current_app.logger.error(f"Erreur création abonnement Stripe pour {company.name}: {str(e)}")
        db.session.rollback()
        return {'success': False, 'error': str(e)}

def get_subscription_info(company):
    """Récupérer les informations d'abonnement depuis Stripe (pour affichage uniquement).
    Les résultats sont cachés 5 minutes par company_id pour réduire les appels API Stripe."""
    from app import cache

    try:
        if not company.stripe_subscription_id:
            return {
                'status': 'no_subscription',
                'plan_name': company.get_plan_display_name(),
                'quantity': company.quantity_licenses or 1,
                'cancel_at_period_end': False
            }

        # Vérifier le cache d'abord
        cache_key = f"stripe_sub_info:{company.id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)

        result = {
            'status': subscription.status,
            'plan_name': company.get_plan_display_name(),
            'quantity': company.quantity_licenses or 1,
            'cancel_at_period_end': subscription.cancel_at_period_end,
            'current_period_end': datetime.fromtimestamp(subscription.current_period_end) if subscription.current_period_end else None
        }

        # Cacher le résultat pour 5 minutes
        cache.set(cache_key, result, timeout=300)
        return result

    except Exception as e:
        current_app.logger.error(f"Erreur récupération info abonnement pour {company.name}: {str(e)}")
        return {
            'status': 'error',
            'plan_name': company.get_plan_display_name(),
            'quantity': company.quantity_licenses or 1,
            'cancel_at_period_end': False
        }


def invalidate_stripe_cache(company_id):
    """Invalider le cache Stripe pour une entreprise (appeler après webhook ou modification)."""
    from app import cache
    cache.delete(f"stripe_sub_info:{company_id}")
    cache.delete(f"stripe_prices_list")

def create_checkout_session_simple(company, plan, quantity_licenses, success_url, cancel_url):
    """Créer une session checkout simple pour nouvelle souscription ou changement"""
    try:
        is_eligible_for_trial = not company.stripe_customer_id and not company.stripe_subscription_id

        if not company.stripe_customer_id:
            customer = create_stripe_customer(company)
            company.stripe_customer_id = customer.id
            from app import db
            db.session.commit()
        session = stripe.checkout.Session.create(
            customer=company.stripe_customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price': plan.stripe_price_id,
                'quantity': quantity_licenses,
            }],
            mode='subscription',
            automatic_tax={'enabled': True},
            customer_update={
                'shipping': 'auto',
                'address': 'auto'
            },
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                'company_id': company.id,
                'company_name': company.name,
                'plan_id': str(plan.id),
                'quantity_licenses': str(quantity_licenses),
                'registration_type': 'plan_change'
            },
            subscription_data={
                **({'trial_period_days': 14} if is_eligible_for_trial else {}),
                'metadata': {
                    'company_id': company.id,
                    'company_name': company.name,
                    'plan_id': str(plan.id),
                    'quantity_licenses': str(quantity_licenses)
                }
            }
        )

        current_app.logger.info(f"Session checkout créée pour {company.name}: {quantity_licenses} licences du plan {plan.name}")
        return session.url

    except Exception as e:
        current_app.logger.error(f"Erreur création session checkout pour {company.name}: {str(e)}")
        return None

# Fonctions d'audit PHASE 2
def log_subscription_change(company, action_type, old_plan_id=None, new_plan_id=None, old_quantity=None, new_quantity=None, stripe_event_id=None, webhook_event_id=None, change_summary=None):
    """PHASE 2 : Logger les changements d'abonnement de façon complète"""
    from app import db
    from models import SubscriptionAuditLog

    try:
        log = SubscriptionAuditLog(
            company_id=company.id,
            action_type=action_type,
            old_plan_id=old_plan_id,
            new_plan_id=new_plan_id or company.plan_id,
            old_quantity=old_quantity,
            new_quantity=new_quantity,
            webhook_event_id=webhook_event_id or stripe_event_id,
            change_summary=change_summary,
            created_at=datetime.utcnow()
        )

        db.session.add(log)
        db.session.commit()

        current_app.logger.info(f"Changement abonnement V2 loggé pour {company.name}: {action_type} - {change_summary or 'sync'}")
        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur logging changement abonnement V2: {str(e)}")
        return False