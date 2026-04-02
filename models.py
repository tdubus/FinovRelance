from app import db
from flask_login import UserMixin
from datetime import datetime, date, timedelta
from sqlalchemy import event
from sqlalchemy.orm import validates
from flask import current_app, g
import stripe
import enum
from db_utils import safe_db_query


class Plan(db.Model):
    """Model for subscription plans - REFONTE STRIPE 2.0 : Architecture simplifiée avec une seule licence"""
    __tablename__ = 'plans'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)  # 'decouverte', 'relance', 'relance_plus'
    display_name = db.Column(db.String(100), nullable=False)  # 'Découverte', 'Relance', 'Relance+'
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    is_free = db.Column(db.Boolean, default=False)  # True for discovery plan

    # NOUVEAU - Architecture simplifiée avec une seule licence payante (IDs en clair)
    stripe_product_id = db.Column(db.String(255))  # Un seul produit Stripe - EN CLAIR
    stripe_price_id = db.Column(db.String(255))    # Un seul prix Stripe - EN CLAIR


    # Plan hierarchy and limits (conservé)
    plan_level = db.Column(db.Integer, unique=True, nullable=False)  # 1=découverte, 2=relance, 3=relance+, etc.
    max_clients = db.Column(db.Integer)  # NULL = unlimited, 10 for discovery
    daily_sync_limit = db.Column(db.Integer)  # NULL = unlimited, nombre de syncs par jour
    allows_email_sending = db.Column(db.Boolean, default=False)
    allows_email_connection = db.Column(db.Boolean, default=False)
    allows_accounting_connection = db.Column(db.Boolean, default=False)
    allows_team_management = db.Column(db.Boolean, default=False)
    allows_email_templates = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Plus besoin de propriétés avec chiffrement - les IDs Stripe sont publics

    def get_pricing_info(self):
        """REFONTE STRIPE 2.0 : Récupérer les informations de prix depuis Stripe avec gestion d'erreurs robuste"""
        try:
            import stripe
            from flask import current_app

            if not self.stripe_price_id:
                return {
                    'amount': None,
                    'currency': 'EUR',
                    'interval': 'month',
                    'currency_symbol': '€',
                    'interval_display': 'mois'
                }

            try:
                # Vérifier le cache d'abord (évite des appels Stripe API répétés)
                try:
                    from app import cache
                    cache_key = f"stripe_price:{self.stripe_price_id}"
                    cached = cache.get(cache_key)
                    if cached is not None:
                        return cached
                except Exception:
                    pass  # Si le cache n'est pas dispo, continuer sans

                # Récupération DIRECTE du prix depuis Stripe
                price = stripe.Price.retrieve(self.stripe_price_id)

                # Conversion devise
                currency_map = {
                    'eur': {'symbol': '€', 'name': 'EUR'},
                    'usd': {'symbol': '$', 'name': 'USD'},
                    'cad': {'symbol': 'CAD$', 'name': 'CAD'},
                    'gbp': {'symbol': '£', 'name': 'GBP'}
                }

                # Conversion intervalle
                interval_map = {
                    'day': 'jour',
                    'week': 'semaine',
                    'month': 'mois',
                    'year': 'année'
                }

                currency = price.currency.lower()
                currency_info = currency_map.get(currency, {'symbol': '€', 'name': 'EUR'})

                interval = price.recurring.interval if price.recurring else 'month'
                interval_display = interval_map.get(interval, 'mois')

                pricing_info = {
                    'amount': (price.unit_amount / 100) if price.unit_amount else 0,
                    'currency': currency_info['name'],
                    'currency_symbol': currency_info['symbol'],
                    'interval': interval,
                    'interval_display': interval_display,
                    'stripe_price_id': self.stripe_price_id,
                    'product_name': price.nickname or self.display_name
                }

                if current_app:
                    current_app.logger.info(f"Prix récupéré pour {self.display_name}: {pricing_info['amount']}{pricing_info['currency_symbol']} par {pricing_info['interval_display']}")

                # Mettre en cache pour 5 minutes
                try:
                    from app import cache
                    cache.set(f"stripe_price:{self.stripe_price_id}", pricing_info, timeout=300)
                except Exception:
                    pass

                return pricing_info

            except stripe.StripeError as e:
                if current_app:
                    current_app.logger.warning(f"Erreur Stripe API pour prix {self.stripe_price_id}: {str(e)}")
                return {
                    'amount': None,
                    'currency': 'EUR',
                    'currency_symbol': '€',
                    'interval': 'month',
                    'interval_display': 'mois'
                }

        except Exception as e:
            if current_app:
                current_app.logger.error(f"Erreur récupération pricing pour plan {self.display_name}: {str(e)}")
            return {
                'amount': None,
                'currency': 'EUR',
                'currency_symbol': '€',
                'interval': 'month',
                'interval_display': 'mois'
            }

    def __repr__(self):
        return f'<Plan {self.display_name}>'

class WebhookActionStatus(enum.Enum):
    """Enum pour les statuts d'action des webhooks"""
    SUCCESS = 'success'
    FAILED = 'failed'
    IGNORED = 'ignored'


class WebhookLog(db.Model):
    """Model for tracking Stripe webhook events and processing"""
    __tablename__ = 'webhook_logs'

    id = db.Column(db.Integer, primary_key=True)

    # Identifiants événement Stripe
    stripe_event_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    event_type = db.Column(db.String(100), nullable=False, index=True)

    # Timestamps
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    processed_at = db.Column(db.DateTime, nullable=True)

    # Statut et résultats
    action_status = db.Column(db.Enum(WebhookActionStatus), nullable=False, index=True)
    action_detail = db.Column(db.String(500), nullable=True)  # Description de l'action effectuée

    # Identifiants Stripe associés
    stripe_customer_id = db.Column(db.String(100), nullable=True, index=True)
    stripe_subscription_id = db.Column(db.String(100), nullable=True, index=True)

    # Liens vers nos modèles
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=True, index=True)
    company_name = db.Column(db.String(255), nullable=True)  # Cache pour éviter JOIN

    # Détails techniques
    processing_time_ms = db.Column(db.Integer, nullable=True)  # Temps de traitement
    error_message = db.Column(db.Text, nullable=True)  # Message d'erreur si échec

    # Données brutes (debug)
    raw_event_data = db.Column(db.JSON, nullable=True)  # Événement Stripe original (si nécessaire)

    # Relations
    company = db.relationship('Company', backref='webhook_logs')

    def __repr__(self):
        return f'<WebhookLog {self.event_type} - {self.action_status.value} - {self.company_name or "Unknown"}>'

    @property
    def action_status_display(self):
        """Version française du statut pour l'interface"""
        status_map = {
            WebhookActionStatus.SUCCESS: 'Succès',
            WebhookActionStatus.FAILED: 'Échoué',
            WebhookActionStatus.IGNORED: 'Ignoré'
        }
        return status_map.get(self.action_status, self.action_status.value)

    @property
    def processing_duration_display(self):
        """Durée de traitement lisible"""
        if not self.processing_time_ms:
            return 'N/A'
        if self.processing_time_ms < 1000:
            return f'{self.processing_time_ms}ms'
        else:
            return f'{self.processing_time_ms / 1000:.1f}s'

    @classmethod
    def create_log(cls, stripe_event_id, event_type, action_status,
                   action_detail=None, company=None, stripe_customer_id=None,
                   stripe_subscription_id=None, processing_time_ms=None,
                   error_message=None, raw_event_data=None):
        """Créer un log de webhook facilement"""
        log = cls(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            action_status=action_status,
            action_detail=action_detail,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
            company_id=company.id if company else None,
            company_name=company.name if company else None,
            processing_time_ms=processing_time_ms,
            error_message=error_message,
            raw_event_data=raw_event_data,
            processed_at=datetime.utcnow()
        )

        try:
            db.session.add(log)
            db.session.commit()
            return log
        except Exception as e:
            db.session.rollback()
            # Log l'erreur mais ne pas faire échouer le processus principal
            import logging
            logging.getLogger(__name__).error(f"Erreur création WebhookLog: {e}")
            return None

class SubscriptionAuditLog(db.Model):
    """Model for tracking subscription changes history - REFONTE WEBHOOKS"""
    __tablename__ = 'subscription_audit_log'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=True)
    event_type = db.Column(db.String(100), nullable=False)  # Type d'événement Stripe (customer.subscription.updated, etc.)
    stripe_event_id = db.Column(db.String(255), unique=True, nullable=False, index=True)  # ID unique de l'événement Stripe
    before_json = db.Column(db.JSON)  # État avant le changement
    after_json = db.Column(db.JSON)  # État après le changement
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Champs de compatibilité (legacy)
    action_type = db.Column(db.String(50))  # Conservé temporairement pour compatibilité
    old_plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True, index=True)
    new_plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True, index=True)
    old_quantity_licenses = db.Column(db.Integer, nullable=True)
    new_quantity_licenses = db.Column(db.Integer, nullable=True)
    effective_date = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    system_actions_status = db.Column(db.JSON, nullable=True)
    predicted_date = db.Column(db.DateTime, nullable=True)
    is_scheduled = db.Column(db.Boolean, default=False)
    change_summary = db.Column(db.Text, nullable=True)

    # Relations
    company = db.relationship('Company', backref='subscription_audit_logs')
    old_plan = db.relationship('Plan', foreign_keys=[old_plan_id])
    new_plan = db.relationship('Plan', foreign_keys=[new_plan_id])
    user = db.relationship('User', backref='subscription_changes')

    __table_args__ = (
        db.Index('idx_sal_company', 'company_id'),
    )

    def __repr__(self):
        return f'<SubscriptionAuditLog {self.event_type} for Company {self.company_id}>'

# REFONTE STRIPE 2.0 - Classe LicenseState supprimée
# Gestion des états de licence remplacée par le système V2 unifié


class UserCompany(db.Model):
    """Association table for User-Company many-to-many relationship"""
    __tablename__ = 'user_companies'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='employe')

    @db.validates('role')
    def validate_role(self, key, role):
        """Normaliser automatiquement les rôles à l'écriture - SÉCURITÉ"""
        from utils.role_utils import normalize_role
        try:
            return normalize_role(role)
        except ValueError:
            return role  # Garder tel quel si non reconnu
    is_active = db.Column(db.Boolean, default=True)
    can_create_campaigns = db.Column(db.Boolean, default=False)  # Permission de créer des campagnes (déléguée par super_admin)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Gestion désactivation différée pour downgrades
    scheduled_deactivation_date = db.Column(db.DateTime, nullable=True)  # Date à laquelle désactiver cet utilisateur
    deactivation_reason = db.Column(db.String(100), nullable=True)  # Raison (ex: "licence_reduction")

    __table_args__ = (
        db.UniqueConstraint('user_id', 'company_id', name='unique_user_company'),
        db.Index('idx_uc_company', 'company_id'),
    )

    @validates('role')
    def validate_role(self, key, role):
        if role not in ['admin', 'employe', 'super_admin', 'lecteur']:
            raise ValueError("Role must be 'admin', 'employe', 'super_admin', or 'lecteur'")
        return role

    @property
    def is_scheduled_for_deactivation(self):
        """Vérifier si cet utilisateur est programmé pour désactivation"""
        return (self.scheduled_deactivation_date is not None and
                self.scheduled_deactivation_date > datetime.utcnow())

    @property
    def deactivation_status_text(self):
        """Texte descriptif du statut de désactivation"""
        if not self.scheduled_deactivation_date:
            return None

        if self.scheduled_deactivation_date <= datetime.utcnow():
            return "Désactivation en attente de traitement"
        else:
            return f"Sera désactivé le {self.scheduled_deactivation_date.strftime('%d/%m/%Y à %H:%M')}"

