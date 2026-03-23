"""
Tests unitaires pour le nouveau système de webhooks Stripe
Couvre les 5 événements maîtres et les règles métier
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from app import app, db
from models import Company, User, UserCompany, SubscriptionAuditLog
from stripe_finov.webhooks.handler import (
    handle_checkout_completed,
    handle_subscription_created,
    handle_subscription_updated,
    handle_payment_succeeded,
    handle_payment_action_required,
    auto_convert_excess_users_to_reader,
    apply_cancel_to_free,
    apply_downgrade
)

@pytest.fixture
def client():
    """Client de test Flask"""
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()

@pytest.fixture
def company_with_users():
    """Créer une entreprise avec plusieurs utilisateurs pour les tests"""
    with app.app_context():
        # Créer l'entreprise
        company = Company(
            name="Test Company",
            email="test@company.com",
            plan="relance",
            status="active",
            stripe_customer_id="cus_test123",
            stripe_subscription_id="sub_test123",
            quantity_licenses=5
        )
        db.session.add(company)
        db.session.flush()

        # Créer les utilisateurs
        users = []
        for i in range(6):  # 1 super admin + 5 employés
            user = User(
                first_name=f"User{i}",
                last_name=f"Test{i}",
                email=f"user{i}@test.com",
                password_hash="hashed_password"
            )
            db.session.add(user)
            db.session.flush()
            users.append(user)

            # Créer la relation UserCompany
            role = "super_admin" if i == 0 else "employe"
            uc = UserCompany(
                user_id=user.id,
                company_id=company.id,
                role=role,
                is_active=True
            )
            db.session.add(uc)

        db.session.commit()
        return company, users

def create_stripe_event(event_type, data, event_id="evt_test123"):
    """Helper pour créer un événement Stripe de test"""
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": data
        }
    }

class TestCheckoutSessionCompleted:
    """Tests pour checkout.session.completed"""

    def test_checkout_with_company_id(self, client):
        """Test checkout avec company_id dans metadata"""
        with app.app_context():
            # Créer une entreprise
            company = Company(name="New Company", email="new@company.com", plan="decouverte")
            db.session.add(company)
            db.session.commit()
            company_id = company.id

            # Créer l'événement checkout
            session_data = {
                "id": "cs_test123",
                "customer": "cus_new123",
                "subscription": "sub_new123",
                "metadata": {
                    "company_id": str(company_id)
                }
            }
            event = create_stripe_event("checkout.session.completed", session_data)

            # Mock Stripe API
            with patch('stripe.Subscription.retrieve') as mock_retrieve:
                mock_subscription = MagicMock()
                mock_subscription.status = "active"
                mock_subscription.items.data = [MagicMock(quantity=3)]
                mock_subscription.cancel_at = None
                mock_retrieve.return_value = mock_subscription

                response = handle_checkout_completed(event)

                # Vérifications
                assert response == ("ok", 200)

                company = Company.query.get(company_id)
                assert company.stripe_customer_id == "cus_new123"
                assert company.stripe_subscription_id == "sub_new123"
                assert company.status == "active"
                assert company.quantity_licenses == 3

    def test_checkout_orphan_no_company_id(self, client):
        """Test checkout sans company_id (orphelin)"""
        with app.app_context():
            session_data = {
                "id": "cs_orphan",
                "customer": "cus_orphan",
                "metadata": {}  # Pas de company_id
            }
            event = create_stripe_event("checkout.session.completed", session_data)

            response = handle_checkout_completed(event)

            # Vérifier qu'on retourne OK mais qu'on log l'orphelin
            assert response == ("ok", 200)

            # Vérifier l'audit log
            audit = SubscriptionAuditLog.query.filter_by(
                event_type="checkout.session.completed"
            ).first()
            assert audit is not None
            assert audit.before_json.get("orphan") == True

class TestSubscriptionCreated:
    """Tests pour customer.subscription.created"""

    def test_subscription_created_existing_customer(self, company_with_users):
        """Test création d'abonnement pour client existant"""
        company, users = company_with_users

        with app.app_context():
            subscription_data = {
                "id": "sub_created",
                "customer": company.stripe_customer_id,
                "status": "active",
                "items": {
                    "data": [{"quantity": 10}]
                }
            }
            event = create_stripe_event("customer.subscription.created", subscription_data)

            response = handle_subscription_created(event, company)

            assert response == ("ok", 200)
            assert company.status == "active"
            assert company.quantity_licenses == 10
            assert company.pending_plan is None
            assert company.pending_quantity is None

