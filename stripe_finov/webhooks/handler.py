"""
REFONTE WEBHOOKS STRIPE - Gestionnaire unique
Gère les 5 événements maîtres avec filtrage et idempotence
"""

from flask import Blueprint, request
import os
import stripe
from app import db
from models import Company, SubscriptionAuditLog, User, UserCompany, Plan
from datetime import datetime, timedelta
import logging
from .helpers import _get_stripe_items_safely, get_item_quantity, get_item_price_id

STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

# Blueprint
webhook_blueprint = Blueprint("stripe_webhook", __name__)

# Logger
logger = logging.getLogger(__name__)

def is_idempotent(event_id):
    """Vérifier si l'événement a déjà été traité"""
    return db.session.query(SubscriptionAuditLog.id)\
        .filter(SubscriptionAuditLog.stripe_event_id == event_id).first() is not None

def create_idempotency_record(event_id, event_type):
    """Créer un enregistrement d'idempotence AVANT le traitement"""
    try:
        audit = SubscriptionAuditLog()
        audit.company_id = None  # Sera mis à jour après
        audit.event_type = event_type
        audit.stripe_event_id = event_id
        audit.before_json = {"processing": True}
        audit.after_json = {}
        db.session.add(audit)
        db.session.commit()
        return audit
    except Exception as e:
        db.session.rollback()
        # Si l'enregistrement existe déjà, c'est qu'on a déjà traité cet événement
        return None

def log_audit(company_id, event_type, event_id, before_dict, after_dict):
    """Enregistrer l'audit de l'événement - METTRE À JOUR l'enregistrement d'idempotence existant"""
    # Chercher l'enregistrement d'idempotence existant
    audit = db.session.query(SubscriptionAuditLog).filter_by(stripe_event_id=event_id).first()

    if audit:
        # METTRE À JOUR l'enregistrement existant (créé par create_idempotency_record)
        audit.company_id = company_id
        audit.event_type = event_type
        audit.before_json = before_dict
        audit.after_json = after_dict
        logger.debug(f"Updated existing audit record for event {event_id}")
    else:
        # Fallback : créer un nouvel enregistrement si aucun n'existe (ne devrait pas arriver)
        audit = SubscriptionAuditLog()
        audit.company_id = company_id
        audit.event_type = event_type
        audit.stripe_event_id = event_id
        audit.before_json = before_dict
        audit.after_json = after_dict
        db.session.add(audit)
        logger.warning(f"Created new audit record for event {event_id} (idempotency record missing)")

def find_company_by_customer(customer_id):
    """Trouver une entreprise par son customer ID Stripe"""
    if not customer_id:
        return None
    return Company.query.filter_by(stripe_customer_id=customer_id).first()

def auto_convert_excess_users_to_reader(company, target_quantity):
    """Convertir automatiquement les utilisateurs excédentaires en lecteurs"""
    users = company.active_users_excluding_super_admin()
    over = max(0, len(users) - target_quantity)
    if over <= 0:
        return []

    # Tri du plus récent au plus ancien
    users_sorted = sorted(users, key=lambda u: u.created_at, reverse=True)
    converted = []

    for u in users_sorted[:over]:
        # Trouver la relation UserCompany
        uc = UserCompany.query.filter_by(user_id=u.id, company_id=company.id).first()
        if uc and uc.role != 'super_admin':
            uc.role = 'lecteur'
            converted.append(u.id)

    return converted

def apply_cancel_to_free(company):
    """Appliquer l'annulation immédiate - passage au plan Découverte"""
    from models import Plan

    # Trouver le plan gratuit "decouverte" (nom technique, pas display_name)
    free_plan = Plan.query.filter_by(name='decouverte').first()
    if free_plan:
        company.plan_id = free_plan.id
        company.plan = 'decouverte'  # CORRECTION: Synchroniser le champ obsolète 'plan'

    # Conservation du super admin uniquement
    # REFONTE: Utiliser plan_id comme source unique - ne plus mettre à jour company.plan
    company.quantity_licenses = 1
    company.status = 'active'  # Retour au statut actif avec plan gratuit
    company.is_free_account = True  # 🔧 CORRECTION: Marquer comme compte gratuit
    company.stripe_subscription_id = None  # Plus d'abonnement Stripe
    company.cancel_at = None
    company.pending_plan = None
    company.pending_quantity = None
    company.pending_expires_at = None

    super_admin = company.get_super_admin()

    # Désactiver tous les utilisateurs sauf le super admin
    for uc in company.user_companies:
        if uc.user_id != super_admin.id if super_admin else False:
            uc.is_active = False