class Company(db.Model):
    """Model for companies (multi-tenant SaaS support)"""
    __tablename__ = 'companies'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20))
    address = db.Column(db.Text)
    logo_path = db.Column(db.String(255))  # OBSOLÈTE - conservé pour migration temporaire
    logo_base64 = db.Column(db.Text)  # Logo encodé en Base64 avec data URI (survit aux redéploiements)
    primary_color = db.Column(db.String(7), default='#007bff')  # Hex color code
    secondary_color = db.Column(db.String(7), default='#6c757d')  # Hex color code
    timezone = db.Column(db.String(50), default='America/Montreal')  # Fuseau horaire
    currency = db.Column(db.String(3), default='CAD')  # Code devise ISO 4217 (CAD, USD, EUR, etc.)
    aging_calculation_method = db.Column(db.String(20), default='invoice_date')  # 'invoice_date' or 'due_date'

    # Project field configuration (optional hierarchy: Client > Project > Invoices)
    project_field_enabled = db.Column(db.Boolean, default=False)  # Enable/disable project hierarchy
    project_field_name = db.Column(db.String(50), default='Projet')  # Customizable name (e.g., "Projet", "Contrat", "Chantier")

    # Configuration obsolète - remplacée par OAuth utilisateur individuel

    # Configuration courriel déplacée au niveau utilisateur

    # SaaS fields - REFONTE: plan_id est maintenant la source unique de vérité
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True, index=True)  # Reference to Plan table - SOURCE UNIQUE
    plan = db.Column(db.String(50), nullable=False, default='decouverte')  # OBSOLÈTE - utiliser plan_id
    plan_status = db.Column(db.String(20), default='active')  # OBSOLETE - utiliser 'status' à la place
    registration_date = db.Column(db.DateTime, default=datetime.utcnow)
    connection_type = db.Column(db.String(50), default='manual')  # 'manual', 'csv_import', 'quickbooks', 'microsoft'

    # Stripe subscription management - REFONTE STRIPE 2.0 - EN CLAIR
    stripe_subscription_id = db.Column(db.String(50))  # Main Stripe subscription ID
    stripe_customer_id = db.Column(db.String(50))  # Stripe customer ID

    # NOUVEAU - Architecture simplifiée avec une seule licence payante
    quantity_licenses = db.Column(db.Integer, default=1)  # Nombre total de licences payantes (admin, super_admin, employe)
    quantity = db.Column(db.Integer, default=1)  # REFONTE WEBHOOKS - Nombre de licences actuel

    # Stripe IDs stockés en clair - Correction architecture selon documentation Stripe
    # Les IDs Stripe (cus_*, sub_*) sont des identifiants opaques non-sensibles
    # Le chiffrement empêchait les appels API Stripe et le Customer Portal

    # OBSOLÈTE - À supprimer en Phase 3 (conservé temporairement pour migration)
    # REFONTE STRIPE 2.0 - Système unifié : quantity_admin/quantity_employee supprimés
    # Remplacés par quantity_licenses (nombre total de licences payantes)
    grace_period_end = db.Column(db.DateTime, nullable=True)  # Date de fin de période de grâce pour éviter double facturation

    # Plan limits (for discovery and free accounts)
    client_limit = db.Column(db.Integer)  # NULL = unlimited, 10 for discovery
    current_client_count = db.Column(db.Integer, default=0)  # Current client count for limit checking

    # Manual override for free accounts created by super admin
    is_free_account = db.Column(db.Boolean, default=False)  # True for accounts created manually without Stripe

    # Company creator tracking
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)  # User who created this company

    # Gestion des états d'abonnement (REFONTE WEBHOOKS)
    subscription_status = db.Column(db.String(50), default='active')  # OBSOLETE - utiliser 'status' à la place
    status = db.Column(db.String(50), default='active')  # 'active', 'pending_cancellation', 'pending_downgrade', 'canceled', 'past_due', 'unpaid', 'expired'
    cancel_at = db.Column(db.DateTime, nullable=True)  # Date d'effet d'annulation différée
    pending_plan = db.Column(db.String(50), nullable=True)  # Plan cible pour downgrade différé
    pending_quantity = db.Column(db.Integer, nullable=True)  # Quantité cible pour downgrade différé
    pending_expires_at = db.Column(db.DateTime, nullable=True)  # Date d'application du downgrade différé

    # OBSOLÈTE - À supprimer en Phase 3 (conservé temporairement pour migration)
    # REFONTE STRIPE 2.0 - Gestion annulation via Customer Portal Stripe uniquement
    # Champs cancellation_date et can_reactivate supprimés

    # Note: Champs pending_* supprimés - utilisation de cancel_at_period_end native de Stripe

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user_companies = db.relationship('UserCompany', backref='company', lazy=True, cascade='all, delete-orphan')
    clients = db.relationship('Client', backref='company', lazy=True, cascade='all, delete-orphan')
    email_templates = db.relationship('EmailTemplate', backref='company', lazy=True, cascade='all, delete-orphan')
    accounting_connections = db.relationship('AccountingConnection', backref='company', lazy=True, cascade='all, delete-orphan')
    plan_ref = db.relationship('Plan', foreign_keys=[plan_id], backref='companies', lazy=True)
    # Note: pending_plan_ref supprimé avec l'approche cancel_at_period_end

    @validates('plan')
    def validate_plan(self, key, plan):
        # Si plan est None ou vide, c'est valide (compte gratuit)
        if not plan:
            return plan

        # Valider que le plan existe en base de données
        from sqlalchemy import text
        existing_plan = db.session.execute(
            text("SELECT name FROM plans WHERE name = :plan_name AND is_active = true"),
            {"plan_name": plan}
        ).fetchone()

        if not existing_plan:
            # Récupérer la liste des plans actifs pour le message d'erreur
            active_plans = db.session.execute(
                text("SELECT name FROM plans WHERE is_active = true")
            ).fetchall()
            valid_plans = [p[0] for p in active_plans]
            raise ValueError(f"Plan must be one of {valid_plans}")
        return plan

    def get_plan_display_name(self):
        """Get display name for the plan - REFONTE: utilise exclusivement plan_id"""
        # PRIORITÉ: Utiliser plan_id comme source unique de vérité
        if self.plan_id:
            # CORRECTION CRITIQUE : Ne PAS modifier self.plan_ref pour éviter flush automatique
            if self.plan_ref:
                return self.plan_ref.display_name
            else:
                # Lire directement depuis la base sans modifier l'objet
                plan = Plan.query.get(self.plan_id)
                if plan:
                    return plan.display_name

        # Fallback legacy uniquement si pas de plan_id (anciens comptes)
        plan_names = {
            'decouverte': 'Découverte',
            'relance': 'Relance',
            'relance_plus': 'Relance+'
        }
        return plan_names.get(self.plan, 'Découverte')

    @property
    def plan_name(self):
        """REFONTE: Propriété calculée basée sur plan_id - remplace le champ plan"""
        if self.plan_id and self.plan_ref:
            return self.plan_ref.name
        elif self.plan_id:
            # Forcer le chargement si pas déjà fait
            plan = Plan.query.get(self.plan_id)
            if plan:
                return plan.name
        # Fallback legacy
        return self.plan or 'decouverte'

    def get_plan_features(self):
        """Get plan features from Plan model or legacy logic"""
        # Si on a un plan_id, charger le plan depuis la base de données
        # CORRECTION CRITIQUE : Ne PAS modifier self.plan_ref pour éviter flush automatique
        plan_to_use = None
        if self.plan_id:
            if self.plan_ref:
                plan_to_use = self.plan_ref
            else:
                from utils.plan_cache import get_plan_by_id
                plan_to_use = get_plan_by_id(self.plan_id)

        if plan_to_use:
            return {
                'max_clients': plan_to_use.max_clients,
                'allows_email_sending': plan_to_use.allows_email_sending,
                'allows_email_connection': plan_to_use.allows_email_connection,
                'allows_accounting_connection': plan_to_use.allows_accounting_connection,
                'allows_team_management': plan_to_use.allows_team_management,
                'allows_email_templates': plan_to_use.allows_email_templates,
                'is_free': plan_to_use.is_free
            }

        # Legacy plan logic avec support pour les plans avec variantes
        legacy_features = {
            'decouverte': {
                'max_clients': 10,
                'allows_email_sending': False,
                'allows_email_connection': False,
                'allows_accounting_connection': False,
                'allows_team_management': False,
                'allows_email_templates': False,
                'is_free': True
            },
            'relance': {
                'max_clients': None,  # Unlimited
                'allows_email_sending': False,
                'allows_email_connection': False,
                'allows_accounting_connection': True,
                'allows_team_management': False,  # Relance = 1 seul utilisateur selon spécifications
                'allows_email_templates': False,
                'is_free': False
            },
            'relance_plus': {
                'max_clients': None,  # Unlimited
                'allows_email_sending': True,
                'allows_email_connection': True,
                'allows_accounting_connection': True,
                'allows_team_management': True,
                'allows_email_templates': True,
                'is_free': False
            }
        }

        # Support pour les plans avec variantes (ex: "Relance (Mensuel)")
        plan_name = self.plan.lower() if self.plan else 'decouverte'

        # Extraire le nom de base du plan (avant les parenthèses)
        if '(' in plan_name:
            plan_name = plan_name.split('(')[0].strip()

        # Normaliser le nom du plan
        plan_name = plan_name.replace(' ', '_').replace('+', '_plus')

        return legacy_features.get(plan_name, legacy_features['decouverte'])

    def get_client_limit(self):
        """Get client limit for this company"""
        # Check direct client_limit field first (for manually set limits)
        if self.client_limit is not None:
            return self.client_limit

        # PRIORITÉ: Utiliser la relation plan_ref canonique AVANT fallback
        # CORRECTION CRITIQUE : Ne PAS modifier self.plan_ref pour éviter flush automatique
        plan_to_use = None
        if self.plan_id:
            if self.plan_ref:
                plan_to_use = self.plan_ref
            else:
                from utils.plan_cache import get_plan_by_id
                plan_to_use = get_plan_by_id(self.plan_id)

        if plan_to_use:
            from flask import current_app
            return plan_to_use.max_clients

        # Fall back to plan features UNIQUEMENT si plan_ref manque
        features = self.get_plan_features()
        from flask import current_app
        current_app.logger.warning(f"⚠️ FALLBACK PLAN pour {self.name}: plan='{self.plan}', résolu max_clients={features.get('max_clients')}")
        return features.get('max_clients')

    def can_add_client(self):
        """Check if company can add more clients based on plan limits"""
        max_clients = self.get_client_limit()

        if max_clients is None or max_clients == 0:
            return True  # Unlimited (None ou 0 = unlimited)

        return self.current_client_count < max_clients

    def assert_client_capacity(self, delta=1):
        """SÉCURISÉ: Vérification avec COUNT(*) frais de la DB pour éviter les bypasses de licence"""
        from flask import current_app

        max_clients = self.get_client_limit()

        if max_clients is None or max_clients == 0:
            # Plan illimité : skip silencieux pour éviter pollution logs (appelé à chaque création de client)
            return True  # Unlimited (None ou 0 = unlimited)

        # COUNT(*) FRAIS de la base de données - évite les caches/stale data
        from models import Client  # Import local pour éviter les imports circulaires
        current_client_count = db.session.query(db.func.count(Client.id)).filter_by(company_id=self.id).scalar()

        if current_client_count + delta > max_clients:
            current_app.logger.warning(f"🚫 LICENCE BLOQUÉE - {self.name}: {current_client_count} clients actuels + {delta} tentative = {current_client_count + delta} > limite {max_clients} (plan: {self.get_plan_display_name()})")

            # Flash message uniquement si dans un contexte de requête HTTP
            from flask import flash, has_request_context
            if has_request_context():
                flash(f'❌ Limite de licence atteinte: Votre plan "{self.get_plan_display_name()}" limite à {max_clients} clients. Vous avez actuellement {current_client_count} clients. Veuillez mettre à niveau votre plan.', 'error')

            raise ValueError(f"License limit exceeded: {current_client_count + delta} > {max_clients}")

        # Réduire verbosité : DEBUG au lieu d'INFO (utile seulement pour debugging)
        return True

    def get_total_user_count(self):
        """Get total number of users in this company"""
        # PHASE 1 : protection RelationshipProperty
        return len([uc for uc in list(self.user_companies) if uc.is_active])

    def get_user_count_by_role(self, role):
        """Get number of users with specific role"""
        # PHASE 1 : protection RelationshipProperty
        return len([uc for uc in list(self.user_companies) if uc.is_active and uc.role == role])

    # Méthodes find_by_stripe_* supprimées - Remplacées par requêtes directes
    # Utilisez : Company.query.filter_by(stripe_customer_id=customer_id).first()
    # Utilisez : Company.query.filter_by(stripe_subscription_id=subscription_id).first()

    def get_super_admin(self):
        """Obtenir le super admin de cette entreprise"""
        for uc in self.user_companies:
            if uc.role == 'super_admin' and uc.is_active:
                return uc.user
        return None

    def get_all_users(self):
        """Obtenir tous les utilisateurs actifs de cette entreprise"""
        users = []
        for uc in self.user_companies:
            if uc.is_active:
                users.append(uc.user)
        return users

    def active_users_excluding_super_admin(self):
        """Obtenir tous les utilisateurs actifs sauf le super admin"""
        users = []
        for uc in self.user_companies:
            if uc.is_active and uc.role != 'super_admin':
                users.append(uc.user)
        return users

    def to_dict(self):
        """Convertir l'objet Company en dictionnaire pour l'audit"""
        return {
            'id': self.id,
            'name': self.name,
            'plan': self.plan,
            'status': self.status,
            'quantity': self.quantity,
            'stripe_customer_id': self.stripe_customer_id,
            'stripe_subscription_id': self.stripe_subscription_id,
            'cancel_at': self.cancel_at.isoformat() if self.cancel_at else None,
            'pending_plan': self.pending_plan,
            'pending_quantity': self.pending_quantity,
            'pending_expires_at': self.pending_expires_at.isoformat() if self.pending_expires_at else None
        }


    # Note: Méthodes pending_* supprimées - gestion native via Stripe cancel_at_period_end

    # REFONTE STRIPE 2.0 - Système complexe de licences supprimé
    # Remplacé par accès direct aux données Stripe via le système V2

    # REFONTE STRIPE 2.0 - Méthode supprimée (get_active_grace_period)
    # Système de grâce remplacé par la logique simplifiée V2

    # REFONTE STRIPE 2.0 - Méthode create_grace_period supprimée
    # Système de grâce remplacé par gestion directe via Customer Portal Stripe

    # SUPPRESSION DÉFINITIVE - Méthode remplacée par get_license_counts('stripe')

    def get_pending_changes_from_stripe(self):
        """Alias pour get_pending_subscription_changes pour compatibilité"""
        return self.get_pending_subscription_changes()

    def get_pending_subscription_changes(self):
        """Récupérer les changements programmés depuis Stripe avec monitoring complet"""
        try:
            if not self.stripe_subscription_id:
                return None

            import stripe
            from flask import current_app
            from datetime import datetime

            subscription = stripe.Subscription.retrieve(self.stripe_subscription_id)

            # Système de monitoring complet des changements
            pending_changes = {
                'has_changes': False,
                'next_billing_date': None,
                'new_plan_id': None,
                # REFONTE STRIPE 2.0 - Champs quantity_admin/employee supprimés
                'change_type': None,  # 'upgrade', 'downgrade', 'cancellation', 'license_change'
                'current_plan_name': None,
                'new_plan_name': None,
                'cancellation_date': None,
                'is_cancelled': False,
                'detailed_changes': []  # Liste détaillée des changements
            }

            # Date du prochain cycle de facturation
            current_period_end = getattr(subscription, 'current_period_end', None)
            if current_period_end:
                pending_changes['next_billing_date'] = datetime.fromtimestamp(current_period_end)

            # Récupérer le plan actuel pour comparaison
            current_plan = Plan.query.get(self.plan_id) if self.plan_id else None
            pending_changes['current_plan_name'] = current_plan.display_name if current_plan else "Aucun"

            # Vérifier si l'abonnement sera annulé à la fin de la période
            if subscription.cancel_at_period_end:
                pending_changes['has_changes'] = True
                pending_changes['change_type'] = 'cancellation'
                pending_changes['is_cancelled'] = True
                pending_changes['cancellation_date'] = pending_changes['next_billing_date']
                pending_changes['new_plan_name'] = "Découverte (Gratuit)"
                pending_changes['detailed_changes'].append({
                    'type': 'plan_change',
                    'from': pending_changes['current_plan_name'],
                    'to': 'Découverte (Gratuit)',
                    'effective_date': pending_changes['cancellation_date']
                })
                pending_changes['detailed_changes'].append({
                    'type': 'license_change',
                    'from': f"Licences actuelles: {self.quantity_licenses}",
                    'to': "Plan Découverte: 1 licence gratuite",
                    'effective_date': pending_changes['cancellation_date']
                })
                return pending_changes

            # Vérifier les métadonnées pour les changements de plan programmés
            metadata = subscription.metadata or {}
            if metadata.get('action') == 'downgrade':
                try:
                    pending_changes['has_changes'] = True
                    pending_changes['change_type'] = 'downgrade'

                    # REFONTE STRIPE 2.0 : Récupérer la quantité de licences simplifiée
                    new_quantity_licenses = int(metadata.get('quantity_licenses', 1))

                    pending_changes['new_quantity_licenses'] = new_quantity_licenses

                    # Plan change si plan_id est spécifié
                    if metadata.get('plan_id'):
                        pending_changes['new_plan_id'] = int(metadata.get('plan_id'))
                        new_plan = Plan.query.get(pending_changes['new_plan_id'])
                        pending_changes['new_plan_name'] = new_plan.display_name if new_plan else "Plan inconnu"

                        pending_changes['detailed_changes'].append({
                            'type': 'plan_change',
                            'from': pending_changes['current_plan_name'],
                            'to': pending_changes['new_plan_name'],
                            'effective_date': pending_changes['next_billing_date']
                        })

                    # REFONTE STRIPE 2.0 : License change (architecture simplifiée)
                    current_licenses = self.quantity_licenses or 1

                    if new_quantity_licenses != current_licenses:
                        pending_changes['detailed_changes'].append({
                            'type': 'license_change',
                            'description': f"Licences: {current_licenses} → {new_quantity_licenses}",
                            'effective_date': pending_changes['next_billing_date']
                        })

                    current_app.logger.info(f"Downgrade programmé détecté: {self.name} → {new_quantity_licenses} licences")

                    # Retourner immédiatement car nous avons toutes les informations des métadonnées
                    return pending_changes

                except (ValueError, TypeError):
                    current_app.logger.warning(f"Erreur parsing métadonnées abonnement pour {self.name}")

            # REFONTE STRIPE 2.0 : Vérifier les changements de quantité simplifiés
            current_licenses = self.quantity_licenses or 1
            # Dans le système V2, conversion pour compatibilité avec l'ancien affichage
            current_admin = 1
            current_employee = max(0, current_licenses - 1)

            # Récupérer les quantités actuelles de Stripe
            stripe_admin_qty = 0
            stripe_employee_qty = 0

            # Récupérer le plan actuel pour identifier les prices
            current_plan = Plan.query.get(self.plan_id) if self.plan_id else None

            # CORRECTION CRITIQUE avec helpers normalisés
            try:
                from utils import _get_stripe_items_safely
                items_data = _get_stripe_items_safely(subscription)
                # PHASE 1 : current_app protection
                from flask import current_app
                if current_app and hasattr(items_data, '__len__'):
                    current_app.logger.debug(f"Items Stripe récupérés pour {self.name}: {len(items_data)} items")
            except Exception as e:
                # PHASE 1 : current_app protection
                from flask import current_app
                if current_app:
                    current_app.logger.error(f"Erreur accès items Stripe: {str(e)}")
                return pending_changes

            # REFONTE STRIPE 2.0 : Architecture simplifiée avec quantity_licenses
            stripe_licenses = 0

            # Vérifier que items_data est itérable
            if hasattr(items_data, '__iter__') and not isinstance(items_data, str):
                from utils import get_item_price_id, get_item_quantity
                for item in items_data:
                    item_price_id = get_item_price_id(item)
                    if item_price_id and current_plan:
                        # REFONTE STRIPE 2.0 : Un seul price_id unifié
                        if item_price_id == current_plan.stripe_price_id:
                            stripe_licenses = get_item_quantity(item)

            # Détecter les changements de licence programmés
            if stripe_licenses != current_licenses and not pending_changes['has_changes']:
                pending_changes['has_changes'] = True
                pending_changes['change_type'] = 'license_change'
                pending_changes['new_quantity_licenses'] = stripe_licenses
                pending_changes['detailed_changes'].append({
                    'type': 'license_change',
                    'from': f"Licences: {current_licenses}",
                    'to': f"Licences: {stripe_licenses}",
                    'effective_date': pending_changes['next_billing_date']
                })

            return pending_changes

        except Exception as e:
            # PHASE 1 : current_app protection
            from flask import current_app
            if current_app:
                current_app.logger.error(f"Erreur récupération changements programmés pour {self.name}: {str(e)}")
            return None

    # REFONTE STRIPE 2.0 - NOUVELLES MÉTHODES SIMPLIFIÉES

    def get_used_licenses(self):
        """Compter seulement les rôles payants - SÉCURISÉ avec synonymes legacy.
        Les superusers (support) sont exclus du comptage des licences."""
        from utils.role_utils import PAID_ROLES
        # CRITIQUE: Inclure les synonymes legacy pour éviter contournement
        PAID_ROLES_WITH_LEGACY = PAID_ROLES + ['employee']  # 'employee' legacy
        return UserCompany.query.join(User, User.id == UserCompany.user_id).filter(
            UserCompany.company_id == self.id,
            UserCompany.is_active == True,
            UserCompany.role.in_(PAID_ROLES_WITH_LEGACY),
            User.is_superuser == False
        ).count()

    def can_add_paid_user(self):
        """Vérifier si on peut ajouter un utilisateur payant - ARCHITECTURE SIMPLIFIÉE"""
        used = self.get_used_licenses()
        return used < (self.quantity_licenses or 1)

    def can_add_user(self, role):
        """Vérifier si on peut ajouter un utilisateur avec un rôle donné - MÉTHODE CENTRALISÉE"""
        from utils.role_utils import normalize_role, is_paid_role

        try:
            normalized_role = normalize_role(role)
        except ValueError as e:
            return False, str(e)

        # Rôles gratuits - pas de vérification
        if not is_paid_role(normalized_role):
            return True, "OK"

        # Rôles payants - vérification des licences
        if self.can_add_paid_user():
            return True, "OK"
        else:
            current_licenses = self.quantity_licenses or 1
            used_licenses = self.get_used_licenses()
            return False, f'Limite de licences atteinte. Vous avez {current_licenses} licence(s) et {used_licenses} utilisateur(s) payant(s). Veuillez acheter plus de licences ou choisir le rôle "Lecteur" (gratuit).'

    def handle_license_downgrade_auto_conversion(self):
        """Convertir automatiquement les utilisateurs excédentaires en Lecteur lors d'un downgrade"""
        from flask import current_app

        current_licenses = self.quantity_licenses or 1
        current_paid_users = self.get_used_licenses()

        if current_paid_users <= current_licenses:
            # Pas de dépassement, rien à faire
            return 0

        # Calculer le nombre d'utilisateurs à convertir
        excess_users = current_paid_users - current_licenses

        current_app.logger.info(f"📉 Conversion automatique de {excess_users} utilisateur(s) en Lecteur")

        # Récupérer les utilisateurs payants triés par date de création (les plus récents en premier)
        paid_users = UserCompany.query.filter_by(
            company_id=self.id,
            is_active=True
        ).filter(
            UserCompany.role.in_(['admin', 'super_admin', 'employe'])
        ).order_by(UserCompany.created_at.desc()).limit(excess_users).all()

        converted_count = 0
        for user_company in paid_users:
            old_role = user_company.role
            user_company.role = 'lecteur'

            # Logger la conversion

            converted_count += 1

        # Sauvegarder les changements
        db.session.commit()


        return converted_count

    def get_license_counts(self, source='local'):
        """REFONTE STRIPE 2.0 : Méthode de compatibilité pour système V2 unifié"""
        if source == 'stripe':
            # TEMPORAIREMENT DÉSACTIVÉ - Éviter erreur subscription.items
            from flask import current_app
            # PHASE 1 : current_app protection
            if current_app:
                current_app.logger.warning(f"get_license_counts('stripe') temporairement désactivé pour {self.name}")
            # Dans le système V2, toutes les licences payantes sont quantity_licenses
            return {
                'admin': 1,  # Un admin par défaut
                'employee': max(0, (self.quantity_licenses or 1) - 1),  # Le reste en employés
                'total': self.quantity_licenses or 1
            }
        elif source == 'local':
            # Compter les utilisateurs actifs par rôle
            used_licenses = self.get_used_licenses()
            return {
                'admin': UserCompany.query.filter_by(company_id=self.id, is_active=True, role='admin').count() +
                        UserCompany.query.filter_by(company_id=self.id, is_active=True, role='super_admin').count(),
                'employee': UserCompany.query.filter_by(company_id=self.id, is_active=True, role='employe').count(),
                'total': used_licenses
            }
        else:
            # Fallback vers données locales
            return self.get_license_counts('local')

    def get_license_summary(self):
        """Résumé des licences : utilisées/disponibles/lecteurs - ARCHITECTURE SIMPLIFIÉE"""
        used_licenses = self.get_used_licenses()
        readers_count = UserCompany.query.filter_by(
            company_id=self.id,
            is_active=True,
            role='lecteur'
        ).count()

        return {
            'used': used_licenses,
            'available': (self.quantity_licenses or 1) - used_licenses,
            'total_paid': self.quantity_licenses or 1,
            'readers': readers_count
        }

    def sync_from_stripe(self):
        """Synchroniser les données depuis Stripe (webhooks) - ARCHITECTURE SIMPLIFIÉE"""
        try:
            if not self.stripe_subscription_id:
                return False

            import stripe
            from flask import current_app

            subscription = stripe.Subscription.retrieve(self.stripe_subscription_id)

            # Mettre à jour le statut
            self.subscription_status = subscription.status

            # Récupérer le plan et la quantité depuis les items - CORRECTION V2
            try:
                items_data = subscription.items.data if hasattr(subscription.items, 'data') else subscription.items
                # Vérifier que items_data est itérable
                if hasattr(items_data, '__iter__') and not isinstance(items_data, str):
                    for item in items_data:
                        plan = Plan.query.filter_by(stripe_price_id=item.price.id).first()
                        if plan:
                            self.plan_id = plan.id
                            self.plan = plan.name  # CORRECTION: Synchroniser le champ obsolète 'plan'
                            self.quantity_licenses = item.quantity
                            # Forcer SQLAlchemy à détecter les modifications
                            from sqlalchemy.orm.attributes import flag_modified
                            flag_modified(self, 'plan_id')
                            flag_modified(self, 'plan')
                            break
            except Exception as e:
                # PHASE 1 : current_app protection
                if current_app:
                    current_app.logger.error(f"Erreur accès items sync: {str(e)}")

            db.session.commit()
            # PHASE 1 : current_app protection
            if current_app:
                current_app.logger.info(f"Synchronisation Stripe réussie pour {self.name}: {self.quantity_licenses} licences")
            return True

        except Exception as e:
            # PHASE 1 : current_app protection
            from flask import current_app
            if current_app:
                current_app.logger.error(f"Erreur synchronisation Stripe pour {self.name}: {str(e)}")
            return False

    # SUPPRESSION DÉFINITIVE - Méthode remplacée par get_license_counts('grace')

    def get_stripe_programmed_quantities(self):
        """REFONTE STRIPE 2.0 : Récupérer la quantité de licences depuis Stripe (architecture simplifiée)"""
        try:
            if not self.stripe_subscription_id:
                return self.quantity_licenses or 1

            import stripe

            subscription = stripe.Subscription.retrieve(self.stripe_subscription_id)

            # Accès sécurisé aux items Stripe
            from flask import current_app

            try:
                # CORRECTION CRITIQUE : subscription.items est déjà un ListObject
                items_data = subscription.items.data if hasattr(subscription.items, 'data') else subscription.items
                if not items_data:
                    return self.quantity_licenses or 1
            except Exception as e:
                # PHASE 1 : current_app protection
                if current_app:
                    current_app.logger.error(f"Erreur accès items Stripe: {str(e)}")
                return self.quantity_licenses or 1

            current_plan = Plan.query.get(self.plan_id) if self.plan_id else None
            if not current_plan:
                return self.quantity_licenses or 1

            # REFONTE STRIPE 2.0 : Une seule licence uniforme
            # Vérifier que items_data est itérable
            if hasattr(items_data, '__iter__') and not isinstance(items_data, str):
                for item in items_data:
                    if hasattr(item, 'price') and item.price and hasattr(item.price, 'id'):
                        if item.price.id == current_plan.stripe_price_id:
                            return getattr(item, 'quantity', 1)

            return self.quantity_licenses or 1

        except Exception as e:
            # PHASE 1 : current_app protection
            from flask import current_app
            if current_app:
                current_app.logger.error(f"Erreur récupération quantités Stripe pour {self.name}: {str(e)}")
            return self.quantity_licenses or 1

    def get_effective_license_counts(self):
        """REFONTE STRIPE 2.0 : Obtenir le nombre de licences (architecture simplifiée)"""
        return self.quantity_licenses or 1

    def get_license_breakdown(self, target_licenses=None):
        """REFONTE STRIPE 2.0 : Calculer la répartition des licences (architecture simplifiée)"""
        try:
            # Utiliser quantité actuelle si pas de cible spécifiée
            if target_licenses is None:
                target_licenses = self.quantity_licenses or 1

            # 1. Récupérer quantité Stripe actuelle
            stripe_licenses = self.get_stripe_programmed_quantities()

            # 2. Calculer ce qui sera facturé (différence)
            billable_licenses = max(0, target_licenses - (self.quantity_licenses or 1))

            return {
                'active_licenses': target_licenses,
                'billable_licenses': billable_licenses,
                'current_licenses': self.quantity_licenses or 1,
                'stripe_licenses': stripe_licenses
            }

        except Exception as e:
            from flask import current_app
            current_app.logger.error(f"Erreur calcul répartition licences V2: {e}")
            return {
                'active_licenses': target_licenses or 1,
                'billable_licenses': 0,
                'current_licenses': self.quantity_licenses or 1,
                'stripe_licenses': self.quantity_licenses or 1
            }

    # REFONTE STRIPE 2.0 - Méthode supprimée (sync_local_quantities_with_stripe)
    # Synchronisation automatique via le système V2

    def should_bill_prorata(self, target_licenses):
        """REFONTE STRIPE 2.0 : Déterminer si une facturation prorata est nécessaire"""
        breakdown = self.get_license_breakdown(target_licenses)

        # Facturation nécessaire si on dépasse les quantités payées actuelles
        needs_billing = breakdown['billable_licenses'] > 0

        return needs_billing

    def is_in_grace_period_for_quantity(self, target_licenses):
        """REFONTE STRIPE 2.0 : Vérifier si l'augmentation est dans la période de grâce"""
        return not self.should_bill_prorata(target_licenses)

    def is_in_grace_period(self):
        """REFONTE STRIPE 2.0 : Méthode legacy pour compatibilité - architecture simplifiée"""
        return self.is_in_grace_period_for_quantity(self.quantity_licenses or 1)

    def get_grace_period_end_date(self):
        """Récupérer la date de fin de période de grâce depuis Stripe"""
        try:
            if not self.stripe_subscription_id:
                return None

            import stripe
            from datetime import datetime

            subscription = stripe.Subscription.retrieve(self.stripe_subscription_id)

            # MÉTHODE SÉCURISÉE: Accès via _get_stripe_items_safely déplacée vers utils
            from utils import _get_stripe_items_safely
            items_data = _get_stripe_items_safely(subscription)

            # Vérifier que items_data est itérable
            if items_data and hasattr(items_data, '__iter__') and not isinstance(items_data, str):
                for item in items_data:
                    if hasattr(item, 'current_period_end') and item.current_period_end:
                        return datetime.fromtimestamp(item.current_period_end)

            return None

        except Exception as e:
            from flask import current_app
            current_app.logger.error(f"Erreur récupération date de grâce: {e}")
            return None

    # REFONTE STRIPE 2.0 - Méthode supprimée (validate_license_downgrade)
    # Validation complexe remplacée par gestion directe dans Stripe Customer Portal

    # REFONTE STRIPE 2.0 - Méthode supprimée (schedule_user_deactivations)
    # Désactivations programmées remplacées par gestion directe via Customer Portal

    # REFONTE STRIPE 2.0 - Méthode supprimée (get_admin_count)
    # Plus de distinction Admin/Employee dans le nouveau système

    # REFONTE STRIPE 2.0 - Méthode supprimée (get_employee_count)
    # Plus de distinction Admin/Employee dans le nouveau système

    def get_license_change_notifications(self):
        """Get formatted notifications for license changes"""
        try:
            from datetime import datetime
            import stripe

            if not self.stripe_subscription_id:
                return []

            # Get current subscription using subscription ID
            subscription = stripe.Subscription.retrieve(self.stripe_subscription_id)

            if not subscription or subscription.status != 'active':
                return []

            notifications = []

            # Get current and programmed quantities (nouvelle méthode centralisée)
            # REFONTE STRIPE 2.0 - Conversion quantity_licenses vers admin/employee pour affichage
            current_licenses = self.quantity_licenses or 1
            current_quantities = {'admin': 1, 'employee': max(0, current_licenses - 1)}
            programmed_quantities = current_quantities  # Dans V2, pas de programmation complexe

            # Get the effective date for changes
            current_period_end = getattr(subscription, 'current_period_end', None)
            if current_period_end:
                effective_date = datetime.fromtimestamp(current_period_end)
                formatted_date = effective_date.strftime('%d/%m/%Y')
            else:
                formatted_date = "prochaine période"

            # Check for admin license changes
            if current_quantities['admin'] != programmed_quantities['admin']:
                notifications.append({
                    'type': 'admin_change',
                    'current': current_quantities['admin'],
                    'programmed': programmed_quantities['admin'],
                    'effective_date': formatted_date,
                    'message': f"À partir du {formatted_date}, les licences Administrateur passeront de {current_quantities['admin']} à {programmed_quantities['admin']}"
                })

            # Check for employee license changes
            if current_quantities['employee'] != programmed_quantities['employee']:
                notifications.append({
                    'type': 'employee_change',
                    'current': current_quantities['employee'],
                    'programmed': programmed_quantities['employee'],
                    'effective_date': formatted_date,
                    'message': f"À partir du {formatted_date}, les licences Employé passeront de {current_quantities['employee']} à {programmed_quantities['employee']}"
                })

            return notifications

        except Exception as e:
            from flask import current_app
            current_app.logger.error(f"Error getting license change notifications for {self.name}: {e}")
            return []

    # PHASE 1 EXTENSION - Méthodes gestion annulations et états (ajustement_stripe.md)
    def is_pending_cancellation(self):
        """Vérifier si l'abonnement est en cours d'annulation"""
        return self.subscription_status == 'pending_cancellation'

    def is_cancelled(self):
        """Vérifier si l'abonnement est annulé"""
        return self.subscription_status == 'cancelled'

    def can_be_reactivated(self):
        """REFONTE STRIPE 2.0 : Vérifier si l'abonnement peut être réactivé"""
        # REFONTE STRIPE 2.0 : Logique simplifiée, gestion via Stripe Customer Portal
        return self.subscription_status in ['pending_cancellation', 'cancelled']

    def set_pending_cancellation(self, cancellation_date):
        """REFONTE STRIPE 2.0 : Marquer l'abonnement comme en cours d'annulation"""
        self.subscription_status = 'pending_cancellation'
        # REFONTE STRIPE 2.0 : cancellation_date et can_reactivate supprimés des modèles
        # Gestion via Stripe Customer Portal uniquement

    def cancel_subscription(self):
        """REFONTE STRIPE 2.0 : Marquer l'abonnement comme annulé définitivement"""
        self.subscription_status = 'cancelled'
        # REFONTE STRIPE 2.0 : can_reactivate supprimé des modèles

    def reactivate_subscription(self):
        """REFONTE STRIPE 2.0 : Réactiver un abonnement annulé"""
        if self.can_be_reactivated():
            self.subscription_status = 'active'
            # REFONTE STRIPE 2.0 : cancellation_date et can_reactivate supprimés
            return True
        return False

    def get_license_state(self, license_type):
        """Récupérer l'état d'un type de licence"""
        # REFONTE STRIPE 2.0 - LicenseState supprimée
        return None

    def get_file_import_mapping(self):
        """Récupérer la configuration de mapping Excel/CSV pour cette compagnie"""
        return FileImportMapping.query.filter_by(company_id=self.id).first()

    def __repr__(self):
        return f'<Company {self.name}>'