class TestSubscriptionUpdated:
    """Tests pour customer.subscription.updated - ÉVÉNEMENT MAÎTRE"""

    def test_pending_cancellation(self, company_with_users):
        """Test annulation différée"""
        company, users = company_with_users

        with app.app_context():
            cancel_at = int((datetime.utcnow() + timedelta(days=30)).timestamp())
            subscription_data = {
                "id": "sub_cancel",
                "customer": company.stripe_customer_id,
                "status": "active",
                "cancel_at_period_end": True,
                "cancel_at": cancel_at,
                "items": {"data": [{"quantity": 5}]}
            }
            event = create_stripe_event("customer.subscription.updated", subscription_data)

            response = handle_subscription_updated(event, company)

            assert response == ("ok", 200)
            assert company.status == "pending_cancellation"
            assert company.cancel_at is not None

    def test_immediate_cancellation(self, company_with_users):
        """Test annulation immédiate - passage au plan free"""
        company, users = company_with_users

        with app.app_context():
            # Vérifier l'état initial
            initial_active_users = len([uc for uc in company.user_companies if uc.is_active])
            assert initial_active_users == 6  # 1 super admin + 5 employés

            subscription_data = {
                "id": "sub_canceled",
                "customer": company.stripe_customer_id,
                "status": "canceled",
                "cancel_at_period_end": False,
                "items": {"data": [{"quantity": 0}]}
            }
            event = create_stripe_event("customer.subscription.updated", subscription_data)

            response = handle_subscription_updated(event, company)

            assert response == ("ok", 200)
            assert company.plan == "free"
            assert company.status == "canceled"
            assert company.quantity_licenses == 1

            # Vérifier que seul le super admin reste actif
            active_users = [uc for uc in company.user_companies if uc.is_active]
            assert len(active_users) == 1
            assert active_users[0].role == "super_admin"

    def test_license_reduction(self, company_with_users):
        """Test réduction immédiate de licences - conversion en lecteurs"""
        company, users = company_with_users

        with app.app_context():
            # État initial : 5 licences, 6 utilisateurs (1 super admin + 5 employés)
            assert company.quantity_licenses == 5

            subscription_data = {
                "id": "sub_reduced",
                "customer": company.stripe_customer_id,
                "status": "active",
                "cancel_at_period_end": False,
                "items": {"data": [{"quantity": 3}]}  # Réduction à 3 licences
            }
            event = create_stripe_event("customer.subscription.updated", subscription_data)

            response = handle_subscription_updated(event, company)

            assert response == ("ok", 200)
            assert company.quantity_licenses == 3

            # Vérifier les conversions en lecteurs (2 employés les plus récents)
            readers = [uc for uc in company.user_companies if uc.role == "lecteur"]
            assert len(readers) == 2  # 5 employés - 3 licences = 2 conversions

            # Vérifier que le super admin n'est jamais converti
            super_admin = [uc for uc in company.user_companies if uc.role == "super_admin"]
            assert len(super_admin) == 1
            assert super_admin[0].is_active == True

    def test_license_increase(self, company_with_users):
        """Test augmentation de licences"""
        company, users = company_with_users

        with app.app_context():
            subscription_data = {
                "id": "sub_increased",
                "customer": company.stripe_customer_id,
                "status": "active",
                "cancel_at_period_end": False,
                "items": {"data": [{"quantity": 10}]}  # Augmentation à 10 licences
            }
            event = create_stripe_event("customer.subscription.updated", subscription_data)

            response = handle_subscription_updated(event, company)

            assert response == ("ok", 200)
            assert company.quantity_licenses == 10

            # Aucune conversion ne doit avoir lieu
            readers = [uc for uc in company.user_companies if uc.role == "lecteur"]
            assert len(readers) == 0

    def test_pending_downgrade(self, company_with_users):
        """Test downgrade différé"""
        company, users = company_with_users

        with app.app_context():
            expires_at = int((datetime.utcnow() + timedelta(days=30)).timestamp())
            subscription_data = {
                "id": "sub_downgrade",
                "customer": company.stripe_customer_id,
                "status": "active",
                "cancel_at_period_end": False,
                "pending_update": {
                    "subscription_items": [{"quantity": 2}],
                    "expires_at": expires_at
                },
                "items": {"data": [{"quantity": 5}]}  # Quantité actuelle
            }
            event = create_stripe_event("customer.subscription.updated", subscription_data)

            response = handle_subscription_updated(event, company)

            assert response == ("ok", 200)
            assert company.status == "pending_downgrade"
            assert company.pending_quantity == 2
            assert company.pending_expires_at is not None

class TestPaymentEvents:
    """Tests pour les événements de paiement"""

    def test_payment_succeeded(self, company_with_users):
        """Test paiement réussi"""
        company, users = company_with_users

        with app.app_context():
            invoice_data = {
                "id": "in_success",
                "customer": company.stripe_customer_id,
                "status": "paid"
            }
            event = create_stripe_event("invoice.payment_succeeded", invoice_data)

            response = handle_payment_succeeded(event, company)

            assert response == ("ok", 200)
            # Le paiement ne doit pas modifier le plan/licences
            assert company.plan == "relance"
            assert company.quantity_licenses == 5

    def test_payment_action_required(self, company_with_users):
        """Test action requise (3DS)"""
        company, users = company_with_users

        with app.app_context():
            invoice_data = {
                "id": "in_action",
                "customer": company.stripe_customer_id,
                "status": "open"
            }
            event = create_stripe_event("invoice.payment_action_required", invoice_data)

            response = handle_payment_action_required(event, company)

            assert response == ("ok", 200)
            assert company.status == "past_due"