def apply_downgrade(company, plan=None, quantity=None):
    """Appliquer un downgrade (immédiat ou différé)"""
    from models import Plan

    # REFONTE: Utiliser plan_id au lieu de champs string legacy
    # target_plan = plan or company.pending_plan or company.plan
    target_qty = quantity if quantity is not None else (company.pending_quantity or company.quantity_licenses)

    # Conversion des excédentaires en lecteur
    auto_convert_excess_users_to_reader(company, target_qty)

    # REFONTE: Utiliser plan_id comme source unique - ne plus mettre à jour company.plan
    company.quantity_licenses = target_qty
    company.status = 'active'

    # Déterminer le plan cible : paramètre explicite > pending_plan > fallback découverte
    target_plan_name = plan or company.pending_plan
    if target_plan_name:
        target_plan_obj = Plan.query.filter_by(name=target_plan_name).first()
    else:
        target_plan_obj = None

    if target_plan_obj:
        company.plan_id = target_plan_obj.id
        company.plan = target_plan_obj.name
        company.is_free_account = target_plan_obj.is_free
    else:
        # Fallback : plan découverte (gratuit)
        free_plan = Plan.query.filter_by(name='decouverte').first()
        if free_plan:
            company.plan_id = free_plan.id
            company.plan = 'decouverte'
        company.is_free_account = True

    company.pending_plan = None
    company.pending_quantity = None
    company.pending_expires_at = None

@webhook_blueprint.route("/stripe/unified/webhook", methods=["POST"])
def webhook():
    """Point d'entrée unique pour tous les webhooks Stripe"""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.error(f"Invalid signature: {e}")
        return "invalid signature", 400

    # Vérifier l'idempotence
    if is_idempotent(event["id"]):
        logger.info(f"Event {event['id']} already processed - skipping")
        return "ok", 200

    # Créer l'enregistrement d'idempotence AVANT tout traitement
    etype = event["type"]
    audit = create_idempotency_record(event["id"], etype)
    if not audit:
        # Déjà traité (race condition)
        return "ok", 200

    obj = event["data"]["object"]

    # 1) Checkout : exception (peut lier un nouveau client)
    if etype == "checkout.session.completed":
        return handle_checkout_completed(event)

    # 2) Déterminer le customer_id selon le type d'événement
    if etype.startswith("subscription_schedule."):
        # 🎯 CORRECTION : Stratégies multiples pour trouver la compagnie
        company = None
        customer_id = None

        # Stratégie 1 : Via subscription_id dans l'objet schedule
        subscription_id = obj.get("subscription")
        if subscription_id:
            company = Company.query.filter_by(stripe_subscription_id=subscription_id).first()
            logger.debug(f"Schedule event: Searched by subscription_id '{subscription_id}' → {'Found' if company else 'Not found'}")

        # Stratégie 2 : Via customer dans l'objet schedule
        if not company:
            customer_id = obj.get("customer")
            if customer_id:
                company = find_company_by_customer(customer_id)
                logger.debug(f"Schedule event: Searched by customer_id '{customer_id}' → {'Found' if company else 'Not found'}")

        # Stratégie 3 : Via phases du schedule (regarder dans les items)
        if not company and obj.get("phases"):
            for phase in obj["phases"]:
                if phase.get("items"):
                    for item in phase["items"]:
                        price_data = item.get("price")
                        # price_data peut être un string (ID) ou un dict (objet étendu)
                        if price_data and isinstance(price_data, dict) and price_data.get("product"):
                            # Chercher par product_id (fallback)
                            logger.debug(f"Schedule event: Found product in phase: {price_data['product']}")
                            break
    else:
        # Pour les autres événements, customer_id est direct
        customer_id = obj.get("customer")
        company = find_company_by_customer(customer_id)

    # 3) Filtrage global : ignorer client inconnu
    if not company:
        logger.info(f"Ignoring event {etype} for unknown customer/subscription")
        return "ok", 200

    # Gestionnaires d'événements
    handlers = {
        "customer.subscription.created": handle_subscription_created,
        "customer.subscription.updated": handle_subscription_updated,
        "invoice.payment_succeeded": handle_payment_succeeded,
        "invoice.payment_action_required": handle_payment_action_required,
        # 🎯 NOUVEAUX : Gestion des subscription schedules
        "subscription_schedule.created": handle_subscription_schedule_created,
        "subscription_schedule.updated": handle_subscription_schedule_updated,
        "subscription_schedule.canceled": handle_subscription_schedule_canceled,
    }

    handler = handlers.get(etype)
    if not handler:
        logger.debug(f"Event {etype} not handled - ignored")
        # Mettre à jour l'audit pour indiquer que l'événement a été ignoré
        audit.after_json = {"ignored": True, "reason": "no_handler"}
        db.session.commit()
        return "ok", 200

    try:
        result = handler(event, company)
        # L'audit est déjà mis à jour par le handler via log_audit()
        # Ne pas écraser with {"success": True} pour préserver les détails
        db.session.commit()
        return result
    except Exception as e:
        logger.error(f"Error processing {etype}: {e}")
        db.session.rollback()
        # Marquer l'audit comme échoué
        try:
            audit.after_json = {"error": str(e)}
            db.session.commit()
        except Exception:
            pass
        return "internal error", 500