class User(UserMixin, db.Model):
    """Model for users with multi-company support"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    must_change_password = db.Column(db.Boolean, default=False)
    terms_accepted_at = db.Column(db.DateTime)  # Date d'acceptation des CGU
    terms_version_accepted = db.Column(db.String(10), default='1.0')  # Version des CGU acceptée

    # Global super admin for admin panel access
    is_superuser = db.Column(db.Boolean, default=False)  # Global admin for admin panel

    # OAuth désormais géré au niveau EmailConfiguration

    last_company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=True, index=True)
    migration_notice_dismissed = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # Two-factor authentication is now mandatory for all users
    # No field needed as it's enforced at the application level

    # Relationships
    user_companies = db.relationship('UserCompany', backref='user', lazy=True, cascade='all, delete-orphan')
    created_companies = db.relationship('Company', foreign_keys='Company.created_by_user_id', backref='creator', lazy=True)

    @property
    def display_name(self):
        """Return user's display name (first_name + last_name) with fallback to email"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        elif self.last_name:
            return self.last_name
        else:
            return self.email

    def get_company_membership(self, company_id):
        """Get UserCompany record for a specific company with retry and cache"""
        cache_key = f'user_{self.id}_company_{company_id}_membership'

        try:
            if hasattr(g, '_membership_cache') and cache_key in g._membership_cache:
                return g._membership_cache[cache_key]
        except RuntimeError:
            pass

        result = safe_db_query(
            lambda: UserCompany.query.filter_by(user_id=self.id, company_id=company_id).first(),
            default=None
        )

        try:
            if not hasattr(g, '_membership_cache'):
                g._membership_cache = {}
            g._membership_cache[cache_key] = result
        except RuntimeError:
            pass

        return result

    def get_role_in_company(self, company_id):
        """Get user's role in a specific company"""
        membership = self.get_company_membership(company_id)
        return membership.role if membership else None

    def is_active_in_company(self, company_id):
        """Check if user is active in a specific company"""
        membership = self.get_company_membership(company_id)
        return membership.is_active if membership else False

    def get_companies(self):
        """Get all companies the user belongs to - ordered by most recent first"""
        # Retourner les compagnies triées par date de création (plus récente en premier)
        active_relations = [uc for uc in list(self.user_companies) if uc.is_active]
        return [uc.company for uc in sorted(active_relations, key=lambda x: x.created_at, reverse=True)]

    def is_admin(self, company_id=None):
        """Check if user has admin privileges in a company"""
        if company_id is None:
            # Backward compatibility - uses selected company
            selected_company = self.get_selected_company()
            if selected_company:
                role = self.get_role_in_company(selected_company.id)
                return role in ['admin', 'super_admin']
            return False
        else:
            role = self.get_role_in_company(company_id)
            return role in ['admin', 'super_admin']

    def is_super_admin(self, company_id=None):
        """Check if user is super admin in a company"""
        if company_id is None:
            # Backward compatibility - uses selected company
            selected_company = self.get_selected_company()
            if selected_company:
                role = self.get_role_in_company(selected_company.id)
                return role == 'super_admin'
            return False
        else:
            role = self.get_role_in_company(company_id)
            return role == 'super_admin'

    def can_manage_users(self, company_id=None):
        """Check if user can manage other users in a company"""
        if company_id is None:
            # Backward compatibility - uses selected company
            selected_company = self.get_selected_company()
            if selected_company:
                role = self.get_role_in_company(selected_company.id)
                return role in ['admin', 'super_admin']
            return False
        else:
            role = self.get_role_in_company(company_id)
            return role in ['admin', 'super_admin']

    def can_access_company_settings(self, company_id=None):
        """Check if user can access company settings in a company"""
        if company_id is None:
            # Backward compatibility - uses selected company
            selected_company = self.get_selected_company()
            if selected_company:
                role = self.get_role_in_company(selected_company.id)
                return role in ['super_admin', 'admin']  # Admin peut voir Settings mais certaines sections grisées
            return False
        else:
            role = self.get_role_in_company(company_id)
            return role in ['super_admin', 'admin']

    def can_access_imports(self, company_id=None):
        """Check if user can access import/export features in a company"""
        if company_id is None:
            # Backward compatibility - uses selected company
            selected_company = self.get_selected_company()
            if selected_company:
                role = self.get_role_in_company(selected_company.id)
                return role in ['admin', 'super_admin']  # Employé ne peut pas importer selon spécifications
            return False
        else:
            role = self.get_role_in_company(company_id)
            return role in ['admin', 'super_admin']

    def can_create_edit(self, company_id):
        """Check if user can create or edit data in a company"""
        role = self.get_role_in_company(company_id)
        return role in ['admin', 'super_admin', 'employe']

    def is_read_only(self, company_id=None):
        """Check if user is read-only in a company"""
        if company_id is None:
            # Backward compatibility - uses selected company
            selected_company = self.get_selected_company()
            if selected_company:
                role = self.get_role_in_company(selected_company.id)
                return role == 'lecteur'
            return True
        else:
            role = self.get_role_in_company(company_id)
            return role == 'lecteur'

    def get_primary_company(self):
        """Get the first active company for backward compatibility"""
        # PHASE 1 : protection RelationshipProperty
        active_memberships = [uc for uc in list(self.user_companies) if uc.is_active]
        if active_memberships:
            return active_memberships[0].company
        return None

    def get_selected_company(self):
        """Get the currently selected company from session or primary company"""
        try:
            from flask import session
            selected_company_id = session.get('selected_company_id')

            if selected_company_id:
                # Check if user has access to this company
                membership = self.get_company_membership(selected_company_id)
                if membership and membership.is_active:
                    return membership.company
        except RuntimeError:
            # Outside of request context, fallback to primary company
            pass

        # Fallback to primary company
        return self.get_primary_company()

    @property
    def company_id(self):
        """Backward compatibility property - returns selected company ID"""
        selected_company = self.get_selected_company()
        return selected_company.id if selected_company else None

    @property
    def company(self):
        """Backward compatibility property - returns selected company"""
        return self.get_selected_company()

    @property
    def role(self):
        """Backward compatibility property - returns role in primary company"""
        primary_company = self.get_primary_company()
        if primary_company:
            return self.get_role_in_company(primary_company.id)
        return None

    @property
    def is_active(self):
        """Flask-Login requires this to return True for users allowed to log in"""
        return True

    @property
    def is_active_in_primary_company(self):
        """Check if active in primary company (business logic, not Flask-Login)"""
        primary_company = self.get_primary_company()
        if primary_company:
            return self.is_active_in_company(primary_company.id)
        return False

    @property
    def full_name(self):
        """Get full name for display purposes"""
        return f"{self.first_name} {self.last_name}"

    def get_current_role(self):
        """Get the role in currently selected company"""
        selected_company = self.get_selected_company()
        if selected_company:
            return self.get_role_in_company(selected_company.id)
        return None

    def get_display_name(self):
        """Get display name for UI purposes"""
        return self.full_name

    def __repr__(self):
        return f'<User {self.email}>'


