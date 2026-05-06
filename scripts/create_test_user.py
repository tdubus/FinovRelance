"""Crée un utilisateur de test pour le développement local.

Usage (depuis le conteneur app) :
    docker exec finovrelance-app-1 python scripts/create_test_user.py

Identifiants créés :
    Email    : dev@finovrelance.dev
    Password : DevTest2026!

REFUSE explicitement de s'exécuter en production : si FLASK_ENV=production
ou si DATABASE_URL pointe vers une base prod (neon, supabase, ou contient
"prod"), le script sort avec un code d'erreur sans toucher à la DB.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Garde-fou prod : refuser l'exécution si l'environnement ressemble à prod.
_FLASK_ENV = os.environ.get('FLASK_ENV', '').lower()
_DB_URL = os.environ.get('DATABASE_URL', '').lower()
_PROD_MARKERS = ('production', 'prod')
_PROD_DB_HOSTS = ('neon.tech', 'supabase.co', '.amazonaws.com', 'rds.')
if _FLASK_ENV in _PROD_MARKERS or any(m in _DB_URL for m in _PROD_DB_HOSTS) or 'prod' in _DB_URL:
    print(
        "ERREUR: scripts/create_test_user.py refuse de s'exécuter en production.\n"
        f"  FLASK_ENV='{_FLASK_ENV}', DATABASE_URL host suspecté prod.\n"
        "  Ce script crée un super_admin avec un mot de passe connu — usage dev local uniquement.",
        file=sys.stderr,
    )
    sys.exit(1)

from app import app, db
from models import User, Company, UserCompany
from werkzeug.security import generate_password_hash

EMAIL = 'dev@finovrelance.dev'
PASSWORD = 'DevTest2026!'
COMPANY_NAME = 'DevTest'

with app.app_context():
    user = User.query.filter_by(email=EMAIL).first()
    if user:
        print(f'User {EMAIL} already exists (id={user.id}). Resetting password.')
        user.password_hash = generate_password_hash(PASSWORD)
        user.must_change_password = False
        user.is_superuser = True
        db.session.commit()
    else:
        company = Company.query.filter_by(name=COMPANY_NAME).first()
        if not company:
            company = Company(
                name=COMPANY_NAME,
                email=EMAIL,
                currency='CAD',
                timezone='America/Montreal',
                plan='decouverte',
                plan_status='active',
            )
            db.session.add(company)
            db.session.flush()
            print(f'Company "{COMPANY_NAME}" created (id={company.id})')

        user = User(
            email=EMAIL,
            first_name='Dev',
            last_name='Tester',
            password_hash=generate_password_hash(PASSWORD),
            must_change_password=False,
            is_superuser=True,
            terms_accepted_at=datetime.utcnow(),
            terms_version_accepted='1.0',
            last_company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()

        link = UserCompany(
            user_id=user.id,
            company_id=company.id,
            role='super_admin',
            is_active=True,
        )
        db.session.add(link)
        db.session.commit()
        print(f'User {EMAIL} created (id={user.id}) linked to company id={company.id} as super_admin')

    print('---')
    print(f'  Email    : {EMAIL}')
    print(f'  Password : {PASSWORD}')
    print('---')
    print('2FA bypassed in dev (DEV_BYPASS_2FA_EMAIL + app.debug). Login goes')
    print('straight to the dashboard. Append ?theme=v2 to any URL to preview UI v2.')