def handle_checkout_completed(event):
    """Gérer checkout.session.completed - liaison initiale OU création nouvelle entreprise"""
    session = event["data"]["object"]
    event_id = event["id"]
    metadata = session.get("metadata", {})

    # Extraire les informations pour abonnements
    registration_type = metadata.get("registration_type")

    if registration_type == "stripe_onboarding":
        from stripe_finov.webhooks.onboarding_handler import handle_onboarding_checkout_completed
        logger.info(f"Routing to onboarding handler for event {event_id}")
        return handle_onboarding_checkout_completed(event)
    company_id = metadata.get("company_id")
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")

    # CORRECTION CRITIQUE : Toujours vérifier d'abord si une compagnie existe
    # Priorité 1 : company_id dans metadata
    if company_id:
        company = Company.query.get(company_id)
        if company:
            logger.info(f"Updating existing company {company_id} from checkout")
            # Continuer avec la mise à jour normale ci-dessous
        else:
            logger.error(f"Company {company_id} not found for checkout session")
            return "company not found", 404
    # Priorité 2 : recherche par stripe_customer_id
    elif customer_id:
        company = Company.query.filter_by(stripe_customer_id=customer_id).first()
        if company:
            logger.info(f"Found existing company {company.id} by customer_id {customer_id}")
            company_id = company.id
        # Priorité 3 : recherche par stripe_subscription_id si disponible
        elif subscription_id:
            company = Company.query.filter_by(stripe_subscription_id=subscription_id).first()
            if company:
                logger.info(f"Found existing company {company.id} by subscription_id {subscription_id}")
                company_id = company.id

    # Si aucune compagnie trouvée ET registration_type indique nouvelle compagnie
    if not company_id and registration_type in ["new_company", "new_company_authenticated"]:
        logger.info(f"Processing new company registration: {registration_type}")
        return handle_new_company_registration(session, event_id, registration_type)

    # Si toujours pas de compagnie, c'est une session orpheline
    if not company_id:
        logger.warning(f"Orphan checkout session {session['id']} - no company found or created")
        log_audit(None, "checkout.session.completed", event_id,
                 {"orphan": True}, {"session_id": session["id"], "customer": customer_id})
        db.session.commit()
        return "ok", 200

    # Récupérer l'entreprise
    company = Company.query.get(company_id)
    if not company:
        logger.error(f"Company {company_id} not found for checkout session")
        return "company not found", 404

    before_state = company.to_dict()

    # Mise à jour des IDs Stripe
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")

    # Si pas de subscription_id direct, le récupérer
    if not subscription_id and customer_id:
        subscriptions = stripe.Subscription.list(customer=customer_id, limit=1)
        if subscriptions.data:
            subscription_id = subscriptions.data[0].id

    company.stripe_customer_id = customer_id
    company.stripe_subscription_id = subscription_id

    # Récupérer les détails de l'abonnement
    if subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(
                subscription_id,
                expand=["latest_invoice.payment_intent", "items.data.price.product"]
            )

            # Mise à jour du statut et de la quantité
            company.status = subscription.status
            items = _get_stripe_items_safely(subscription)
            if items:
                quantity = get_item_quantity(items[0])
                company.quantity_licenses = quantity

                # 🔧 CORRECTION CRITIQUE: Marquer comme compte payant après checkout réussi
                company.is_free_account = False
                logger.info(f"✅ Company {company.id} marked as paid account (is_free_account=False)")

                # Mettre à jour plan_id si disponible dans les métadonnées
                from models import Plan
                plan_id_meta = metadata.get('plan_id')
                if plan_id_meta:
                    try:
                        plan_obj = Plan.query.get(int(plan_id_meta))
                        if plan_obj:
                            company.plan_id = plan_obj.id
                            company.plan = plan_obj.name  # CORRECTION: Synchroniser le champ obsolète 'plan'
                            company.plan_status = 'active'  # 🔧 CORRECTION: Activer le statut du plan
                            logger.info(f"✅ Company {company.id} plan updated to {plan_obj.name} (plan_id={plan_obj.id}, plan_status=active)")
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid plan_id in metadata: {plan_id_meta}")

            # Gérer cancel_at si présent
            if subscription.cancel_at:
                company.cancel_at = datetime.fromtimestamp(subscription.cancel_at)

        except Exception as e:
            logger.error(f"Error retrieving subscription {subscription_id}: {e}")

    # Log audit
    log_audit(company.id, "checkout.session.completed", event_id,
             before_state, company.to_dict())

    db.session.commit()
    logger.info(f"Checkout completed for company {company.id}")
    return "ok", 200