class TwoFactorAuth(db.Model):
    """Model for two-factor authentication codes"""
    __tablename__ = 'two_factor_auth'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    code = db.Column(db.String(6), nullable=False)  # 6-digit numeric code
    used = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Security logging
    ip_address = db.Column(db.String(255))  # Support IPv6 and proxy chains (increased from 45 to 255)
    user_agent = db.Column(db.Text)

    # Relationships
    user = db.relationship('User', backref=db.backref('two_factor_codes', cascade='all, delete-orphan'))

    @staticmethod
    def create_2fa_code(user, ip_address=None, user_agent=None):
        """Create a new 2FA code for a user"""
        import secrets

        # Generate 6-digit code using cryptographic RNG
        code = f"{secrets.randbelow(900000) + 100000}"

        # Set expiration to 10 minutes from now
        expires_at = datetime.utcnow() + timedelta(minutes=10)

        # Truncate IP address to fit database column (255 chars max)
        if ip_address and len(ip_address) > 255:
            ip_address = ip_address[:255]

        # Invalidate any existing unused codes for this user
        existing_codes = TwoFactorAuth.query.filter_by(
            user_id=user.id,
            used=False
        ).all()

        for existing_code in existing_codes:
            existing_code.used = True

        # SÉCURITÉ : Nettoyer automatiquement les codes expirés de plus de 24h
        TwoFactorAuth.cleanup_expired_codes()

        # Create new code - PHASE 1 : constructeur corrigé
        two_factor_code = TwoFactorAuth()
        two_factor_code.user_id = user.id
        two_factor_code.code = code
        two_factor_code.expires_at = expires_at
        two_factor_code.ip_address = ip_address
        two_factor_code.user_agent = user_agent

        db.session.add(two_factor_code)
        db.session.commit()

        return two_factor_code

    @staticmethod
    def find_valid_code(user_id, code):
        """Find a valid unused code for a user"""
        return TwoFactorAuth.query.filter_by(
            user_id=user_id,
            code=code,
            used=False
        ).filter(
            TwoFactorAuth.expires_at > datetime.utcnow()
        ).first()

    def mark_as_used(self):
        """Mark this code as used"""
        self.used = True
        db.session.commit()

    def is_expired(self):
        """Check if the code has expired"""
        return datetime.utcnow() > self.expires_at

    def is_valid(self):
        """Check if the code is valid (not used and not expired)"""
        return not self.used and not self.is_expired()

    @staticmethod
    def cleanup_expired_codes():
        """SÉCURITÉ : Nettoyer automatiquement les codes 2FA expirés (>24h)"""
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=24)
            expired_codes = TwoFactorAuth.query.filter(
                TwoFactorAuth.expires_at < cutoff_time
            ).count()

            if expired_codes > 0:
                TwoFactorAuth.query.filter(
                    TwoFactorAuth.expires_at < cutoff_time
                ).delete()
                db.session.commit()
                current_app.logger.info(f"🧹 Nettoyage automatique: {expired_codes} codes 2FA expirés supprimés")

        except Exception as e:
            current_app.logger.error(f"Erreur nettoyage codes 2FA: {str(e)}")

    def __repr__(self):
        return f'<TwoFactorAuth {self.code} for user {self.user_id}>'


class Client(db.Model):
    """Model for clients"""
    __tablename__ = 'clients'
    __table_args__ = (
        db.UniqueConstraint('code_client', 'company_id', name='clients_code_client_company_unique'),
        db.Index('idx_client_lookup', 'company_id', 'code_client'),  # Index for fast lookups during sync
        db.Index('idx_client_collector', 'company_id', 'collector_id'),  # Index for collector filter queries
        db.Index('idx_client_parent', 'parent_client_id'),  # Index for parent-child hierarchy queries
    )

    id = db.Column(db.Integer, primary_key=True)
    code_client = db.Column(db.String(50), nullable=False)  # Client code - unique per company
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(500))
    phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    collector_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # Assigned collector from team
    representative_name = db.Column(db.String(200))  # Sales representative
    payment_terms = db.Column(db.String(100))  # e.g., "Net 30", "COD", etc.
    language = db.Column(db.String(5), default='fr')  # 'fr' for French, 'en' for English
    parent_client_id = db.Column(db.Integer, db.ForeignKey('clients.id'))  # Parent client for corporate hierarchy
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    invoices = db.relationship('Invoice', backref='client', lazy=True, cascade='all, delete-orphan')
    communication_notes = db.relationship('CommunicationNote', lazy=True, cascade='all, delete-orphan')
    collector = db.relationship('User', foreign_keys=[collector_id], backref='assigned_clients')
    contacts = db.relationship('ClientContact', backref='client', lazy=True, cascade='all, delete-orphan')

    # Parent-child relationships
    parent_client = db.relationship('Client', remote_side=[id], backref='child_clients')

    @property
    def is_parent(self):
        """Check if this client has child clients"""
        return len(self.child_clients) > 0

    @property
    def has_parent(self):
        """Check if this client has a parent"""
        return self.parent_client_id is not None

    def get_all_child_clients(self):
        """Get all child clients for this parent"""
        if not self.is_parent:
            return []
        return self.child_clients

    def get_consolidated_invoices(self, include_children=False):
        """Get invoices for this client and optionally children"""
        invoices = list(self.invoices)
        if include_children and self.is_parent:
            for child in self.child_clients:
                invoices.extend(child.invoices)
        return invoices

    def get_consolidated_notes(self, include_children=False):
        """Get communication notes for this client and optionally children"""
        notes = list(self.communication_notes)
        if include_children and self.is_parent:
            for child in self.child_clients:
                notes.extend(child.communication_notes)
        return sorted(notes, key=lambda x: x.created_at, reverse=True)

    def validate_parent_child_relationship(self, new_parent_id=None):
        """Validate parent-child relationship rules"""
        if new_parent_id:
            # Rule 1: A client cannot be its own parent
            if new_parent_id == self.id:
                return False, "Un client ne peut pas être son propre parent"

            # Rule 2: Get the proposed parent and check if it has a parent (a child cannot be a parent)
            proposed_parent = Client.query.get(new_parent_id)
            if proposed_parent and proposed_parent.parent_client_id is not None:
                return False, "Un compte enfant ne peut pas être parent d'autres comptes"

            # Rule 4: Check if the proposed parent is actually a child of this client
            if new_parent_id in [child.id for child in self.child_clients]:
                return False, "Impossible de définir un enfant comme parent"

            # Rule 5: Check circular relationships
            if proposed_parent and proposed_parent.parent_client_id == self.id:
                return False, "Relation circulaire détectée"

        return True, ""

    def get_total_outstanding(self):
        """Calculate total outstanding amount for this client"""
        return sum(invoice.amount for invoice in self.invoices if not invoice.is_paid)

    @property
    def collector_name(self):
        """Get collector name for template compatibility"""
        return self.collector.full_name if self.collector else None

    def get_aged_balances(self, calculation_method='invoice_date'):
        """Calculate aged balances for this client"""
        today = date.today()
        balances = {'current': 0, '30_days': 0, '60_days': 0, '90_days': 0, 'over_90_days': 0}

        for invoice in self.invoices:
            # Exclure les factures payées
            if invoice.is_paid:
                continue
            # Pour "current", on utilise toujours la date d'échéance
            # Pour les autres tranches, on utilise la méthode choisie
            if not invoice.is_overdue():
                # Facture pas encore échue = courante
                balances['current'] += invoice.amount
            else:
                # Facture échue - calculer selon la méthode choisie
                calc_date = invoice.invoice_date if calculation_method == 'invoice_date' else invoice.due_date
                days_old = (today - calc_date).days

                if days_old <= 30:
                    balances['30_days'] += invoice.amount
                elif days_old <= 60:
                    balances['60_days'] += invoice.amount
                elif days_old <= 90:
                    balances['90_days'] += invoice.amount
                else:
                    balances['over_90_days'] += invoice.amount

        return balances

    def __repr__(self):
        return f'<Client {self.name}>'

class Invoice(db.Model):
    """Model for invoices"""
    __tablename__ = 'invoices'
    __table_args__ = (
        db.UniqueConstraint('company_id', 'client_id', 'invoice_number', name='invoices_company_client_number_unique'),
        db.Index('idx_invoice_lookup', 'company_id', 'client_id', 'invoice_number'),  # Index for fast lookups during sync
        db.Index('idx_invoice_unpaid_due', 'company_id', 'is_paid', 'due_date'),  # Index for receivables queries using due_date
        db.Index('idx_invoice_unpaid_invoice', 'company_id', 'is_paid', 'invoice_date'),  # Index for receivables queries using invoice_date
        db.Index('idx_invoice_client_paid', 'client_id', 'is_paid'),  # Index for client detail page queries filtering by payment status
        db.Index('idx_invoice_project_name', 'company_id', 'project_name'),  # Index for project-based grouping queries
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    project_name = db.Column(db.String(255), nullable=True)  # Optional project name (Excel/CSV import only)
    amount = db.Column(db.Numeric(10, 2), nullable=False)  # Current outstanding amount (balance due)
    original_amount = db.Column(db.Numeric(10, 2), nullable=True)  # Original total invoice amount before payments (optional)
    invoice_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    is_paid = db.Column(db.Boolean, default=False)  # Nouvelle colonne pour le statut de paiement
    invoice_id_external = db.Column(db.String(100), nullable=True)  # ID externe de la facture (QuickBooks, Business Central)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Plus de validation de statut - toutes les factures sont impayées

    def days_outstanding(self, calculation_method='invoice_date'):
        """Calculate days outstanding based on calculation method"""
        today = date.today()
        calc_date = self.invoice_date if calculation_method == 'invoice_date' else self.due_date
        return (today - calc_date).days

    def is_overdue(self):
        """Check if invoice is overdue"""
        return date.today() > self.due_date

    def get_status(self):
        """Calculate status automatically based on due date"""
        if self.is_overdue():
            return 'overdue'
        else:
            return 'unpaid'

    def get_status_display(self):
        """Get display text for status"""
        if self.is_overdue():
            return 'En retard'
        else:
            return 'Pas en retard'

    def __repr__(self):
        return f'<Invoice {self.invoice_number}>'

class CommunicationNote(db.Model):
    """Model for communication notes"""
    __tablename__ = 'communication_notes'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)  # Ajouté selon correction LSP
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    note_text = db.Column(db.Text, nullable=False)
    note_type = db.Column(db.String(20), default='general')  # 'general', 'call', 'email', 'meeting'
    note_date = db.Column(db.Date, nullable=False, default=date.today)  # Date personnalisable de la note

    # Détails spécifiques aux courriels
    email_from = db.Column(db.String(255))  # Expéditeur du courriel
    email_to = db.Column(db.String(255))  # Destinataire du courriel
    email_subject = db.Column(db.String(500))  # Objet du courriel
    email_body = db.Column(db.Text)  # Corps du courriel
    attachments = db.Column(db.JSON)  # Liste des pièces jointes (nom, taille, etc.)

    # Identifiants pour le transfert de courriel
    outlook_message_id = db.Column(db.String(255), nullable=True)  # ID du message Outlook/Microsoft 365
    gmail_message_id = db.Column(db.String(255), nullable=True)  # ID du message Gmail
    conversation_id = db.Column(db.String(255), nullable=True)  # ID de conversation pour threading Outlook

    # Identifiants RFC 2822 pour le threading robuste (gère le cas où le sujet change)
    internet_message_id = db.Column(db.String(500), nullable=True, index=True)  # Message-ID unique du courriel (RFC 2822)
    in_reply_to_id = db.Column(db.String(500), nullable=True, index=True)  # In-Reply-To header - référence au message parent

    # Suivi des échanges email (réponses/transferts)
    email_direction = db.Column(db.String(20), nullable=True)  # 'sent', 'received', 'reply', 'forward'
    parent_note_id = db.Column(db.Integer, db.ForeignKey('communication_notes.id'), nullable=True, index=True)  # Lien vers note originale
    last_sync_at = db.Column(db.DateTime, nullable=True)  # Dernière synchronisation des réponses
    is_from_sync = db.Column(db.Boolean, default=False)  # True si créé par synchronisation auto
    is_conversation_active = db.Column(db.Boolean, default=True)  # True si conversation a eu activité récente (pour optimiser le cron)

    reminder_date = db.Column(db.DateTime, nullable=True)  # Date de rappel optionnelle
    is_reminder_completed = db.Column(db.Boolean, default=False)  # Rappel terminé
    is_urgent = db.Column(db.Boolean, default=False)  # Note urgente (supprimé dans le form mais gardé pour compatibilité)
    is_private = db.Column(db.Boolean, default=False)  # Note privée (supprimé dans le form mais gardé pour compatibilité)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations - Ajoutées selon correction LSP
    user = db.relationship('User', foreign_keys=[user_id])
    updater = db.relationship('User', foreign_keys=[updated_by])
    client = db.relationship('Client', foreign_keys=[client_id], overlaps="communication_notes")

    # Relation self-referential pour les réponses/transferts
    parent_note = db.relationship('CommunicationNote', remote_side=[id], backref='replies', foreign_keys=[parent_note_id])

    __table_args__ = (
        db.Index('idx_cn_company', 'company_id'),
        db.Index('idx_cn_company_client', 'company_id', 'client_id'),
        db.Index('idx_cn_conversation', 'conversation_id'),
        db.Index('idx_cn_company_type', 'company_id', 'note_type'),
        db.Index('idx_cn_reminder_date', 'reminder_date'),
    )

    @validates('note_type')
    def validate_note_type(self, key, note_type):
        if note_type not in ['general', 'call', 'email', 'meeting']:
            raise ValueError("Note type must be 'general', 'call', 'email', or 'meeting'")
        return note_type

    def has_reminder(self):
        """Check if note has an active reminder"""
        return self.reminder_date is not None and not self.is_reminder_completed

    def is_reminder_overdue(self):
        """Check if reminder is overdue - utilise timezone local cohérent"""
        if not self.reminder_date or self.is_reminder_completed:
            return False

        # Utiliser timezone local cohérent
        from utils import get_local_today
        try:
            today = get_local_today()
        except Exception:
            # Fallback si get_local_today échoue
            from datetime import date
            today = date.today()

        # Normaliser la date du rappel
        if isinstance(self.reminder_date, datetime):
            reminder_date = self.reminder_date.date()
        else:
            reminder_date = self.reminder_date

        return reminder_date < today

    def is_reminder_today(self):
        """Check if reminder is for today - utilise timezone local cohérent"""
        if not self.reminder_date or self.is_reminder_completed:
            return False

        # Utiliser timezone local cohérent
        from utils import get_local_today
        try:
            today = get_local_today()
        except Exception:
            # Fallback si get_local_today échoue
            from datetime import date
            today = date.today()

        # Normaliser la date du rappel
        if isinstance(self.reminder_date, datetime):
            reminder_date = self.reminder_date.date()
        else:
            reminder_date = self.reminder_date

        return reminder_date == today

    def is_reminder_upcoming(self):
        """Check if reminder is upcoming (future) - utilise timezone local cohérent"""
        if not self.reminder_date or self.is_reminder_completed:
            return False

        # Utiliser timezone local cohérent
        from utils import get_local_today
        try:
            today = get_local_today()
        except Exception:
            # Fallback si get_local_today échoue
            from datetime import date
            today = date.today()

        # Normaliser la date du rappel
        if isinstance(self.reminder_date, datetime):
            reminder_date = self.reminder_date.date()
        else:
            reminder_date = self.reminder_date

        return reminder_date > today

    def get_email_provider(self):
        """Retourner le fournisseur de courriel (outlook ou gmail)"""
        if self.outlook_message_id:
            return 'outlook'
        elif self.gmail_message_id:
            return 'gmail'
        return None

    def get_email_direction_display(self):
        """Retourner l'affichage de la direction de l'email"""
        direction_labels = {
            'sent': 'Envoyé',
            'received': 'Reçu',
            'reply': 'Réponse',
            'forward': 'Transféré'
        }
        return direction_labels.get(self.email_direction, '')

    def get_email_direction_badge_class(self):
        """Retourner la classe CSS Bootstrap pour le badge de direction"""
        badge_classes = {
            'sent': 'bg-primary',
            'received': 'bg-success',
            'reply': 'bg-info',
            'forward': 'bg-warning text-dark'
        }
        return badge_classes.get(self.email_direction, 'bg-secondary')

    def is_email_with_direction(self):
        """Vérifier si c'est un email avec direction définie"""
        return self.note_type == 'email' and self.email_direction is not None

    def has_replies(self):
        """Vérifier si cette note a des réponses/suites"""
        return len(self.replies) > 0 if self.replies else False

    def get_conversation_thread(self):
        """Récupérer toutes les notes de la même conversation"""
        if not self.conversation_id:
            return [self]

        from app import db
        return db.session.query(CommunicationNote).filter(
            CommunicationNote.conversation_id == self.conversation_id,
            CommunicationNote.company_id == self.company_id
        ).order_by(CommunicationNote.created_at).all()

    def get_conversation_count(self):
        """Retourner le nombre de messages dans cette conversation"""
        if not self.conversation_id:
            return 1

        from app import db
        return db.session.query(CommunicationNote).filter(
            CommunicationNote.conversation_id == self.conversation_id,
            CommunicationNote.company_id == self.company_id
        ).count()

    def __repr__(self):
        return f'<CommunicationNote {self.id}>'