class TestIdempotence:
    """Tests pour l'idempotence"""

    def test_duplicate_event_ignored(self, company_with_users):
        """Test qu'un événement dupliqué n'est pas retraité"""
        company, users = company_with_users

        with app.app_context():
            # Créer un log d'audit existant
            audit = SubscriptionAuditLog(
                company_id=company.id,
                event_type="customer.subscription.updated",
                stripe_event_id="evt_duplicate",
                before_json={},
                after_json={}
            )
            db.session.add(audit)
            db.session.commit()

            # Mock is_idempotent pour retourner True
            with patch('stripe_finov.webhooks.handler.is_idempotent', return_value=True):
                # Envoyer un événement webhook
                with app.test_client() as client:
                    with patch('stripe.Webhook.construct_event') as mock_construct:
                        mock_construct.return_value = {
                            "id": "evt_duplicate",
                            "type": "customer.subscription.updated",
                            "data": {"object": {"customer": "cus_test"}}
                        }

                        response = client.post(
                            '/webhook/stripe',
                            data='test_payload',
                            headers={'Stripe-Signature': 'test_sig'}
                        )

                        assert response.status_code == 200

                        # Vérifier qu'aucun nouveau log n'a été créé
                        audit_count = SubscriptionAuditLog.query.filter_by(
                            stripe_event_id="evt_duplicate"
                        ).count()
                        assert audit_count == 1  # Seulement l'original

class TestFiltering:
    """Tests pour le filtrage des clients inconnus"""

    def test_unknown_customer_ignored(self, client):
        """Test que les événements pour clients inconnus sont ignorés"""
        with app.app_context():
            # Événement pour un customer qui n'existe pas
            subscription_data = {
                "id": "sub_unknown",
                "customer": "cus_unknown",
                "status": "active",
                "items": {"data": [{"quantity": 5}]}
            }

            with app.test_client() as client:
                with patch('stripe.Webhook.construct_event') as mock_construct:
                    mock_construct.return_value = create_stripe_event(
                        "customer.subscription.updated",
                        subscription_data
                    )

                    response = client.post(
                        '/webhook/stripe',
                        data='test_payload',
                        headers={'Stripe-Signature': 'test_sig'}
                    )

                    assert response.status_code == 200

                    # Vérifier qu'aucun log n'a été créé
                    audit_count = SubscriptionAuditLog.query.count()
                    assert audit_count == 0

class TestBusinessFunctions:
    """Tests pour les fonctions métier"""

    def test_auto_convert_excess_users(self, company_with_users):
        """Test conversion automatique des utilisateurs excédentaires"""
        company, users = company_with_users

        with app.app_context():
            # Convertir pour 3 licences (2 conversions nécessaires)
            converted = auto_convert_excess_users_to_reader(company, 3)

            assert len(converted) == 2

            # Vérifier que les bons utilisateurs sont convertis (les plus récents)
            readers = UserCompany.query.filter_by(
                company_id=company.id,
                role="lecteur"
            ).all()
            assert len(readers) == 2

            # Vérifier que le super admin n'est pas touché
            super_admin = UserCompany.query.filter_by(
                company_id=company.id,
                role="super_admin"
            ).first()
            assert super_admin is not None
            assert super_admin.is_active == True

    def test_apply_cancel_to_free(self, company_with_users):
        """Test application de l'annulation - plan free"""
        company, users = company_with_users

        with app.app_context():
            apply_cancel_to_free(company)

            assert company.plan == "free"
            assert company.quantity_licenses == 1
            assert company.status == "canceled"

            # Seul le super admin doit rester actif
            active_users = UserCompany.query.filter_by(
                company_id=company.id,
                is_active=True
            ).all()
            assert len(active_users) == 1
            assert active_users[0].role == "super_admin"

    def test_apply_downgrade(self, company_with_users):
        """Test application du downgrade"""
        company, users = company_with_users

        with app.app_context():
            # Préparer un downgrade
            company.pending_plan = "relance"
            company.pending_quantity = 2

            apply_downgrade(company)

            assert company.plan == "relance"
            assert company.quantity_licenses == 2
            assert company.status == "active"
            assert company.pending_plan is None
            assert company.pending_quantity is None

            # Vérifier les conversions (3 employés doivent être convertis)
            readers = UserCompany.query.filter_by(
                company_id=company.id,
                role="lecteur"
            ).all()
            assert len(readers) == 3