def handle_new_company_registration(session, event_id, registration_type):
    """Gérer la création d'une nouvelle entreprise lors du checkout"""
    metadata = session.get("metadata", {})

    try:
        # Extraire les informations de base
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")
        company_name = metadata.get("company_name")
        user_email = metadata.get("user_email")

        if not customer_id or not company_name or not user_email:
            logger.error(f"Missing required fields for new company: customer={customer_id}, company={company_name}, email={user_email}")
            return "missing fields", 400

        # IDEMPOTENCE RENFORCÉE : Vérifier par customer_id ET subscription_id
        existing_company = Company.query.filter_by(stripe_customer_id=customer_id).first()
        if not existing_company and subscription_id:
            existing_company = Company.query.filter_by(stripe_subscription_id=subscription_id).first()

        if existing_company:
            logger.info(f"Company already exists for customer {customer_id}, checking UserCompany relation")

            # Vérifier si la relation UserCompany existe et la créer si nécessaire
            user = User.query.filter_by(email=user_email).first()
            if user:
                existing_relation = UserCompany.query.filter_by(
                    user_id=user.id,
                    company_id=existing_company.id
                ).first()

                if not existing_relation:
                    logger.info(f"Creating missing UserCompany relation for {user.email} and company {existing_company.id}")
                    user_company = UserCompany()
                    user_company.user_id = user.id
                    user_company.company_id = existing_company.id
                    user_company.role = 'super_admin'
                    user_company.is_active = True
                    user_company.created_at = datetime.utcnow()
                    db.session.add(user_company)

            # Mettre à jour l'audit avec l'entreprise existante
            audit = SubscriptionAuditLog.query.filter_by(stripe_event_id=event_id).first()
            if audit:
                audit.company_id = existing_company.id
                audit.after_json = {"idempotent": True, "existing_company_id": existing_company.id}
                db.session.commit()
            return "ok", 200

        logger.info(f"Creating new company: {company_name} for {user_email}")

        # Récupérer les détails de l'abonnement Stripe
        subscription = stripe.Subscription.retrieve(
            subscription_id,
            expand=["latest_invoice.payment_intent", "items.data.price.product"]
        ) if subscription_id else None

        # Déterminer le plan et la quantité depuis les métadonnées ET l'abonnement
        plan_id_from_meta = metadata.get('plan_id')
        quantity_from_meta = metadata.get('quantity_licenses', '1')
        quantity = 1

        # Priorité 1: Utiliser plan_id et quantity des métadonnées
        if plan_id_from_meta:
            try:
                plan_id = int(plan_id_from_meta)
            except (ValueError, TypeError):
                plan_id = None
                logger.warning(f"Invalid plan_id in metadata: {plan_id_from_meta}")
        else:
            plan_id = None

        if quantity_from_meta:
            try:
                quantity = int(quantity_from_meta)
            except (ValueError, TypeError):
                quantity = 1

        # Priorité 2: Récupérer la quantité depuis l'abonnement si disponible
        if subscription and hasattr(subscription, 'items') and subscription.items and hasattr(subscription.items, 'data'):
            items = _get_stripe_items_safely(subscription)
            if items:
                quantity = get_item_quantity(items[0])

        # Créer l'entreprise
        company = Company()
        company.name = company_name
        company.email = user_email

        # 🔧 CORRECTION: Utiliser le plan_id des métadonnées
        if plan_id:
            plan_obj = Plan.query.get(plan_id)
            if plan_obj:
                company.plan_id = plan_obj.id
                company.plan = plan_obj.name
                company.plan_status = 'active'
                company.is_free_account = plan_obj.is_free
                logger.info(f"✅ New company plan set to {plan_obj.name} (plan_id={plan_obj.id})")
            else:
                logger.warning(f"Plan {plan_id} not found, using default")
                default_plan = Plan.query.filter_by(name='decouverte').first()
                if default_plan:
                    company.plan_id = default_plan.id
                    company.plan = 'decouverte'
                    company.plan_status = 'active'
                    company.is_free_account = default_plan.is_free
        else:
            # Fallback: plan gratuit par défaut
            default_plan = Plan.query.filter_by(name='decouverte').first()
            if default_plan:
                company.plan_id = default_plan.id
                company.plan = 'decouverte'
                company.plan_status = 'active'
                company.is_free_account = default_plan.is_free

        company.quantity_licenses = quantity
        company.status = subscription.status if subscription else "active"
        company.stripe_customer_id = customer_id
        company.stripe_subscription_id = subscription_id
        company.created_at = datetime.utcnow()
        company.created_by_user_id = None  # Sera défini après avoir trouvé l'utilisateur

        db.session.add(company)
        db.session.flush()  # Pour obtenir l'ID de l'entreprise

        # Dans TOUS les cas (new_company ET new_company_authenticated),
        # l'utilisateur existe déjà - il faut juste le trouver
        user = User.query.filter_by(email=user_email).first()
        if not user:
            logger.error(f"User {user_email} not found for registration_type: {registration_type}")
            # Rollback et mettre à jour l'audit avec l'erreur
            db.session.rollback()
            audit = SubscriptionAuditLog.query.filter_by(stripe_event_id=event_id).first()
            if audit:
                audit.after_json = {
                    "error": True,
                    "registration_type": registration_type,
                    "error_message": f"User {user_email} not found"
                }
                db.session.commit()
            return "user not found", 404

        logger.info(f"Found existing user: {user.first_name} {user.last_name} ({user.email})")

        # Vérifier que l'utilisateur a un ID avant de créer la relation
        if not user or not user.id:
            logger.error(f"User ID is None after creation/lookup for {user_email}")
            return "user creation failed", 500

        # 🔧 CORRECTION: Définir le créateur de l'entreprise
        company.created_by_user_id = user.id

        # Créer la relation UserCompany avec le rôle super_admin
        user_company = UserCompany()
        user_company.user_id = user.id
        user_company.company_id = company.id
        user_company.role = 'super_admin'
        user_company.is_active = True
        user_company.created_at = datetime.utcnow()
        db.session.add(user_company)

        # Mettre à jour l'audit pré-créé avec les résultats
        audit = SubscriptionAuditLog.query.filter_by(stripe_event_id=event_id).first()
        if audit:
            audit.company_id = company.id
            audit.after_json = {
                "success": True,
                "registration_type": registration_type,
                "company_id": company.id,
                "company_name": company.name,
                "user_email": user.email,
                "plan": company.plan,
                "quantity": quantity
            }

        db.session.commit()
        logger.info(f"✅ New company created successfully: {company.name} (ID: {company.id}) for user {user.email}")
        return "ok", 200

    except Exception as e:
        logger.error(f"Error creating new company: {e}")
        db.session.rollback()
        # Mettre à jour l'audit pré-créé avec l'erreur
        try:
            audit = SubscriptionAuditLog.query.filter_by(stripe_event_id=event_id).first()
            if audit:
                audit.after_json = {
                    "error": True,
                    "registration_type": registration_type,
                    "error_message": str(e)
                }
                db.session.commit()
        except Exception:
            pass
        return "internal error", 500