class EmailTemplate(db.Model):
    """Model for email templates"""
    __tablename__ = 'email_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True)
    is_shared = db.Column(db.Boolean, default=False)  # True if shared with team
    is_editable_by_team = db.Column(db.Boolean, default=False)  # True if team can edit
    original_template_id = db.Column(db.Integer, db.ForeignKey('email_templates.id'), index=True)  # For duplicated templates
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_templates')
    original_template = db.relationship('EmailTemplate', remote_side=[id], backref='duplicates')

    def can_edit(self, user):
        """Check if user can edit this template"""
        # Creator can always edit
        if self.created_by == user.id:
            return True

        # Get user role in this template's company
        user_role = user.get_role_in_company(self.company_id)

        # Super admin can edit any template in their company
        if user_role == 'super_admin':
            return True

        # Admin can edit if template is editable by team
        if user_role == 'admin' and self.is_editable_by_team:
            return True

        return False

    def can_delete(self, user):
        """Check if user can delete this template"""
        # Creator can always delete their own templates
        if self.created_by == user.id:
            return True

        # Get user role in this template's company
        user_role = user.get_role_in_company(self.company_id)

        # Super admin can delete any template in their company
        if user_role == 'super_admin':
            return True

        return False

    def can_duplicate(self, user):
        """Check if user can duplicate this template"""
        # Get user role in this template's company
        user_role = user.get_role_in_company(self.company_id)

        # Users can only duplicate templates from their own company
        if user_role is None:
            return False

        # Anyone in the company can duplicate shared templates or their own
        if self.is_shared or self.created_by == user.id:
            return True

        # Super admin can duplicate any template in their company
        if user_role == 'super_admin':
            return True

        return False

    def get_visibility_display(self):
        """Get display text for template visibility"""
        if self.is_shared:
            return "Partagé avec l'équipe"
        else:
            return "Personnel"

    def __repr__(self):
        return f'<EmailTemplate {self.name}>'

class AccountingConnection(db.Model):
    """Model for accounting system connections"""
    __tablename__ = 'accounting_connections'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    system_type = db.Column(db.String(50), nullable=False)  # 'quickbooks', 'sage', 'xero', etc.
    system_name = db.Column(db.String(100), nullable=False)  # Display name

    # Connection details (encrypted)
    _access_token = db.Column('access_token', db.Text)  # OAuth access token - CHIFFRÉ (for Odoo: API Key)
    _refresh_token = db.Column('refresh_token', db.Text)  # OAuth refresh token - CHIFFRÉ
    token_expires_at = db.Column(db.DateTime)
    company_id_external = db.Column(db.String(100))  # External company ID (QB realmId, Odoo username)

    # Odoo-specific fields
    odoo_url = db.Column(db.String(255))  # Odoo server URL (e.g., https://mycompany.odoo.com)
    odoo_database = db.Column(db.String(100))  # Odoo database name

    # Configuration
    is_active = db.Column(db.Boolean, default=True)
    is_sandbox = db.Column(db.Boolean, default=False)  # Development/Sandbox mode (for QB, Odoo, etc.)
    auto_sync = db.Column(db.Boolean, default=True)
    last_sync_at = db.Column(db.DateTime)
    last_customers_sync_at = db.Column(db.DateTime)  # Business Central only: separate delta tracking for customers
    last_invoices_sync_at = db.Column(db.DateTime)  # Business Central only: separate delta tracking for invoices
    sync_frequency = db.Column(db.String(20), default='daily')  # 'hourly', 'daily', 'weekly'

    # Field mapping configuration (JSON)
    field_mapping = db.Column(db.Text)  # JSON string for field mappings
    sync_settings = db.Column(db.Text)  # JSON string for sync settings
    sync_stats = db.Column(db.JSON, nullable=True)  # JSON pour statistiques de synchronisation

    # Delta sync support (Phase 1 improvements)
    delta_enabled = db.Column(db.Boolean, default=False)
    delta_field = db.Column(db.String(100), default='SystemModifiedAt')
    full_sync_interval = db.Column(db.Integer, default=7)  # Force full sync every X days
    last_full_sync = db.Column(db.DateTime)
    batch_size_preference = db.Column(db.Integer, default=100)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @validates('system_type')
    def validate_system_type(self, key, system_type):
        allowed_types = ['quickbooks', 'sage', 'xero', 'wave', 'freshbooks', 'business_central', 'odoo', 'pennylane']
        if system_type not in allowed_types:
            raise ValueError(f"System type must be one of: {', '.join(allowed_types)}")
        return system_type

    @validates('sync_frequency')
    def validate_sync_frequency(self, key, sync_frequency):
        allowed_frequencies = ['hourly', 'daily', 'weekly', 'manual']
        if sync_frequency not in allowed_frequencies:
            raise ValueError(f"Sync frequency must be one of: {', '.join(allowed_frequencies)}")
        return sync_frequency

    @property
    def access_token(self):
        """Déchiffre le token d'accès OAuth"""
        if not self._access_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._access_token,
            'bc_access',
            self.company_id
        )

    @access_token.setter
    def access_token(self, value):
        """Chiffre le token d'accès OAuth avant stockage"""
        if not value:
            self._access_token = None
            return
        from security.encryption_service import encryption_service
        self._access_token = encryption_service.encrypt_token(
            value,
            'bc_access',
            self.company_id
        )

    @property
    def refresh_token(self):
        """Déchiffre le token de rafraîchissement OAuth"""
        if not self._refresh_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._refresh_token,
            'bc_refresh',
            self.company_id
        )

    @refresh_token.setter
    def refresh_token(self, value):
        """Chiffre le token de rafraîchissement OAuth avant stockage"""
        if not value:
            self._refresh_token = None
            return
        from security.encryption_service import encryption_service
        self._refresh_token = encryption_service.encrypt_token(
            value,
            'bc_refresh',
            self.company_id
        )

    def is_token_valid(self):
        """Check if the access token is still valid"""
        # Odoo uses permanent API keys (no expiration)
        if self.system_type == 'odoo':
            return bool(self.access_token and self.odoo_url and self.odoo_database)

        # OAuth-based systems (QuickBooks, Xero, Business Central)
        if not self.token_expires_at:
            return False
        return datetime.utcnow() < self.token_expires_at

    def needs_token_refresh(self):
        """Check if OAuth token needs refresh (within 30 minutes of expiry)

        Standard pattern for accounting connectors (QuickBooks, Business Central):
        - Access tokens expire after 1 hour
        - Refresh window: 30 minutes before expiration
        - Ensures 2 retry opportunities before expiration
        """
        if not self.token_expires_at or not self.refresh_token:
            return False

        # Refresh if token expires within 30 minutes
        refresh_threshold = datetime.utcnow() + timedelta(minutes=30)
        return self.token_expires_at <= refresh_threshold

    def get_field_mapping(self):
        """Get field mapping as dictionary"""
        import json
        if self.field_mapping:
            return json.loads(self.field_mapping)
        return self.get_default_field_mapping()

    def set_field_mapping(self, mapping_dict):
        """Set field mapping from dictionary"""
        import json
        self.field_mapping = json.dumps(mapping_dict)

    def get_default_field_mapping(self):
        """Get default field mapping - EMPTY for dynamic discovery"""
        # NO DEFAULT MAPPING - fields must be discovered dynamically
        # This ensures multi-tenant isolation
        return {}

    def get_sync_settings(self):
        """Get sync settings as dictionary"""
        import json
        if self.sync_settings:
            return json.loads(self.sync_settings)
        return self.get_default_sync_settings()

    def set_sync_settings(self, settings_dict):
        """Set sync settings from dictionary"""
        import json
        self.sync_settings = json.dumps(settings_dict)

    def get_default_sync_settings(self):
        """Get default sync settings"""
        return {
            'sync_customers': True,
            'sync_invoices': True,
            'sync_payments': False,  # For future use
            'only_unpaid_invoices': True,
            'date_range_days': 365,  # Only sync recent data
            'create_missing_clients': True,
            'update_existing_clients': True,
            'update_existing_invoices': True
        }

    def __repr__(self):
        return f'<AccountingConnection {self.system_name} ({self.system_type})>'

class BusinessCentralConfig(db.Model):
    """Configuration for Business Central OData connections"""
    __tablename__ = 'business_central_configs'

    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey('accounting_connections.id'), nullable=False, unique=True)

    # OData URLs configuration
    customers_odata_url = db.Column(db.Text)  # URL for customers table
    invoices_odata_url = db.Column(db.Text)  # URL for invoices table

    # OData filters
    customers_filter = db.Column(db.Text)  # OData filter for customers
    invoices_filter = db.Column(db.Text)  # OData filter for invoices

    # OData orderby fields - REQUIRED to avoid 400 errors with Business Central
    customers_orderby_field = db.Column(db.String(100))  # Field to use for ordering customers (e.g., 'No', 'Name')
    invoices_orderby_field = db.Column(db.String(100))  # Field to use for ordering invoices (e.g., 'Entry_No', 'Document_No')

    # GUID de la company BC pour le téléchargement de PDF (API REST v2.0)
    bc_company_guid = db.Column(db.String(100))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    connection = db.relationship('AccountingConnection', backref=db.backref('bc_config', uselist=False))

    def __repr__(self):
        return f'<BusinessCentralConfig {self.id}>'

class BusinessCentralSyncLog(db.Model):
    """Log spécifique pour les synchronisations Business Central"""
    __tablename__ = 'business_central_sync_logs'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)  # 'running', 'completed', 'error'
    clients_synced = db.Column(db.Integer, default=0)
    invoices_synced = db.Column(db.Integer, default=0)
    errors_count = db.Column(db.Integer, default=0)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)

    # Relations
    company = db.relationship('Company', backref='bc_sync_logs')


class SyncLog(db.Model):
    """Model for tracking sync operations"""
    __tablename__ = 'sync_logs'

    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey('accounting_connections.id'), nullable=False)
    sync_type = db.Column(db.String(50), nullable=False)  # 'manual', 'automatic', 'test'
    status = db.Column(db.String(30), nullable=False)  # 'running', 'completed', 'failed', 'interrupted', 'partial', 'completed_with_limit', 'stopped_manual'

    # Manual stop control
    manual_stop_requested_at = db.Column(db.DateTime, nullable=True)
    manual_stop_ack_at = db.Column(db.DateTime, nullable=True)

    # Statistics
    clients_synced = db.Column(db.Integer, default=0)
    invoices_synced = db.Column(db.Integer, default=0)
    errors_count = db.Column(db.Integer, default=0)

    # Checkpoint fields for resume capability
    last_processed_page = db.Column(db.Integer, default=0)
    last_processed_skip = db.Column(db.Integer, default=0)
    total_pages_estimated = db.Column(db.Integer)
    can_resume = db.Column(db.Boolean, default=False)

    # Performance metrics (Phase 1 improvements)
    pages_processed = db.Column(db.Integer, default=0)
    items_processed = db.Column(db.Integer, default=0)
    processing_rate = db.Column(db.Float)  # items/second
    avg_page_time = db.Column(db.Float)  # seconds per page
    estimated_completion = db.Column(db.DateTime)
    estimated_total = db.Column(db.Integer)
    last_activity_at = db.Column(db.DateTime)

    # Delta sync support
    is_delta_sync = db.Column(db.Boolean, default=False)
    delta_filter = db.Column(db.Text)
    entity_type = db.Column(db.String(50))  # 'customers', 'invoices'

    # Details
    error_message = db.Column(db.Text)
    details = db.Column(db.Text)  # JSON string with detailed info

    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    # Relationships
    connection = db.relationship('AccountingConnection', backref='sync_logs')

    # Index for efficient lookup of active syncs by connection
    __table_args__ = (
        db.Index('idx_sync_logs_connection_status', 'connection_id', 'status'),
    )

    def __repr__(self):
        return f'<SyncLog {self.id} - {self.status}>'

    def is_stop_requested(self):
        """Check if manual stop has been requested for this sync"""
        return self.manual_stop_requested_at is not None and self.manual_stop_ack_at is None

    def acknowledge_stop(self):
        """Acknowledge that the sync has processed the stop request"""
        self.manual_stop_ack_at = datetime.utcnow()
        self.status = 'stopped_manual'
        if not self.error_message:
            self.error_message = "Synchronisation arrêtée manuellement par un administrateur"
        self.completed_at = datetime.utcnow()

class ImportHistory(db.Model):
    """Model for tracking import operations"""
    __tablename__ = 'import_history'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    import_type = db.Column(db.String(50), nullable=False)  # 'clients', 'invoices'
    filename = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # 'success', 'failed', 'partial'

    # Statistics
    total_rows = db.Column(db.Integer, default=0)
    success_count = db.Column(db.Integer, default=0)
    error_count = db.Column(db.Integer, default=0)

    # Details
    error_details = db.Column(db.Text)  # JSON string with error messages

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    company = db.relationship('Company', backref='import_history')
    user = db.relationship('User', backref='import_history')

    __table_args__ = (
        db.Index('idx_ih_company', 'company_id'),
    )

    def get_status_class(self):
        """Get Bootstrap CSS class for status display"""
        status_classes = {
            'success': 'success',
            'failed': 'danger',
            'partial': 'warning'
        }
        return status_classes.get(self.status, 'secondary')

    def get_status_display(self):
        """Get display text for status"""
        status_display = {
            'success': 'Réussi',
            'failed': 'Échoué',
            'partial': 'Partiel'
        }
        return status_display.get(self.status, self.status)

    def __repr__(self):
        return f'<ImportHistory {self.id} - {self.import_type} - {self.status}>'

class ClientContact(db.Model):
    """Model for client contacts"""
    __tablename__ = 'client_contacts'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)

    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(50))
    position = db.Column(db.String(100))  # Fonction
    language = db.Column(db.String(5), default='fr')  # 'fr' for French, 'en' for English
    is_primary = db.Column(db.Boolean, default=False)  # Contact principal
    campaign_allowed = db.Column(db.Boolean, default=False)  # Autorisé à recevoir des campagnes

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_cc_client', 'client_id'),
        db.Index('idx_cc_company', 'company_id'),
    )

    def __repr__(self):
        return f'<ClientContact {self.first_name} {self.last_name} ({self.email})>'

    @property
    def full_name(self):
        """Return full name of contact"""
        return f"{self.first_name} {self.last_name}"

    @property
    def display_name(self):
        """Return display name for dropdowns: email - ClientName"""
        return f"{self.email} - {self.client.name}"

