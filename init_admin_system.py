"""
Script d'initialisation du système administrateur
Ce script migre les données existantes et initialise les forfaits par défaut
"""
from app import app
from models import db, Plan, Company

def init_plans():
    """Initialise les forfaits par défaut"""
    print("Initialisation des forfaits...")

    plans_data = [
        {
            'name': 'decouverte',
            'display_name': 'Découverte',
            'description': 'Forfait gratuit avec fonctionnalités de base',
            'is_active': True,
            'is_free': True,
            'max_clients': 10,
            'allows_email_sending': False,
            'allows_email_connection': False,
            'allows_accounting_connection': False,
            'allows_team_management': False,
            'allows_email_templates': False
        },
        {
            'name': 'relance',
            'display_name': 'Relance',
            'description': 'Forfait avec connexion comptable et gestion équipe',
            'is_active': True,
            'is_free': False,
            'max_clients': None,  # Unlimited
            'allows_email_sending': False,
            'allows_email_connection': False,
            'allows_accounting_connection': True,
            'allows_team_management': True,
            'allows_email_templates': False
        },
        {
            'name': 'relance_plus',
            'display_name': 'Relance+',
            'description': 'Forfait complet avec toutes les fonctionnalités',
            'is_active': True,
            'is_free': False,
            'max_clients': None,  # Unlimited
            'allows_email_sending': True,
            'allows_email_connection': True,
            'allows_accounting_connection': True,
            'allows_team_management': True,
            'allows_email_templates': True
        }
    ]

    for plan_data in plans_data:
        existing_plan = Plan.query.filter_by(name=plan_data['name']).first()
        if not existing_plan:
            plan = Plan(**plan_data)
            db.session.add(plan)
            print(f"  Forfait '{plan_data['display_name']}' créé")
        else:
            print(f"  Forfait '{plan_data['display_name']}' existe déjà")

    db.session.commit()
    print("Forfaits initialisés ✓")

def migrate_existing_companies():
    """Migre les entreprises existantes vers le nouveau système"""
    print("Migration des entreprises existantes...")

    companies = Company.query.all()

    for company in companies:
        # Associer le forfait basé sur le champ legacy 'plan'
        if not company.plan_id:
            plan = Plan.query.filter_by(name=company.plan).first()
            if plan:
                company.plan_id = plan.id
                print(f"  Entreprise '{company.name}' associée au forfait '{plan.display_name}'")
            else:
                # Forfait par défaut si non trouvé
                default_plan = Plan.query.filter_by(name='decouverte').first()
                if default_plan:
                    company.plan_id = default_plan.id
                    company.plan = 'decouverte'
                    print(f"  Entreprise '{company.name}' migré vers forfait découverte par défaut")

        # Marquer comme compte gratuit si c'est un compte découverte sans abonnement Stripe
        if (company.plan == 'decouverte' and
            not company.stripe_subscription_id and
            company.is_free_account is None):
            company.is_free_account = True
            print(f"  Entreprise '{company.name}' marquée comme compte gratuit")

        # Initialiser les limites de clients pour les comptes découverte
        if company.plan == 'decouverte' and company.client_limit is None:
            company.client_limit = 10
            # Compter les clients existants
            from models import Client
            current_count = Client.query.filter_by(company_id=company.id).count()
            company.current_client_count = current_count
            print(f"  Limite clients définie pour '{company.name}': {current_count}/10")

    db.session.commit()
    print("Migration des entreprises terminée ✓")

def create_test_plans():
    """Crée des forfaits de test pour Stripe"""
    print("Création des forfaits de test...")

    test_plans_data = [
        {
            'name': 'relance_test',
            'display_name': 'Relance (Test)',
            'description': 'Version test du forfait Relance',
            'is_active': False,  # Désactivé par défaut
            'is_free': False,
            'max_clients': None,
            'allows_email_sending': False,
            'allows_email_connection': False,
            'allows_accounting_connection': True,
            'allows_team_management': True,
            'allows_email_templates': False,
            # REFONTE STRIPE 2.0 : Champs unifiés
            'stripe_product_id': 'prod_test_relance',
            'stripe_price_id': 'price_test_relance'
        },
        {
            'name': 'relance_plus_test',
            'display_name': 'Relance+ (Test)',
            'description': 'Version test du forfait Relance+',
            'is_active': False,  # Désactivé par défaut
            'is_free': False,
            'max_clients': None,
            'allows_email_sending': True,
            'allows_email_connection': True,
            'allows_accounting_connection': True,
            'allows_team_management': True,
            'allows_email_templates': True,
            # REFONTE STRIPE 2.0 : Champs unifiés
            'stripe_product_id': 'prod_test_relance_plus',
            'stripe_price_id': 'price_test_relance_plus'
        }
    ]

    for plan_data in test_plans_data:
        existing_plan = Plan.query.filter_by(name=plan_data['name']).first()
        if not existing_plan:
            plan = Plan(**plan_data)
            db.session.add(plan)
            print(f"  Forfait test '{plan_data['display_name']}' créé (désactivé)")
        else:
            print(f"  Forfait test '{plan_data['display_name']}' existe déjà")

    db.session.commit()
    print("Forfaits de test créés ✓")

def display_superuser_instructions():
    """Affiche les instructions pour créer le premier super admin"""
    print("\n" + "="*60)
    print("INSTRUCTIONS POUR CRÉER LE PREMIER SUPER ADMINISTRATEUR")
    print("="*60)
    print("1. Connectez-vous à votre base de données PostgreSQL")
    print("2. Trouvez votre utilisateur dans la table 'users'")
    print("3. Exécutez cette commande SQL pour vous donner les droits super admin :")
    print()
    print("   UPDATE users SET is_superuser = true WHERE email = 'votre@email.com';")
    print()
    print("4. Une fois fait, vous pourrez accéder au panel admin à :")
    print("   https://votre-domaine.com/admin-dashboard")
    print()
    print("IMPORTANT : Remplacez 'votre@email.com' par votre vraie adresse email.")
    print("="*60)

def main():
    """Fonction principale de migration"""
    # app is already imported directly

    with app.app_context():
        print("Début de l'initialisation du système administrateur...")
        print()

        # Créer les tables si elles n'existent pas
        db.create_all()
        print("Tables de base de données vérifiées ✓")
        print()

        # Initialiser les forfaits
        init_plans()
        print()

        # Créer les forfaits de test
        create_test_plans()
        print()

        # Migrer les entreprises existantes
        migrate_existing_companies()
        print()

        # Afficher les instructions pour le super admin
        display_superuser_instructions()

        print("Initialisation terminée avec succès ! 🎉")

if __name__ == "__main__":
    main()