# Fonction supprimée : create_new_user_from_metadata
# L'utilisateur existe déjà dans TOUS les cas (new_company ET new_company_authenticated)
# car il a été créé lors du processus d'inscription initial

def handle_subscription_created(event, company):
    """Gérer customer.subscription.created - création/réactivation"""
    subscription = event["data"]["object"]
    event_id = event["id"]

    before_state = company.to_dict()

    # Mise à jour du statut et de la quantité
    company.status = subscription["status"]
    items = _get_stripe_items_safely(subscription)
    if items:
        quantity = get_item_quantity(items[0])
        company.quantity_licenses = quantity

    # Réinitialiser les pending
    company.pending_plan = None
    company.pending_quantity = None
    company.pending_expires_at = None
    company.cancel_at = None

    # Log audit
    log_audit(company.id, "customer.subscription.created", event_id,
             before_state, company.to_dict())

    db.session.commit()
    logger.info(f"Subscription created for company {company.id}")
    return "ok", 200

def handle_subscription_updated(event, company):
    """Gérer customer.subscription.updated - ÉVÉNEMENT MAÎTRE pour tous les changements"""
    subscription = event["data"]["object"]
    event_id = event["id"]

    before_state = company.to_dict()
    old_quantity = company.quantity_licenses or 1

    # Extraire les informations clés
    status = subscription["status"]
    cancel_at_period_end = subscription.get("cancel_at_period_end", False)
    cancel_at = subscription.get("cancel_at")
    pending_update = subscription.get("pending_update")
    schedule_id = subscription.get("schedule")
    items = _get_stripe_items_safely(subscription)
    new_quantity = get_item_quantity(items[0]) if items else 1

    # 🎯 NOUVELLE LOGIQUE : Détecter les changements de plan via previous_attributes
    previous_attributes = event["data"].get("previous_attributes", {})
    plan_changed = False

    if previous_attributes:
        # Détecter changement de plan (upgrade/downgrade immédiat)
        old_plan_price_id = None
        new_plan_price_id = None

        # CORRECTION CRITIQUE : Utiliser subscription.items au lieu de subscription.plan (obsolète)
        # Ancien plan dans previous_attributes.items
        if "items" in previous_attributes:
            old_items = previous_attributes.get("items", {}).get("data", [])
            if old_items:
                old_price = old_items[0].get("price", {})
                old_plan_price_id = old_price.get("id") if isinstance(old_price, dict) else None

        # Nouveau plan dans subscription.items actuel
        if items:
            new_plan_price_id = get_item_price_id(items[0])

        # Si les price_id sont différents, c'est un changement de plan
        if old_plan_price_id and new_plan_price_id and old_plan_price_id != new_plan_price_id:
            plan_changed = True
            logger.info(f"🔄 Plan change detected for company {company.id}: {old_plan_price_id} → {new_plan_price_id}")

            # Chercher le nouveau plan dans notre base de données
            from models import Plan
            new_plan = Plan.query.filter_by(stripe_price_id=new_plan_price_id).first()

            if new_plan:
                old_plan_id = company.plan_id
                company.plan_id = new_plan.id
                company.plan = new_plan.name  # CORRECTION: Synchroniser le champ obsolète 'plan' avec plan_id
                logger.info(f"✅ Company {company.id} plan updated: plan_id {old_plan_id} → {new_plan.id} ({new_plan.name})")

                # CRITIQUE : Mettre à jour les métadonnées Stripe avec les bonnes valeurs
                try:
                    import stripe
                    stripe.Subscription.modify(
                        subscription["id"],
                        metadata={
                            'company_id': str(company.id),
                            'plan_id': str(new_plan.id),  # ✅ Mettre à jour avec le BON plan_id
                            'quantity_licenses': str(new_quantity)
                        }
                    )
                    logger.info(f"✅ Métadonnées Stripe mises à jour pour company {company.id}: plan_id={new_plan.id}")
                except Exception as meta_error:
                    logger.warning(f"⚠️ Impossible de mettre à jour métadonnées Stripe: {meta_error}")
            else:
                logger.warning(f"❌ New plan not found in database for price_id: {new_plan_price_id}")

    # 🎯 LOGIQUE EXISTANTE : Détecter les subscription schedules
    schedule_changes = None
    if schedule_id:
        schedule_changes = _get_schedule_changes(schedule_id, subscription)
        if schedule_changes:
            logger.info(f"Subscription schedule detected for company {company.id}: {schedule_changes}")

    # A) Annulation différée
    if cancel_at_period_end and cancel_at:
        company.status = "pending_cancellation"
        company.cancel_at = datetime.fromtimestamp(cancel_at)
        logger.info(f"Pending cancellation for company {company.id} at {company.cancel_at}")

    # B) Annulation immédiate
    elif status == "canceled":
        apply_cancel_to_free(company)
        logger.info(f"Immediate cancellation for company {company.id} - moved to free plan")

    # C) Downgrade différé via pending_update
    elif pending_update:
        company.status = "pending_downgrade"
        if pending_update.get("subscription_items"):
            # Extraire la quantité cible
            for item in pending_update["subscription_items"]:
                if "quantity" in item:
                    company.pending_quantity = item["quantity"]

        if pending_update.get("expires_at"):
            company.pending_expires_at = datetime.fromtimestamp(pending_update["expires_at"])

        logger.info(f"Pending downgrade for company {company.id} via pending_update")

    # C2) 🎯 NOUVEAU : Downgrade différé via subscription schedule
    elif schedule_changes and schedule_changes.get("is_downgrade"):
        company.status = "pending_downgrade"
        company.pending_quantity = schedule_changes.get("target_quantity")

        # Utiliser la date du schedule ou current_period_end
        if schedule_changes.get("effective_date"):
            company.pending_expires_at = schedule_changes["effective_date"]
        elif subscription.get("current_period_end"):
            company.pending_expires_at = datetime.fromtimestamp(subscription["current_period_end"])

        logger.info(f"Pending downgrade for company {company.id} via subscription schedule: {old_quantity} -> {company.pending_quantity}")

    # D) Réduction de licences directe → TOUJOURS traiter comme downgrade différé pour routine cron
    elif new_quantity < old_quantity:
        # Configurer le downgrade différé pour la routine cron
        company.status = "pending_downgrade"
        company.pending_quantity = new_quantity

        # Utiliser la fin du cycle Stripe (current_period_end) ou calculer fin de mois
        current_period_end = subscription.get("current_period_end")
        if current_period_end:
            company.pending_expires_at = datetime.fromtimestamp(current_period_end)
        else:
            # Fallback : calculer vraie fin de mois (datetime/timedelta déjà importés en haut du fichier)
            now = datetime.utcnow()
            next_month = now.replace(day=28) + timedelta(days=4)
            company.pending_expires_at = next_month - timedelta(days=next_month.day)

        logger.info(f"License decrease scheduled for company {company.id}: {old_quantity} -> {new_quantity} (will apply on {company.pending_expires_at})")

    # E) Augmentation de licences
    elif new_quantity > old_quantity:
        company.quantity_licenses = new_quantity
        logger.info(f"License increase for company {company.id}: {old_quantity} -> {new_quantity}")

        # CRITIQUE : Mettre à jour les métadonnées Stripe lors d'augmentation de licences
        try:
            import stripe
            stripe.Subscription.modify(
                subscription["id"],
                metadata={
                    'company_id': str(company.id),
                    'plan_id': str(company.plan_id) if company.plan_id else '0',
                    'quantity_licenses': str(new_quantity)
                }
            )
            logger.info(f"✅ Métadonnées Stripe mises à jour (augmentation licences): quantity={new_quantity}")
        except Exception as meta_error:
            logger.warning(f"⚠️ Impossible de mettre à jour métadonnées Stripe: {meta_error}")

    # F) Mise à jour du statut - MAIS préserver les statuts pending_*
    if status in ["active", "trialing", "past_due", "unpaid", "expired"] and company.status not in ["pending_downgrade", "pending_cancellation"]:
        company.status = status
        logger.info(f"Status update for company {company.id}: {status}")

    # Log audit
    log_audit(company.id, "customer.subscription.updated", event_id,
             before_state, company.to_dict())

    db.session.commit()

    # Invalider le cache Stripe pour cette entreprise
    try:
        from stripe_integration import invalidate_stripe_cache
        invalidate_stripe_cache(company.id)
    except Exception:
        pass  # Non bloquant

    return "ok", 200