class EmailConfiguration(db.Model):
    """Model for email configuration per user per company"""
    __tablename__ = 'email_configurations'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)

    outlook_email = db.Column(db.String(255))
    gmail_email = db.Column(db.String(255))

    # Email signature
    email_signature = db.Column(db.Text)  # HTML signature content
    use_outlook_signature = db.Column(db.Boolean, default=False)  # Use Outlook's configured signature

    # OAuth tokens pour Outlook/Microsoft Graph API par entreprise - CHIFFRÉS
    _outlook_oauth_access_token = db.Column('outlook_oauth_access_token', db.Text)
    _outlook_oauth_refresh_token = db.Column('outlook_oauth_refresh_token', db.Text)
    outlook_oauth_token_expires = db.Column(db.DateTime)
    outlook_oauth_connected_at = db.Column(db.DateTime)

    # OAuth tokens pour Gmail/Google API par entreprise - CHIFFRÉS (DEPRECATED - utilisez SMTP)
    _gmail_oauth_access_token = db.Column('gmail_oauth_access_token', db.Text)
    _gmail_oauth_refresh_token = db.Column('gmail_oauth_refresh_token', db.Text)
    gmail_oauth_token_expires = db.Column(db.DateTime)
    gmail_oauth_connected_at = db.Column(db.DateTime)

    # Gmail SMTP configuration - CHIFFRÉ
    _gmail_smtp_app_password = db.Column('gmail_smtp_app_password', db.Text)

    # Delta sync pour Outlook - permet de ne récupérer que les changements
    outlook_delta_link = db.Column(db.Text)  # deltaLink retourné par Graph API
    outlook_delta_captured_at = db.Column(db.DateTime)  # Dernière capture delta réussie

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Constraints pour s'assurer qu'un utilisateur n'a qu'une configuration par entreprise
    __table_args__ = (db.UniqueConstraint('user_id', 'company_id', name='unique_user_company_email_config'),)

    # Relationships
    user = db.relationship('User', backref='email_configurations')
    company = db.relationship('Company', backref='email_configurations')

    @property
    def outlook_oauth_access_token(self):
        """Déchiffre le token d'accès OAuth"""
        if not self._outlook_oauth_access_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._outlook_oauth_access_token,
            'outlook_access',
            self.user_id
        )

    @outlook_oauth_access_token.setter
    def outlook_oauth_access_token(self, value):
        """Chiffre le token d'accès OAuth avant stockage"""
        if not value:
            self._outlook_oauth_access_token = None
            return
        from security.encryption_service import encryption_service
        self._outlook_oauth_access_token = encryption_service.encrypt_token(
            value,
            'outlook_access',
            self.user_id
        )

    @property
    def outlook_oauth_refresh_token(self):
        """Déchiffre le token de rafraîchissement OAuth"""
        if not self._outlook_oauth_refresh_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._outlook_oauth_refresh_token,
            'outlook_refresh',
            self.user_id
        )

    @outlook_oauth_refresh_token.setter
    def outlook_oauth_refresh_token(self, value):
        """Chiffre le token de rafraîchissement OAuth avant stockage"""
        if not value:
            self._outlook_oauth_refresh_token = None
            return
        from security.encryption_service import encryption_service
        self._outlook_oauth_refresh_token = encryption_service.encrypt_token(
            value,
            'outlook_refresh',
            self.user_id
        )

    @property
    def gmail_oauth_access_token(self):
        """Déchiffre le token d'accès OAuth Gmail"""
        if not self._gmail_oauth_access_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._gmail_oauth_access_token,
            'gmail_access',
            self.user_id
        )

    @gmail_oauth_access_token.setter
    def gmail_oauth_access_token(self, value):
        """Chiffre le token d'accès OAuth Gmail avant stockage"""
        if not value:
            self._gmail_oauth_access_token = None
            return
        from security.encryption_service import encryption_service
        self._gmail_oauth_access_token = encryption_service.encrypt_token(
            value,
            'gmail_access',
            self.user_id
        )

    @property
    def gmail_oauth_refresh_token(self):
        """Déchiffre le token de rafraîchissement OAuth Gmail"""
        if not self._gmail_oauth_refresh_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._gmail_oauth_refresh_token,
            'gmail_refresh',
            self.user_id
        )

    @gmail_oauth_refresh_token.setter
    def gmail_oauth_refresh_token(self, value):
        """Chiffre le token de rafraîchissement OAuth Gmail avant stockage"""
        if not value:
            self._gmail_oauth_refresh_token = None
            return
        from security.encryption_service import encryption_service
        self._gmail_oauth_refresh_token = encryption_service.encrypt_token(
            value,
            'gmail_refresh',
            self.user_id
        )

    @property
    def gmail_smtp_app_password(self):
        """Déchiffre le mot de passe d'application Gmail SMTP"""
        if not self._gmail_smtp_app_password:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._gmail_smtp_app_password,
            'gmail_smtp_password',
            self.user_id
        )

    @gmail_smtp_app_password.setter
    def gmail_smtp_app_password(self, value):
        """Chiffre le mot de passe d'application Gmail SMTP avant stockage"""
        if not value:
            self._gmail_smtp_app_password = None
            return
        from security.encryption_service import encryption_service
        self._gmail_smtp_app_password = encryption_service.encrypt_token(
            value,
            'gmail_smtp_password',
            self.user_id
        )

    def is_outlook_connected(self):
        """Check if Outlook is connected and token is valid"""
        return (self.outlook_oauth_access_token is not None and
                self.outlook_oauth_token_expires is not None and
                self.outlook_oauth_token_expires > datetime.utcnow())

    def is_outlook_token_expired(self):
        """Check if Outlook token is expired"""
        return (self.outlook_oauth_token_expires is not None and
                self.outlook_oauth_token_expires <= datetime.utcnow())

    def get_outlook_connection_status(self):
        """Get Outlook connection status for display"""
        if not self.outlook_oauth_access_token:
            return "non_connecte"
        elif self.is_outlook_token_expired():
            return "expire"
        else:
            return "connecte"

    def is_gmail_connected(self):
        """Check if Gmail is configured via SMTP"""
        return (self.gmail_email is not None and
                self.gmail_smtp_app_password is not None)

    def is_gmail_token_expired(self):
        """DEPRECATED - Gmail now uses SMTP, no tokens"""
        return False

    def get_gmail_connection_status(self):
        """Get Gmail connection status for display (SMTP-based)"""
        if self.is_gmail_connected():
            return "connecte"
        else:
            return "non_connecte"

    def needs_token_refresh(self):
        """Check if Outlook token needs refresh (within 30 minutes of expiry) - Gmail uses SMTP, no refresh needed

        Returns True if:
        - Token expiration is missing/null (safety check)
        - Token is already expired
        - Token will expire within 30 minutes
        """
        # If we have a refresh token but no access token or expiry, we need refresh
        if self.outlook_oauth_refresh_token and not self.outlook_oauth_access_token:
            return True

        # If no expiration timestamp, assume token needs refresh (safety)
        if not self.outlook_oauth_token_expires:
            return bool(self.outlook_oauth_refresh_token)

        # Check if token is expired or will expire within 30 minutes
        refresh_threshold = datetime.utcnow() + timedelta(minutes=30)
        return self.outlook_oauth_token_expires <= refresh_threshold

    def get_active_email_provider(self):
        """Determine which email provider is currently active (outlook or gmail)"""
        if self.is_gmail_connected():
            return 'gmail'
        elif self.is_outlook_connected():
            return 'outlook'
        return None

    def __repr__(self):
        return f'<EmailConfiguration {self.user_id}-{self.company_id}>'

class SystemEmailConfiguration(db.Model):
    """Model for system-wide email configuration (noreply, notifications, etc.)"""
    __tablename__ = 'system_email_configurations'

    id = db.Column(db.Integer, primary_key=True)
    config_name = db.Column(db.String(100), nullable=False, unique=True)  # 'password_reset', 'notifications', etc.
    email_address = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)

    # OAuth tokens pour Microsoft Graph API - CHIFFRÉS
    _outlook_oauth_access_token = db.Column('outlook_oauth_access_token', db.Text)
    _outlook_oauth_refresh_token = db.Column('outlook_oauth_refresh_token', db.Text)
    outlook_oauth_token_expires = db.Column(db.DateTime)
    outlook_oauth_connected_at = db.Column(db.DateTime)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def outlook_oauth_access_token(self):
        """Déchiffre le token d'accès OAuth système"""
        if not self._outlook_oauth_access_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._outlook_oauth_access_token,
            'system_outlook_access',
            self.id
        )

    @outlook_oauth_access_token.setter
    def outlook_oauth_access_token(self, value):
        """Chiffre le token d'accès OAuth système avant stockage"""
        if not value:
            self._outlook_oauth_access_token = None
            return
        from security.encryption_service import encryption_service
        self._outlook_oauth_access_token = encryption_service.encrypt_token(
            value,
            'system_outlook_access',
            self.id
        )

    @property
    def outlook_oauth_refresh_token(self):
        """Déchiffre le token de rafraîchissement OAuth système"""
        if not self._outlook_oauth_refresh_token:
            return None
        from security.encryption_service import encryption_service
        return encryption_service.decrypt_token(
            self._outlook_oauth_refresh_token,
            'system_outlook_refresh',
            self.id
        )

    @outlook_oauth_refresh_token.setter
    def outlook_oauth_refresh_token(self, value):
        """Chiffre le token de rafraîchissement OAuth système avant stockage"""
        if not value:
            self._outlook_oauth_refresh_token = None
            return
        from security.encryption_service import encryption_service
        self._outlook_oauth_refresh_token = encryption_service.encrypt_token(
            value,
            'system_outlook_refresh',
            self.id
        )

    def is_outlook_connected(self):
        """Check if Outlook is connected and token is valid"""
        return (self.outlook_oauth_access_token is not None and
                self.outlook_oauth_token_expires is not None and
                self.outlook_oauth_token_expires > datetime.utcnow())

    def is_outlook_token_expired(self):
        """Check if Outlook token is expired"""
        return (self.outlook_oauth_token_expires is not None and
                self.outlook_oauth_token_expires <= datetime.utcnow())

    def get_outlook_connection_status(self):
        """Get Outlook connection status for display"""
        if not self.outlook_oauth_access_token:
            return "non_connecte"
        elif self.is_outlook_token_expired():
            return "expire"
        else:
            return "connecte"

    def needs_token_refresh(self):
        """Check if token needs refresh (within 30 minutes of expiry)

        Returns True if:
        - Token expiration is missing/null (safety check)
        - Token is already expired
        - Token will expire within 30 minutes
        """
        # If we have a refresh token but no access token or expiry, we need refresh
        if self.outlook_oauth_refresh_token and not self.outlook_oauth_access_token:
            return True

        # If no expiration timestamp, assume token needs refresh (safety)
        if not self.outlook_oauth_token_expires:
            return bool(self.outlook_oauth_refresh_token)

        # Check if token is expired or will expire within 30 minutes
        refresh_threshold = datetime.utcnow() + timedelta(minutes=30)
        return self.outlook_oauth_token_expires <= refresh_threshold

    def __repr__(self):
        return f'<SystemEmailConfiguration {self.config_name}: {self.email_address}>'

class PasswordResetToken(db.Model):
    """Model for password reset tokens"""
    __tablename__ = 'password_reset_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    token = db.Column(db.String(128), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='password_reset_tokens')

    def is_valid(self):
        """Check if token is still valid"""
        return not self.used and self.expires_at > datetime.utcnow()

    def mark_as_used(self):
        """Mark token as used"""
        self.used = True
        db.session.commit()

    @staticmethod
    def generate_token():
        """Generate a secure random token"""
        import secrets
        return secrets.token_urlsafe(32)

    @staticmethod
    def create_reset_token(user):
        """Create a new reset token for a user"""
        from datetime import timedelta

        # Invalidate any existing tokens for this user
        existing_tokens = PasswordResetToken.query.filter_by(user_id=user.id, used=False).all()
        for token in existing_tokens:
            token.mark_as_used()

        # Create new token - PHASE 1 : constructeur corrigé
        reset_token = PasswordResetToken()
        reset_token.user_id = user.id
        reset_token.token = PasswordResetToken.generate_token()
        reset_token.expires_at = datetime.utcnow() + timedelta(minutes=30)  # 30 minutes expiry

        db.session.add(reset_token)
        db.session.commit()

        return reset_token

    def __repr__(self):
        return f'<PasswordResetToken {self.token[:8]}... for user {self.user_id}>'


class Notification(db.Model):
    """Model for real-time notifications to users"""
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # 'quickbooks_sync', 'email_sent', etc.
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.JSON)  # Additional data (sync stats, etc.)

    # Relationships
    user = db.relationship('User', backref='notifications')
    company = db.relationship('Company', backref='notifications')

    __table_args__ = (
        db.Index('idx_notif_user', 'user_id'),
        db.Index('idx_notif_company', 'company_id'),
    )

    def mark_as_read(self):
        """Mark notification as read"""
        self.is_read = True
        db.session.commit()

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'type': self.type,
            'title': self.title,
            'message': self.message,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat(),
            'data': self.data
        }

    @staticmethod
    def create_notification(user_id, company_id, type, title, message, data=None):
        """Create and save a new notification - PHASE 1 : constructeur corrigé"""
        notification = Notification()
        notification.user_id = user_id
        notification.company_id = company_id
        notification.type = type
        notification.title = title
        notification.message = message
        notification.data = data
        db.session.add(notification)
        db.session.commit()
        return notification

    def __repr__(self):
        return f'<Notification {self.type} for user {self.user_id}>'


class CompanySyncUsage(db.Model):
    """Model for tracking daily sync usage per company"""
    __tablename__ = 'company_sync_usage'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    sync_date = db.Column(db.Date, nullable=False)
    sync_count = db.Column(db.Integer, default=0)
    last_sync_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations
    company = db.relationship('Company', backref='sync_usage_records')

    __table_args__ = (db.UniqueConstraint('company_id', 'sync_date', name='unique_company_sync_date'),)

    @staticmethod
    def check_company_sync_limit(company_id):
        """Vérifie si la compagnie peut effectuer une synchronisation aujourd'hui"""
        from app import db
        from datetime import date

        # Récupérer la compagnie et son forfait
        company = Company.query.get(company_id)
        if not company or not company.plan_ref:
            return True  # Si pas de forfait défini, pas de limite

        # Vérifier la limite du forfait
        daily_limit = company.plan_ref.daily_sync_limit
        if daily_limit is None:
            return True  # Pas de limite définie = illimité

        # Récupérer l'usage d'aujourd'hui
        today = date.today()
        usage = CompanySyncUsage.query.filter_by(
            company_id=company_id,
            sync_date=today
        ).first()

        if not usage:
            return True  # Pas encore de sync aujourd'hui

        return usage.sync_count < daily_limit

    @staticmethod
    def increment_company_sync_count(company_id):
        """Incrémente le compteur de synchronisation pour la compagnie aujourd'hui"""
        from app import db
        from datetime import date

        today = date.today()
        usage = CompanySyncUsage.query.filter_by(
            company_id=company_id,
            sync_date=today
        ).first()

        if not usage:
            # Créer un nouvel enregistrement
            usage = CompanySyncUsage()
            usage.company_id = company_id
            usage.sync_date = today
            usage.sync_count = 1
            usage.last_sync_at = datetime.utcnow()
            db.session.add(usage)
        else:
            # Incrémenter l'existant
            usage.sync_count += 1
            usage.last_sync_at = datetime.utcnow()

        db.session.commit()
        return usage.sync_count

    @staticmethod
    def get_company_remaining_syncs(company_id):
        """Retourne le nombre de synchronisations restantes pour la compagnie aujourd'hui"""
        from datetime import date

        # Récupérer la compagnie et son forfait
        company = Company.query.get(company_id)
        if not company or not company.plan_ref:
            return None  # Illimité

        daily_limit = company.plan_ref.daily_sync_limit
        if daily_limit is None:
            return None  # Illimité

        # Récupérer l'usage d'aujourd'hui
        today = date.today()
        usage = CompanySyncUsage.query.filter_by(
            company_id=company_id,
            sync_date=today
        ).first()

        current_count = usage.sync_count if usage else 0
        return max(0, daily_limit - current_count)

    def __repr__(self):
        return f'<CompanySyncUsage {self.company_id} on {self.sync_date}: {self.sync_count}>'


class ConsentLog(db.Model):
    """
    Journal de consentement conforme RGPD/Loi 25
    Enregistre tous les consentements utilisateur pour les CGU, politique de confidentialité et cookies
    """
    __tablename__ = 'consent_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Nullable pour visiteurs anonymes (cookies)
    consent_type = db.Column(db.String(50), nullable=False)  # 'terms', 'privacy', 'cookies'
    consent_version = db.Column(db.String(20), nullable=False)  # Version du document accepté (ex: '2025-10-13')
    accepted = db.Column(db.Boolean, nullable=False)  # True = accepté, False = refusé
    ip_address = db.Column(db.String(45), nullable=True)  # IPv4 ou IPv6
    user_agent = db.Column(db.Text, nullable=True)  # Informations du navigateur
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Relation
    user = db.relationship('User', backref=db.backref('consent_logs', lazy='dynamic'))

    # Index pour optimiser les requêtes
    __table_args__ = (
        db.Index('idx_user_consent', 'user_id', 'consent_type'),
        db.Index('idx_consent_created', 'created_at'),
    )

    def __repr__(self):
        status = 'accepted' if self.accepted else 'declined'
        return f'<ConsentLog user={self.user_id} type={self.consent_type} {status}>'

    @staticmethod
    def get_user_latest_consent(user_id, consent_type):
        """Récupère le dernier consentement d'un utilisateur pour un type donné"""
        return ConsentLog.query.filter_by(
            user_id=user_id,
            consent_type=consent_type
        ).order_by(ConsentLog.created_at.desc()).first()

    @staticmethod
    def has_user_consented(user_id, consent_type, min_version=None):
        """Vérifie si l'utilisateur a consenti à un type donné (et optionnellement à une version minimale)"""
        latest = ConsentLog.get_user_latest_consent(user_id, consent_type)
        if not latest or not latest.accepted:
            return False

        if min_version and latest.consent_version < min_version:
            return False

        return True


class GuidePage(db.Model):
    """Model for guide/wiki pages in the marketing section"""
    __tablename__ = 'guide_pages'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True, nullable=False, index=True)
    meta_description = db.Column(db.String(300))
    content = db.Column(db.Text, nullable=False)  # Rich HTML content
    image_url = db.Column(db.Text)  # Optional featured image (Base64 data URI pour survie aux redéploiements)
    video_url = db.Column(db.Text)  # Optional embed video URL (Base64 data URI pour survie aux redéploiements)
    is_published = db.Column(db.Boolean, default=False)
    order = db.Column(db.Integer, default=0)  # For sorting pages
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<GuidePage {self.title}>'

    @validates('slug')
    def validate_slug(self, key, slug):
        """Ensure slug is URL-friendly"""
        import re
        if not slug:
            raise ValueError("Slug cannot be empty")
        # Convert to lowercase and replace spaces/special chars with hyphens
        slug = re.sub(r'[^\w\s-]', '', slug.lower())
        slug = re.sub(r'[\s_-]+', '-', slug)
        slug = slug.strip('-')
        return slug