def _get_schedule_changes(schedule_id, current_subscription):
    """
    Retrieve scheduled changes from a subscription schedule.

    Args:
        schedule_id: The subscription schedule ID
        current_subscription: The current subscription object

    Returns:
        dict with is_downgrade, target_quantity, effective_date, schedule_id or None
    """
    import stripe

    try:
        logger.info(f"Fetching subscription schedule: {schedule_id}")

        schedule = stripe.SubscriptionSchedule.retrieve(schedule_id)
        current_quantity = current_subscription.get("quantity", 1)

        result = _analyze_schedule_phases(schedule, current_quantity)
        if result:
            result["schedule_id"] = schedule_id
        return result

    except stripe.StripeError as e:
        logger.error(f"Stripe API error fetching schedule {schedule_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing subscription schedule {schedule_id}: {e}")
        return None

def handle_payment_succeeded(event, company):
    """Gérer invoice.payment_succeeded - paiement réussi"""
    invoice = event["data"]["object"]
    event_id = event["id"]

    # Log audit simplement (pas de changement de plan/licences ici)
    log_audit(company.id, "invoice.payment_succeeded", event_id,
             {"payment_status": "pending"}, {"payment_status": "succeeded"})

    db.session.commit()

    # Invalider le cache Stripe pour cette entreprise
    try:
        from stripe_integration import invalidate_stripe_cache
        invalidate_stripe_cache(company.id)
    except Exception:
        pass

    logger.info(f"Payment succeeded for company {company.id}")
    return "ok", 200