class FileImportMapping(db.Model):
    """
    Model for Excel/CSV file import mapping configuration
    Stores permanent column mappings for automated imports from Excel (.xlsx) or CSV files
    """
    __tablename__ = 'file_import_mappings'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id', ondelete='CASCADE'), nullable=False, index=True)

    # Mapping configurations stored as JSON
    # Format: {"column_name_in_file": "finov_field_name"}
    # Example: {"Code": "code_client", "Nom": "name", "Email": "email", ...}
    client_column_mappings = db.Column(db.JSON, nullable=True)
    invoice_column_mappings = db.Column(db.JSON, nullable=True)

    # Language value mappings stored as JSON
    # Format: {"FR": "Français", "EN": "Anglais", ...}
    # Allows users to map their Excel language values to internal codes
    language_value_mappings = db.Column(db.JSON, nullable=True)

    # Configuration status
    is_configured = db.Column(db.Boolean, default=False, nullable=False)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Index for performance
    __table_args__ = (
        db.Index('idx_company_file_import', 'company_id'),
    )

    def __repr__(self):
        status = 'configured' if self.is_configured else 'not_configured'
        return f'<FileImportMapping company={self.company_id} {status}>'

    def get_client_mapping(self):
        """Get client column mapping as dictionary"""
        return self.client_column_mappings or {}

    def get_invoice_mapping(self):
        """Get invoice column mapping as dictionary"""
        return self.invoice_column_mappings or {}

    def is_client_configured(self):
        """Check if client mapping is configured"""
        return bool(self.client_column_mappings)

    def is_invoice_configured(self):
        """Check if invoice mapping is configured"""
        return bool(self.invoice_column_mappings)

    def get_language_mappings(self):
        """Get language value mappings as dictionary"""
        if self.language_value_mappings:
            return self.language_value_mappings
        return {'FR': 'Français', 'EN': 'Anglais'}

    @staticmethod
    def get_or_create_for_company(company_id):
        """Get existing mapping or create new one for company"""
        mapping = FileImportMapping.query.filter_by(company_id=company_id).first()
        if not mapping:
            mapping = FileImportMapping(company_id=company_id, is_configured=False)
            db.session.add(mapping)
            db.session.commit()
        return mapping