def handle_payment_action_required(event, company):
    """Gérer invoice.payment_action_required - action requise (3DS/carte)"""
    event_id = event["id"]

    before_state = company.to_dict()

    # Mettre le statut à past_due
    company.status = "past_due"

    # Log audit
    log_audit(company.id, "invoice.payment_action_required", event_id,
             before_state, company.to_dict())

    db.session.commit()
    logger.info(f"Payment action required for company {company.id} - status set to past_due")

    # TODO: Envoyer une notification au super admin pour mettre à jour la carte via Customer Portal

    return "ok", 200

# =============================================================================
# 🎯 NOUVEAUX HANDLERS : SUBSCRIPTION SCHEDULES
# =============================================================================

def handle_subscription_schedule_created(event, company):
    """
    Gérer subscription_schedule.created - Schedule de diminution/downgrade créé

    Ce handler détecte quand un client planifie une diminution de licences
    et configure le pending_downgrade approprié.
    """
    schedule = event["data"]["object"]
    event_id = event["id"]

    before_state = company.to_dict()

    try:
        logger.info(f"🗓️ Processing subscription schedule created for company {company.id}")

        # Analyser les phases du schedule pour détecter une diminution
        current_quantity = company.quantity_licenses or 1
        schedule_changes = _analyze_schedule_phases(schedule, current_quantity)

        if schedule_changes and schedule_changes.get("is_downgrade"):
            # Configuration du pending downgrade
            company.status = "pending_downgrade"
            company.pending_quantity = schedule_changes.get("target_quantity")

            # Date d'application du changement
            if schedule_changes.get("effective_date"):
                company.pending_expires_at = schedule_changes["effective_date"]

            logger.info(f"✅ Pending downgrade scheduled for company {company.id}: {current_quantity} -> {company.pending_quantity} on {company.pending_expires_at}")
        else:
            logger.info(f"ℹ️  Schedule created but no downgrade detected for company {company.id}")

        # Log audit
        log_audit(company.id, "subscription_schedule.created", event_id,
                 before_state, company.to_dict())

        db.session.commit()
        return "ok", 200

    except Exception as e:
        logger.error(f"Error processing subscription_schedule.created for company {company.id}: {e}")
        db.session.rollback()
        if isinstance(e, (ValueError, AttributeError, KeyError)):
            logger.warning(f"Data error in subscription_schedule.created, acknowledging: {e}")
            return "ok", 200
        raise e