class ImportJob(db.Model):
    """Model for tracking background import jobs (CSV/Excel)"""
    __tablename__ = 'import_jobs'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    # Job metadata
    import_type = db.Column(db.String(20), nullable=False)  # 'clients' or 'invoices'
    import_mode = db.Column(db.String(20), nullable=False, default='append')  # 'append' or 'sync'
    filename = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer)  # Size in bytes

    # Status tracking
    status = db.Column(db.String(20), nullable=False, default='pending')  # 'pending', 'processing', 'completed', 'failed'
    progress = db.Column(db.Integer, default=0)  # 0-100
    total_rows = db.Column(db.Integer, default=0)
    processed_rows = db.Column(db.Integer, default=0)
    success_count = db.Column(db.Integer, default=0)
    error_count = db.Column(db.Integer, default=0)

    # Sync mode counters
    created_count = db.Column(db.Integer, default=0)  # New records created
    updated_count = db.Column(db.Integer, default=0)  # Existing records updated
    deleted_count = db.Column(db.Integer, default=0)  # Records deleted (sync mode only)

    # Results
    errors = db.Column(db.JSON)  # List of error messages
    warnings = db.Column(db.JSON)  # List of warning messages
    result_message = db.Column(db.Text)  # Final result message

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    company = db.relationship('Company', backref=db.backref('import_jobs', lazy='dynamic'))
    user = db.relationship('User', backref=db.backref('import_jobs', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_ij_company', 'company_id'),
        db.Index('idx_ij_status', 'status'),
    )

    def __repr__(self):
        return f'<ImportJob {self.id} {self.import_type} {self.status}>'

    def get_duration(self):
        """Get duration of job in seconds"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        elif self.started_at:
            return (datetime.utcnow() - self.started_at).total_seconds()
        return 0

    def is_finished(self):
        """Check if job is finished (completed or failed)"""
        return self.status in ['completed', 'failed']

    def mark_as_processing(self):
        """Mark job as started"""
        self.status = 'processing'
        self.started_at = datetime.utcnow()

    def mark_as_completed(self, success_count, error_count, errors=None, created=0, updated=0, deleted=0):
        """Mark job as completed with detailed counts"""
        self.status = 'completed'
        self.completed_at = datetime.utcnow()
        self.success_count = success_count
        self.error_count = error_count
        self.errors = errors or []
        self.created_count = created
        self.updated_count = updated
        self.deleted_count = deleted
        self.progress = 100

    def mark_as_failed(self, error_message):
        """Mark job as failed"""
        self.status = 'failed'
        self.completed_at = datetime.utcnow()
        self.result_message = error_message
        self.errors = [error_message]

    def update_progress(self, processed_rows, total_rows):
        """Update job progress"""
        self.processed_rows = processed_rows
        self.total_rows = total_rows
        if total_rows > 0:
            self.progress = int((processed_rows / total_rows) * 100)


class CampaignStatus(enum.Enum):
    """Enum pour les statuts de campagne"""
    DRAFT = 'draft'  # Brouillon - en cours de configuration
    PROCESSING = 'processing'  # En cours de génération des courriels
    READY = 'ready'  # Prête - courriels générés, en attente d'envoi
    IN_PROGRESS = 'in_progress'  # En cours d'envoi
    STOPPED = 'stopped'  # Arrêtée d'urgence - envoi interrompu
    COMPLETED = 'completed'  # Terminée - tous les courriels envoyés
    CANCELLED = 'cancelled'  # Annulée


class CampaignEmailStatus(enum.Enum):
    """Enum pour les statuts de courriel de campagne"""
    PENDING = 'pending'  # En attente de génération
    GENERATED = 'generated'  # Brouillon généré, prêt à envoyer
    SENDING = 'sending'  # En cours d'envoi
    SENT = 'sent'  # Envoyé avec succès (batch)
    SENT_MANUALLY = 'sent_manually'  # Envoyé manuellement (individuel)
    FAILED = 'failed'  # Échec d'envoi
    SKIPPED = 'skipped'  # Ignoré (exclu manuellement)


class Campaign(db.Model):
    """Model for email campaigns"""
    __tablename__ = 'campaigns'
    __table_args__ = (
        db.Index('idx_campaign_company', 'company_id'),
        db.Index('idx_campaign_status', 'status'),
        db.Index('idx_campaign_created_by', 'created_by'),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.Enum(CampaignStatus), default=CampaignStatus.DRAFT, nullable=False)

    # Filtres de ciblage (stockés en JSON pour flexibilité)
    filter_collector_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    filter_unassigned_collector = db.Column(db.Boolean, default=False)  # True = uniquement clients sans collecteur
    filter_age_days = db.Column(db.Integer, default=0)  # 0 = Tous (inclus Courant), 31, 61, 91, etc.
    filter_representative = db.Column(db.String(200), nullable=True)
    filter_contact_language = db.Column(db.String(5), nullable=True)  # 'fr', 'en', ou None pour tous
    filter_without_notes = db.Column(db.Boolean, default=False)  # True = uniquement clients sans notes de communication

    # Paramètres parent/enfants
    include_children_in_parent_report = db.Column(db.Boolean, default=True)  # Si True, enfants exclus de la liste

    # Paramètre destinataire
    recipient_type = db.Column(db.String(50), default='primary')  # 'primary', 'campaign_contacts', 'both'

    # Contenu du courriel (template)
    email_subject = db.Column(db.String(500), nullable=True)
    email_content = db.Column(db.Text, nullable=True)
    email_template_id = db.Column(db.Integer, db.ForeignKey('email_templates.id'), nullable=True, index=True)

    # Options de pièces jointes
    attach_pdf_statement = db.Column(db.Boolean, default=True)
    attach_excel_statement = db.Column(db.Boolean, default=False)
    attachment_language = db.Column(db.String(5), default='fr')  # Deprecated - langue auto par contact/client

    # Clients sélectionnés (JSON array des IDs) - utilisé entre étapes 2 et 3
    selected_client_ids = db.Column(db.Text, nullable=True)  # JSON: [1, 2, 3, ...]

    # Statistiques
    total_emails = db.Column(db.Integer, default=0)
    emails_sent = db.Column(db.Integer, default=0)
    emails_failed = db.Column(db.Integer, default=0)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    processing_started_at = db.Column(db.DateTime, nullable=True)
    processing_completed_at = db.Column(db.DateTime, nullable=True)
    sending_started_at = db.Column(db.DateTime, nullable=True)
    sending_completed_at = db.Column(db.DateTime, nullable=True)

    # Arrêt d'urgence
    stop_requested = db.Column(db.Boolean, default=False)
    stop_requested_at = db.Column(db.DateTime, nullable=True)
    stop_requested_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    # Relationships
    company = db.relationship('Company', backref=db.backref('campaigns', lazy='dynamic'))
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_campaigns')
    collector_filter = db.relationship('User', foreign_keys=[filter_collector_id])
    email_template = db.relationship('EmailTemplate', backref='campaigns')
    emails = db.relationship('CampaignEmail', backref='campaign', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Campaign {self.id} "{self.name}" - {self.status.value}>'

    @property
    def status_display(self):
        """Version française du statut"""
        status_map = {
            CampaignStatus.DRAFT: 'Brouillon',
            CampaignStatus.PROCESSING: 'En préparation',
            CampaignStatus.READY: 'Prête',
            CampaignStatus.IN_PROGRESS: 'En cours',
            CampaignStatus.STOPPED: 'Arrêtée',
            CampaignStatus.COMPLETED: 'Terminée',
            CampaignStatus.CANCELLED: 'Annulée'
        }
        return status_map.get(self.status, self.status.value)

    @property
    def status_class(self):
        """Classe CSS Bootstrap pour le statut"""
        status_classes = {
            CampaignStatus.DRAFT: 'secondary',
            CampaignStatus.PROCESSING: 'info',
            CampaignStatus.READY: 'primary',
            CampaignStatus.IN_PROGRESS: 'warning',
            CampaignStatus.STOPPED: 'danger',
            CampaignStatus.COMPLETED: 'success',
            CampaignStatus.CANCELLED: 'danger'
        }
        return status_classes.get(self.status, 'secondary')

    @property
    def progress_percentage(self):
        """Pourcentage de progression (envoi)"""
        if self.total_emails == 0:
            return 0
        return int((self.emails_sent / self.total_emails) * 100)

    def can_be_sent(self):
        """Vérifie si la campagne peut être envoyée (READY, IN_PROGRESS ou STOPPED avec des courriels à envoyer)"""
        return self.status in (CampaignStatus.READY, CampaignStatus.IN_PROGRESS, CampaignStatus.STOPPED)

    @property
    def has_emails_to_send(self):
        """Vérifie s'il reste des courriels à envoyer"""
        from models import CampaignEmail, CampaignEmailStatus
        return CampaignEmail.query.filter_by(
            campaign_id=self.id,
            status=CampaignEmailStatus.GENERATED
        ).count() > 0

    def can_be_edited(self):
        """Vérifie si la campagne peut être modifiée"""
        return self.status == CampaignStatus.DRAFT


class CampaignEmail(db.Model):
    """Model for individual emails within a campaign"""
    __tablename__ = 'campaign_emails'
    __table_args__ = (
        db.Index('idx_campaign_email_campaign', 'campaign_id'),
        db.Index('idx_campaign_email_client', 'client_id'),
        db.Index('idx_campaign_email_status', 'status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)

    status = db.Column(db.Enum(CampaignEmailStatus), default=CampaignEmailStatus.PENDING, nullable=False)

    # Destinataires (peut être multiple si type='both')
    to_emails = db.Column(db.Text, nullable=True)  # JSON array des emails

    # Contenu généré (personnalisé pour ce client)
    email_subject = db.Column(db.String(500), nullable=True)
    email_content = db.Column(db.Text, nullable=True)

    # Informations client au moment de la génération (snapshot pour audit)
    client_code = db.Column(db.String(50), nullable=True)
    client_name = db.Column(db.String(200), nullable=True)
    client_balance = db.Column(db.Numeric(15, 2), nullable=True)

    # Pièces jointes (chemins vers fichiers temporaires ou données binaires)
    pdf_attachment_data = db.Column(db.LargeBinary, nullable=True)
    excel_attachment_data = db.Column(db.LargeBinary, nullable=True)

    # Validation sécurité: IDs des clients dont les factures sont incluses dans les pièces jointes
    # Permet de vérifier avant envoi que les factures appartiennent bien au client ou ses enfants
    invoice_client_ids = db.Column(db.Text, nullable=True)  # JSON array: [123, 456, ...]

    # LAZY GENERATION: Snapshot des IDs de factures exactes au moment de la création
    # Utilisé pour générer PDF/Excel à la demande avec garantie d'isolation des données
    invoice_ids_snapshot = db.Column(db.Text, nullable=True)  # JSON array: [789, 790, ...]

    # Résultat d'envoi
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    client = db.relationship('Client', backref=db.backref('campaign_emails', lazy='dynamic'))

    def __repr__(self):
        return f'<CampaignEmail {self.id} - Campaign {self.campaign_id} - Client {self.client_code}>'

    @property
    def status_display(self):
        """Version française du statut"""
        status_map = {
            CampaignEmailStatus.PENDING: 'En attente',
            CampaignEmailStatus.GENERATED: 'Prêt',
            CampaignEmailStatus.SENDING: 'En cours d\'envoi',
            CampaignEmailStatus.SENT: 'Envoyé',
            CampaignEmailStatus.SENT_MANUALLY: 'Envoyé manuellement',
            CampaignEmailStatus.FAILED: 'Échec',
            CampaignEmailStatus.SKIPPED: 'Ignoré'
        }
        return status_map.get(self.status, self.status.value)

    @property
    def status_class(self):
        """Classe CSS Bootstrap pour le statut"""
        status_classes = {
            CampaignEmailStatus.PENDING: 'secondary',
            CampaignEmailStatus.GENERATED: 'primary',
            CampaignEmailStatus.SENDING: 'info',
            CampaignEmailStatus.SENT: 'success',
            CampaignEmailStatus.SENT_MANUALLY: 'info',
            CampaignEmailStatus.FAILED: 'danger',
            CampaignEmailStatus.SKIPPED: 'warning'
        }
        return status_classes.get(self.status, 'secondary')

    def get_to_emails_list(self):
        """Retourne la liste des emails destinataires"""
        import json
        if not self.to_emails:
            return []
        try:
            return json.loads(self.to_emails)
        except Exception:
            return [self.to_emails] if self.to_emails else []

    def set_to_emails_list(self, emails_list):
        """Définit la liste des emails destinataires"""
        import json
        self.to_emails = json.dumps(emails_list) if emails_list else None

    def get_invoice_client_ids_list(self):
        """Retourne la liste des IDs clients dont les factures sont incluses"""
        import json
        if not self.invoice_client_ids:
            return []
        try:
            return json.loads(self.invoice_client_ids)
        except Exception:
            return []

    def set_invoice_client_ids_list(self, client_ids_list):
        """Définit la liste des IDs clients dont les factures sont incluses"""
        import json
        self.invoice_client_ids = json.dumps(list(set(client_ids_list))) if client_ids_list else None

    def get_invoice_ids_snapshot_list(self):
        """LAZY GENERATION: Retourne la liste des IDs de factures figées au moment de la création"""
        import json
        if not self.invoice_ids_snapshot:
            return []
        try:
            return json.loads(self.invoice_ids_snapshot)
        except Exception:
            return []

    def set_invoice_ids_snapshot_list(self, invoice_ids_list):
        """LAZY GENERATION: Stocke la liste des IDs de factures pour génération différée"""
        import json
        self.invoice_ids_snapshot = json.dumps(list(set(invoice_ids_list))) if invoice_ids_list else None


class ReceivablesSnapshot(db.Model):
    """
    Snapshot des comptes à recevoir pour le suivi de l'évolution dans le temps.
    Créé automatiquement après chaque synchronisation ou import de factures.
    """
    __tablename__ = 'receivables_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)

    snapshot_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    total_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    current_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    days_0_30_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    days_31_60_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    days_61_90_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    days_90_plus_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)

    trigger_type = db.Column(db.String(20), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_snapshot_company_date', 'company_id', 'snapshot_date'),
    )

    company = db.relationship('Company', backref=db.backref('receivables_snapshots', lazy='dynamic'))

    def __repr__(self):
        return f'<ReceivablesSnapshot {self.company_id} @ {self.snapshot_date}>'

    @classmethod
    def get_history(cls, company_id, period='month', bucket='total', limit=100, year=None, month=None, week_start=None):
        """
        Récupère l'historique des snapshots pour le graphique avec navigation drill-down.

        Navigation drill-down :
        - Vue Année: tous les mois de l'année sélectionnée (ou toutes les années si year=None)
        - Vue Mois: toutes les semaines du mois sélectionné
        - Vue Semaine: tous les jours de la semaine sélectionnée
        - Vue Jour: tous les snapshots du jour sélectionné

        Args:
            company_id: ID de l'entreprise
            period: 'year', 'month', 'week', 'day'
            bucket: 'total', 'current', '0-30', '31-60', '61-90', '90+'
            limit: Nombre max de points
            year: Année sélectionnée (optionnel)
            month: Mois sélectionné 1-12 (optionnel)
            week_start: Date de début de semaine YYYY-MM-DD (optionnel)
        """
        from sqlalchemy import func, text
        from datetime import datetime, timedelta
        from app import db

        bucket_column_map = {
            'total': 'total_amount',
            'current': 'current_amount',
            '0-30': 'days_0_30_amount',
            '31-60': 'days_31_60_amount',
            '61-90': 'days_61_90_amount',
            '90+': 'days_90_plus_amount'
        }

        amount_col = bucket_column_map.get(bucket, 'total_amount')

        # SQL-01: Validation defensive - whitelist des colonnes autorisees
        VALID_COLUMNS = {'total_amount', 'current_amount', 'days_0_30_amount',
                         'days_31_60_amount', 'days_61_90_amount', 'days_90_plus_amount'}
        if amount_col not in VALID_COLUMNS:
            amount_col = 'total_amount'
        now = datetime.utcnow()

        if period == 'year':
            # Vue Année : 1 point par année (toutes les années disponibles)
            sql = text(f"""
                SELECT DISTINCT ON (DATE_TRUNC('year', snapshot_date))
                    snapshot_date, {amount_col}
                FROM receivables_snapshots
                WHERE company_id = :company_id
                ORDER BY DATE_TRUNC('year', snapshot_date), snapshot_date DESC
                LIMIT :limit
            """)
            result = db.session.execute(sql, {'company_id': company_id, 'limit': limit})
            rows = result.fetchall()

            return [{
                'date': row[0].isoformat(),
                'value': float(row[1]),
                'year': row[0].year
            } for row in rows]

        elif period == 'month':
            # Vue Mois : 12 mois de l'année sélectionnée
            if year:
                start_date = datetime(year, 1, 1)
                end_date = datetime(year, 12, 31, 23, 59, 59)
            else:
                start_date = (now - timedelta(days=365)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                end_date = now

            sql = text(f"""
                SELECT DISTINCT ON (DATE_TRUNC('month', snapshot_date))
                    snapshot_date, {amount_col}
                FROM receivables_snapshots
                WHERE company_id = :company_id
                    AND snapshot_date >= :start_date
                    AND snapshot_date <= :end_date
                ORDER BY DATE_TRUNC('month', snapshot_date), snapshot_date DESC
            """)
            result = db.session.execute(sql, {
                'company_id': company_id,
                'start_date': start_date,
                'end_date': end_date
            })
            rows = result.fetchall()

            return [{
                'date': row[0].isoformat(),
                'value': float(row[1]),
                'year': row[0].year,
                'month': row[0].month
            } for row in rows]

        elif period == 'week':
            # Vue Semaine : jours de la semaine ou du mois sélectionné
            if week_start:
                # Semaine spécifique
                try:
                    start_date = datetime.fromisoformat(week_start)
                    end_date = start_date + timedelta(days=7)
                except Exception:
                    start_date = now - timedelta(days=7)
                    end_date = now
            elif year and month:
                # Toutes les semaines du mois sélectionné (affiche par jour)
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                start_date = datetime(year, month, 1)
                end_date = datetime(year, month, last_day, 23, 59, 59)
            else:
                start_date = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
                end_date = now

            sql = text(f"""
                SELECT DISTINCT ON (DATE(snapshot_date))
                    snapshot_date, {amount_col}
                FROM receivables_snapshots
                WHERE company_id = :company_id
                    AND snapshot_date >= :start_date
                    AND snapshot_date <= :end_date
                ORDER BY DATE(snapshot_date), snapshot_date DESC
            """)
            result = db.session.execute(sql, {
                'company_id': company_id,
                'start_date': start_date,
                'end_date': end_date
            })
            rows = result.fetchall()

            return [{
                'date': row[0].isoformat(),
                'value': float(row[1]),
                'year': row[0].year,
                'month': row[0].month,
                'day': row[0].day,
                'week_start': (row[0] - timedelta(days=row[0].weekday())).strftime('%Y-%m-%d')
            } for row in rows]

        elif period == 'day':
            # Vue Jour : tous les snapshots du jour sélectionné
            if week_start:
                # Jour spécifique passé via week_start
                try:
                    target_date = datetime.fromisoformat(week_start)
                    start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    end_of_day = start_of_day + timedelta(days=1)
                except Exception:
                    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    end_of_day = start_of_day + timedelta(days=1)
            else:
                start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end_of_day = start_of_day + timedelta(days=1)

            snapshots = cls.query.filter(
                cls.company_id == company_id,
                cls.snapshot_date >= start_of_day,
                cls.snapshot_date < end_of_day
            ).order_by(cls.snapshot_date.asc()).limit(limit).all()

            return [{
                'date': s.snapshot_date.isoformat(),
                'value': float(getattr(s, amount_col))
            } for s in snapshots]

        return []


# =====================================================
# SYSTÈME D'AUDIT - Logs utilisateurs et Cron Jobs
# =====================================================

class AuditLog(db.Model):
    """Model for tracking all user actions in the application"""
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user_email = db.Column(db.String(255), nullable=True)

    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=True)

    action = db.Column(db.String(100), nullable=False, index=True)

    entity_type = db.Column(db.String(50), nullable=True, index=True)
    entity_id = db.Column(db.Integer, nullable=True)
    entity_name = db.Column(db.String(255), nullable=True)

    old_value = db.Column(db.JSON, nullable=True)
    new_value = db.Column(db.JSON, nullable=True)

    details = db.Column(db.JSON, nullable=True)

    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(512), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User', backref=db.backref('audit_logs', lazy='dynamic'))
    company = db.relationship('Company', backref=db.backref('audit_logs', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_audit_logs_action_date', 'action', 'created_at'),
        db.Index('idx_audit_logs_user_date', 'user_id', 'created_at'),
        db.Index('idx_audit_logs_company_date', 'company_id', 'created_at'),
    )

    def __repr__(self):
        return f'<AuditLog {self.action} by {self.user_email} at {self.created_at}>'

    @classmethod
    def log(cls, action, entity_type=None, entity_id=None, entity_name=None,
            old_value=None, new_value=None, details=None, user=None, company=None):
        """
        Create an audit log entry.

        Args:
            action: The action performed (login_success, client_created, etc.)
            entity_type: Type of entity affected (client, invoice, template, etc.)
            entity_id: ID of the entity affected
            entity_name: Human-readable name of the entity
            old_value: Previous value (for updates)
            new_value: New value (for creates/updates)
            details: Additional details as dict
            user: User object (optional, auto-detected from current_user)
            company: Company object (optional, auto-detected from user's selected company)
        """
        from flask import request, has_request_context
        from flask_login import current_user

        try:
            if user is None and has_request_context():
                try:
                    if current_user and current_user.is_authenticated:
                        user = current_user
                except Exception:
                    pass

            if company is None and user:
                try:
                    company = user.get_selected_company()
                except Exception:
                    pass

            ip_address = None
            user_agent = None
            if has_request_context():
                ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
                if ip_address:
                    ip_address = ip_address.split(',')[0].strip()
                user_agent = request.headers.get('User-Agent', '')[:512]

            log_entry = cls(
                user_id=user.id if user else None,
                user_email=user.email if user else None,
                company_id=company.id if company else None,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                old_value=old_value,
                new_value=new_value,
                details=details,
                ip_address=ip_address,
                user_agent=user_agent
            )

            db.session.add(log_entry)
            db.session.commit()

            return log_entry

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            if current_app:
                current_app.logger.error(f"Erreur lors de l'enregistrement de l'audit log: {e}")
            return None

    @classmethod
    def log_with_session(cls, session, action, entity_type=None, entity_id=None, entity_name=None,
            old_value=None, new_value=None, details=None, user=None, company=None):
        """
        Create an audit log entry using a specific database session.
        Used by background workers that have their own session.
        """
        try:
            log_entry = cls(
                user_id=user.id if user else None,
                user_email=user.email if user else None,
                company_id=company.id if company else None,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                old_value=old_value,
                new_value=new_value,
                details=details,
                ip_address='background_worker',
                user_agent='ImportWorker/1.0'
            )
            session.add(log_entry)
            return log_entry
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Erreur audit log avec session: {e}")
            return None

    @classmethod
    def get_logs(cls, user_id=None, company_id=None, action=None, entity_type=None,
                 start_date=None, end_date=None, limit=100, offset=0):
        """Query audit logs with filters."""
        query = cls.query

        if user_id:
            query = query.filter(cls.user_id == user_id)
        if company_id:
            query = query.filter(cls.company_id == company_id)
        if action:
            query = query.filter(cls.action == action)
        if entity_type:
            query = query.filter(cls.entity_type == entity_type)
        if start_date:
            query = query.filter(cls.created_at >= start_date)
        if end_date:
            query = query.filter(cls.created_at <= end_date)

        return query.order_by(cls.created_at.desc()).offset(offset).limit(limit).all()


class CronJobLog(db.Model):
    """Model for tracking cron job executions"""
    __tablename__ = 'cron_job_logs'

    id = db.Column(db.Integer, primary_key=True)

    job_name = db.Column(db.String(100), nullable=False, index=True)

    status = db.Column(db.String(20), nullable=False, default='started')

    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)

    duration_seconds = db.Column(db.Float, nullable=True)

    details = db.Column(db.JSON, nullable=True)

    error_message = db.Column(db.Text, nullable=True)

    items_processed = db.Column(db.Integer, nullable=True)
    items_failed = db.Column(db.Integer, nullable=True)
    items_skipped = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.Index('idx_cron_job_logs_name_date', 'job_name', 'created_at'),
        db.Index('idx_cron_job_logs_status', 'status'),
    )

    def __repr__(self):
        return f'<CronJobLog {self.job_name} - {self.status} at {self.started_at}>'

    @classmethod
    def start_job(cls, job_name, details=None):
        """
        Record the start of a cron job execution.
        Returns the log entry to be updated when job completes.
        """
        try:
            log_entry = cls(
                job_name=job_name,
                status='running',
                started_at=datetime.utcnow(),
                details=details or {}
            )

            db.session.add(log_entry)
            db.session.commit()

            return log_entry

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            if current_app:
                current_app.logger.error(f"Erreur lors du démarrage du log cron job: {e}")
            return None

    def complete_job(self, status='success', error_message=None,
                     items_processed=None, items_failed=None, items_skipped=None,
                     details=None):
        """
        Mark the job as completed with final status.

        Args:
            status: 'success', 'warning', 'failed'
            error_message: Error message if failed
            items_processed: Number of items successfully processed
            items_failed: Number of items that failed
            items_skipped: Number of items skipped
            details: Additional details to merge with existing
        """
        try:
            self.ended_at = datetime.utcnow()
            self.status = status
            self.duration_seconds = (self.ended_at - self.started_at).total_seconds()

            if error_message:
                self.error_message = error_message[:5000]

            if items_processed is not None:
                self.items_processed = items_processed
            if items_failed is not None:
                self.items_failed = items_failed
            if items_skipped is not None:
                self.items_skipped = items_skipped

            if details:
                existing_details = self.details or {}
                existing_details.update(details)
                self.details = existing_details

            db.session.commit()

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            if current_app:
                current_app.logger.error(f"Erreur lors de la complétion du log cron job: {e}")

    @classmethod
    def get_recent_jobs(cls, job_name=None, status=None, limit=50):
        """Get recent cron job executions."""
        query = cls.query

        if job_name:
            query = query.filter(cls.job_name == job_name)
        if status:
            query = query.filter(cls.status == status)

        return query.order_by(cls.created_at.desc()).limit(limit).all()

    @classmethod
    def get_job_stats(cls, job_name, days=7):
        """Get statistics for a specific job over the last N days."""
        from sqlalchemy import func

        start_date = datetime.utcnow() - timedelta(days=days)

        stats = db.session.query(
            func.count(cls.id).label('total'),
            func.sum(db.case((cls.status == 'success', 1), else_=0)).label('success_count'),
            func.sum(db.case((cls.status == 'failed', 1), else_=0)).label('failed_count'),
            func.sum(db.case((cls.status == 'warning', 1), else_=0)).label('warning_count'),
            func.avg(cls.duration_seconds).label('avg_duration'),
            func.max(cls.duration_seconds).label('max_duration'),
            func.min(cls.duration_seconds).label('min_duration')
        ).filter(
            cls.job_name == job_name,
            cls.created_at >= start_date,
            cls.status.in_(['success', 'failed', 'warning'])
        ).first()

        return {
            'total': stats.total or 0,
            'success_count': stats.success_count or 0,
            'failed_count': stats.failed_count or 0,
            'warning_count': stats.warning_count or 0,
            'avg_duration': round(stats.avg_duration, 2) if stats.avg_duration else 0,
            'max_duration': round(stats.max_duration, 2) if stats.max_duration else 0,
            'min_duration': round(stats.min_duration, 2) if stats.min_duration else 0,
            'success_rate': round((stats.success_count or 0) / stats.total * 100, 1) if stats.total else 0
        }


class ReceivedPayment(db.Model):
    """Historique des paiements reçus pour le calcul du DMP (Délai Moyen de Paiement).

    Alimentée par sync_payments() dans chaque connecteur comptable.
    Indépendante du cycle de vie des factures — les factures peuvent être
    supprimées après paiement sans perdre l'historique.
    """
    __tablename__ = 'received_payments'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)

    invoice_number = db.Column(db.String(100), nullable=False)
    invoice_date = db.Column(db.Date, nullable=True)
    invoice_due_date = db.Column(db.Date, nullable=True)
    original_invoice_amount = db.Column(db.Numeric(10, 2), nullable=True)

    payment_date = db.Column(db.Date, nullable=False)
    payment_amount = db.Column(db.Numeric(10, 2), nullable=True)

    source = db.Column(db.String(30), nullable=False)
    external_payment_id = db.Column(db.String(150), nullable=True)
    external_invoice_id = db.Column(db.String(150), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship('Company', backref=db.backref('received_payments', lazy='dynamic'))
    client = db.relationship('Client', backref=db.backref('received_payments', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('company_id', 'source', 'external_payment_id', 'invoice_number',
                            name='uq_received_payment_dedup'),
        db.Index('idx_received_payments_company', 'company_id'),
        db.Index('idx_received_payments_client', 'client_id'),
        db.Index('idx_received_payments_payment_date', 'payment_date'),
    )

    def __repr__(self):
        return f'<ReceivedPayment {self.invoice_number} {self.payment_date} {self.source}>'

    @property
    def days_to_payment_from_invoice(self):
        """Jours entre date de facture et date de paiement"""
        if self.payment_date and self.invoice_date:
            return (self.payment_date - self.invoice_date).days
        return None

    @property
    def days_to_payment_from_due(self):
        """Jours entre date d'échéance et date de paiement (positif = retard)"""
        if self.payment_date and self.invoice_due_date:
            return (self.payment_date - self.invoice_due_date).days
        return None


class RecoveryCode(db.Model):
    """Recovery codes for 2FA fallback authentication"""
    __tablename__ = 'recovery_codes'
    __table_args__ = (
        db.Index('idx_recovery_codes_user_used', 'user_id', 'used'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    code_hash = db.Column(db.String(64), nullable=False)  # SHA-256
    used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('recovery_codes', cascade='all, delete-orphan'))

    @staticmethod
    def generate_codes(user_id, count=5):
        """Generate new recovery codes for a user.

        Deletes existing unused codes, generates `count` new ones,
        stores SHA-256 hashes, and returns plaintext codes (one-time display).
        """
        import secrets
        import hashlib

        # Delete old unused codes for this user
        RecoveryCode.query.filter_by(user_id=user_id, used=False).delete()

        codes_plaintext = []
        for _ in range(count):
            raw = secrets.token_hex(4).upper()  # 8 hex chars
            code = f"{raw[:4]}-{raw[4:]}"
            code_hash = hashlib.sha256(code.encode()).hexdigest()

            rc = RecoveryCode()
            rc.user_id = user_id
            rc.code_hash = code_hash
            db.session.add(rc)
            codes_plaintext.append(code)

        db.session.commit()
        return codes_plaintext

    @staticmethod
    def verify_code(user_id, code):
        """Verify a recovery code. Marks it as used if valid. Returns True/False."""
        import hashlib

        code_clean = code.strip().upper()
        code_hash = hashlib.sha256(code_clean.encode()).hexdigest()

        match = RecoveryCode.query.filter_by(
            user_id=user_id, code_hash=code_hash, used=False
        ).first()

        if match:
            match.used = True
            match.used_at = datetime.utcnow()
            db.session.commit()
            return True
        return False

    def __repr__(self):
        return f'<RecoveryCode user={self.user_id} used={self.used}>'


class UserTOTP(db.Model):
    """TOTP (Google Authenticator) configuration per user"""
    __tablename__ = 'user_totp'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    secret_encrypted = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('totp_config', uselist=False, cascade='all, delete-orphan'))

    @property
    def secret(self):
        from security.encryption_service import EncryptionService
        return EncryptionService().decrypt_field(self.secret_encrypted, 'totp_secret', self.user_id)

    @secret.setter
    def secret(self, value):
        from security.encryption_service import EncryptionService
        self.secret_encrypted = EncryptionService().encrypt_field(value, 'totp_secret', self.user_id)

    def verify(self, code):
        import pyotp
        totp = pyotp.TOTP(self.secret)
        return totp.verify(code, valid_window=1)

    def get_provisioning_uri(self, user_email):
        import pyotp
        totp = pyotp.TOTP(self.secret)
        return totp.provisioning_uri(name=user_email, issuer_name='Finov Relance')

    def __repr__(self):
        return f'<UserTOTP user={self.user_id} active={self.is_active}>'


# Add the relationship after all models are defined
Company.accounting_connections = db.relationship('AccountingConnection', backref='company', lazy=True, cascade='all, delete-orphan')

# Event listeners to automatically update timestamps
@event.listens_for(Company, 'before_update')
@event.listens_for(Client, 'before_update')
@event.listens_for(Invoice, 'before_update')
@event.listens_for(EmailTemplate, 'before_update')
@event.listens_for(AccountingConnection, 'before_update')
@event.listens_for(SyncLog, 'before_update')
@event.listens_for(CompanySyncUsage, 'before_update')
@event.listens_for(GuidePage, 'before_update')
@event.listens_for(FileImportMapping, 'before_update')
@event.listens_for(ImportJob, 'before_update')
@event.listens_for(Campaign, 'before_update')
@event.listens_for(CampaignEmail, 'before_update')
def receive_before_update(mapper, connection, target):
    target.updated_at = datetime.utcnow()