def handle_subscription_schedule_updated(event, company):
    """
    Gérer subscription_schedule.updated - Schedule de diminution/downgrade mis à jour

    Ce handler détecte les changements dans un schedule existant
    et met à jour les informations de downgrade en conséquence.
    """
    schedule = event["data"]["object"]
    event_id = event["id"]

    before_state = company.to_dict()

    try:
        logger.info(f"🔄 Processing subscription schedule updated for company {company.id}")

        # 🎯 AJOUT : Vérifier d'abord le statut du schedule et ses propriétés
        schedule_status = schedule.get("status", "unknown")
        schedule_subscription = schedule.get("subscription")
        schedule_released_at = schedule.get("released_at")

        logger.info(f"📋 Schedule details - status: '{schedule_status}', subscription: '{schedule_subscription}', released_at: '{schedule_released_at}' for company {company.id}")

        # 🎯 AMÉLIORATION COMPLÈTE : Détecter tous les cas d'annulation/terminaison de schedule
        # 1. Statuts explicites d'annulation/terminaison
        # 2. Schedule "released" (webhook 5 - annulation de downgrade)
        # 3. Schedule détaché de la subscription
        should_clean_pending = (
            schedule_status in ["canceled", "completed", "not_started", "released"] or
            (schedule_status == "released" and schedule_released_at is not None) or
            (schedule_subscription is None and schedule_released_at is not None)
        )

        if should_clean_pending:
            logger.info(f"🚫 Schedule termination detected (status: {schedule_status}) - cleaning pending downgrades...")

            if company.status == "pending_downgrade":
                old_pending_qty = company.pending_quantity
                company.status = "active"
                company.pending_quantity = None
                company.pending_expires_at = None
                logger.info(f"🧹 Cleared pending downgrade for company {company.id} (was going to {old_pending_qty}) due to schedule status: {schedule_status}")
            else:
                logger.info(f"ℹ️  No pending downgrade to clear for company {company.id} (current status: {company.status})")
        else:
            # Schedule actif, analyser les phases normalement
            current_quantity = company.quantity_licenses or 1
            schedule_changes = _analyze_schedule_phases(schedule, current_quantity)

            logger.info(f"🔍 Schedule analysis result: {schedule_changes}")

            if schedule_changes and schedule_changes.get("is_downgrade"):
                # Mise à jour du pending downgrade
                company.status = "pending_downgrade"
                company.pending_quantity = schedule_changes.get("target_quantity")

                # Date d'application du changement
                if schedule_changes.get("effective_date"):
                    company.pending_expires_at = schedule_changes["effective_date"]

                logger.info(f"✅ Pending downgrade updated for company {company.id}: {current_quantity} -> {company.pending_quantity} on {company.pending_expires_at}")
            else:
                # Plus de downgrade planifié, nettoyer les pending
                if company.status == "pending_downgrade":
                    company.status = "active"
                    company.pending_quantity = None
                    company.pending_expires_at = None
                    logger.info(f"🧹 Cleared pending downgrade for company {company.id} (no downgrade detected)")
                else:
                    logger.info(f"ℹ️  No changes needed for company {company.id}")

        # Log audit
        log_audit(company.id, "subscription_schedule.updated", event_id,
                 before_state, company.to_dict())

        db.session.commit()
        return "ok", 200

    except Exception as e:
        logger.error(f"Error processing subscription_schedule.updated for company {company.id}: {e}")
        db.session.rollback()
        if isinstance(e, (ValueError, AttributeError, KeyError)):
            logger.warning(f"Data error in subscription_schedule.updated, acknowledging: {e}")
            return "ok", 200
        raise e

def handle_subscription_schedule_canceled(event, company):
    """
    Gérer subscription_schedule.canceled - Schedule annulé (client annule sa diminution)

    Ce handler nettoie les pending downgrades quand un client annule
    sa diminution planifiée.
    """
    schedule = event["data"]["object"]
    event_id = event["id"]

    before_state = company.to_dict()

    try:
        logger.info(f"❌ Processing subscription schedule canceled for company {company.id}")

        # Nettoyer les pending downgrades si ils existent
        if company.status == "pending_downgrade":
            company.status = "active"
            old_pending_qty = company.pending_quantity
            company.pending_quantity = None
            company.pending_expires_at = None

            logger.info(f"🧹 Cleared pending downgrade for company {company.id} (was going to {old_pending_qty})")
        else:
            logger.info(f"ℹ️  Schedule canceled but no pending downgrade to clear for company {company.id}")

        # Log audit
        log_audit(company.id, "subscription_schedule.canceled", event_id,
                 before_state, company.to_dict())

        db.session.commit()
        return "ok", 200

    except Exception as e:
        logger.error(f"Error processing subscription_schedule.canceled for company {company.id}: {e}")
        db.session.rollback()
        if isinstance(e, (ValueError, AttributeError, KeyError)):
            logger.warning(f"Data error in subscription_schedule.canceled, acknowledging: {e}")
            return "ok", 200
        raise e

def _analyze_schedule_phases(schedule, current_quantity):
    """
    🎯 HELPER : Analyser les phases d'un subscription schedule

    Args:
        schedule: L'objet subscription schedule de Stripe
        current_quantity: La quantité actuelle de licences

    Returns:
        dict avec is_downgrade, target_quantity, effective_date ou None
    """
    try:
        if not schedule or not schedule.get("phases"):
            logger.warning(f"No phases found in subscription schedule")
            return None

        # Analyser les phases pour détecter les changements de quantité
        for phase in schedule["phases"]:
            if not phase.get("items"):
                continue

            # Vérifier chaque item dans la phase
            for item in phase["items"]:
                phase_quantity = item.get("quantity", 1)

                # Si quantité réduite détectée dans une phase future
                if phase_quantity < current_quantity:
                    effective_date = None
                    if phase.get("start_date"):
                        effective_date = datetime.fromtimestamp(phase["start_date"])

                    logger.info(f"✅ Schedule downgrade detected: {current_quantity} -> {phase_quantity} effective {effective_date}")

                    return {
                        "is_downgrade": True,
                        "target_quantity": phase_quantity,
                        "current_quantity": current_quantity,
                        "effective_date": effective_date
                    }

        logger.info(f"No quantity changes detected in subscription schedule")
        return None

    except Exception as e:
        logger.error(f"Error analyzing subscription schedule phases: {e}")
        return None