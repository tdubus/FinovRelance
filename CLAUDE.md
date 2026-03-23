# FinovRelance

## Description
Application SaaS Flask/Python de gestion de comptes recevables.
Deux domaines servis par la meme app : finov-relance.com (marketing) et app.finov-relance.com (SaaS).

## Stack technique
- Backend : Flask 3.1, Python 3.11, Gunicorn
- Base de donnees : PostgreSQL 16 (Neon en legacy, VPS en production)
- ORM : SQLAlchemy 2.0 avec db.create_all() (pas de Flask-Migrate actif)
- Paiement : Stripe (abonnements, webhooks via stripe_finov/)
- Email : Microsoft Graph API (OAuth), Gmail SMTP (fallback)
- OAuth : Microsoft (MSAL), Xero, Business Central, QuickBooks
- Chiffrement : AES-256 (tokens OAuth stockes, via ENCRYPTION_MASTER_KEY)
- PDF : ReportLab
- Images : Pillow
- Deploiement : Docker, Coolify, VPS

## Structure du projet
```
FinovRelance/
├── app.py                  # Initialisation Flask, extensions, blueprints, middleware
├── main.py                 # Point d'entree (from app import app)
├── config.py               # Classes de configuration
├── constants.py            # Constantes globales (timeouts, pool, etc.)
├── models.py               # Modeles SQLAlchemy principaux
├── onboarding_models.py    # Modeles onboarding/inscription
├── views.py                # Blueprints main_bp, receivable_bp, profile_bp, users_bp, import_bp
├── views/                  # Blueprints par fonctionnalite
│   ├── admin_views.py      # admin_bp (/admin)
│   ├── auth_views.py       # auth_bp (/auth)
│   ├── campaign_views.py   # campaign_bp (/campaigns)
│   ├── client_views.py     # client_bp (/clients)
│   ├── company_views.py    # company_bp (/company)
│   ├── email_views.py      # email_bp (/emails)
│   ├── import_views.py     # import_bp (/import)
│   ├── invoice_views.py    # invoice_bp (/invoices)
│   ├── marketing_views.py  # marketing_bp (/)
│   ├── note_views.py       # note_bp (/notes)
│   ├── receivable_views.py # receivable_bp (/receivables)
│   ├── reminder_views.py   # reminder_bp (/reminders)
│   ├── stripe_onboarding.py # onboarding_bp (/onboarding)
│   └── user_views.py       # users_bp (/users)
├── jobs/                   # Cron jobs (endpoints HTTP)
│   ├── apply_pending_changes.py  # jobs_bp
│   ├── database_backup.py        # backup_bp
│   ├── refresh_email_tokens.py   # refresh_tokens_bp
│   ├── refresh_accounting_tokens.py # refresh_accounting_bp
│   └── sync_email_v3.py          # sync_emails_v3_bp
├── stripe_finov/           # Module Stripe (webhooks, events, notifications)
├── security/               # CSP middleware, encryption, logging
├── utils/                  # Utilitaires (audit, permissions, email, etc.)
├── templates/              # Templates Jinja2 (par section)
├── static/                 # CSS, JS, fonts, uploads
├── marketing_site/         # Assets du site marketing
├── scripts/                # Scripts de migration DB
└── docs/                   # Documentation technique
```

## Blueprints Flask
- marketing_bp (/) : site marketing public, SEO
- main_bp (/) : dashboard, routes generales
- auth_bp (/auth) : login, 2FA, inscription, mot de passe
- client_bp (/clients) : gestion des clients
- receivable_bp (/receivables) : comptes recevables
- company_bp (/company) : parametres entreprise, connecteurs
- import_bp (/import) : historique des imports
- email_bp (/emails) : gabarits et envois de courriels
- note_bp (/notes) : notes et communications
- reminder_bp (/reminders) : rappels automatiques
- invoice_bp (/invoices) : gestion des factures
- campaign_bp (/campaigns) : campagnes d'envoi massif
- profile_bp (/profile) : profil utilisateur + OAuth Microsoft
- users_bp (/users) : gestion des utilisateurs
- admin_bp (/admin) : panneau super-admin
- onboarding_bp (/onboarding) : inscription et essai gratuit
- stripe_checkout_v2_bp (/stripe/v2/checkout) : paiement Stripe
- stripe_portal_bp (/stripe/v2) : portail client Stripe
- unified_webhook_bp : webhooks Stripe entrants
- oauth_callback_bp : callbacks OAuth (connecteurs comptables)
- notification_bp : notifications systeme
- jobs_bp (/jobs) : cron jobs
- backup_bp : backup base de donnees
- refresh_tokens_bp : refresh tokens email
- refresh_accounting_bp : refresh tokens comptables
- sync_emails_v3_bp : sync emails Outlook

## Routage par domaine
- finov-relance.com -> marketing_bp uniquement (middleware before_request dans app.py)
- app.finov-relance.com -> toutes les routes applicatives
- www.finov-relance.com -> redirect 301 vers finov-relance.com
- localhost -> pas de restriction (dev)
- test.finov-relance.com -> tout accessible (staging, MARKETING_URL non defini)
- Le routage est active par la variable MARKETING_URL

## Variables d'environnement
45+ variables configurees via Coolify (prod) ou .env (dev local).
Voir .env.example pour la liste complete.
Ne JAMAIS hardcoder de secret dans le code. Toujours utiliser os.environ.get().

## Conventions de code
- Python : PEP 8
- Langue du code : anglais (noms de variables, fonctions, classes)
- Langue du contenu utilisateur : francais quebecois
- Pas de cadratins dans le contenu genere, utiliser des virgules ou tirets simples
- Templates : Jinja2 dans le dossier templates/
- Assets statiques : dossier static/

## Commandes utiles
- Demarrer en local : `docker compose up --build`
- Demarrer sans Docker : `python main.py` (avec PostgreSQL local)
- Build Docker : `docker build -t finovrelance .`
- Tester le build : `docker build -t finovrelance-test .`
- Migration DB prod : `python scripts/migrate_db.py --source URL --target URL`

## Securite
- Verifier avant chaque commit : `grep -rn "sk_live_\|postgresql://.*neon" . --include="*.py"`
- Le .env ne doit JAMAIS etre commite
- Les tokens OAuth sont chiffres avec ENCRYPTION_MASTER_KEY (AES-256)
- Les sessions dependent de SESSION_SECRET
- CSP headers via security/csp_middleware.py
- Rate limiting via Flask-Limiter (5000/jour, 500/heure par defaut)
- CSRF protection via Flask-WTF (exemptions pour webhooks Stripe)

## Documentation
- docs/MIGRATION-FINOVRELANCE-VPS.md : plan complet de migration
- CONSIGNES-DEV-LOCAL.txt : setup dev local
- COOLIFY-CONFIG.md : configuration Coolify + migration DB
- docs/structure_technique.md : architecture detaillee

## TKO — Token Killer Optimized

TKO est installé globalement. Il compresse automatiquement les outputs
des commandes shell (git, cargo, docker, npm, grep, etc.) via un hook
PreToolUse. Aucune action requise pour les commandes Bash.

### Cache Read

TKO cache les lectures de fichiers. Quand tu relis un fichier inchangé,
tu reçois :

    [fichier inchangé — contenu identique à lecture #N]

Ce message signifie que le contenu est strictement identique à ta lecture #N.
Le cache se réinitialise automatiquement après 5 lectures consécutives.

### Compaction du contexte

Quand le contexte est compacté, TKO le détecte automatiquement et :
1. Purge tout le cache de lecture
2. Injecte ce message dans ton contexte :

       [TKO] Compaction detected — read cache has been reset.
       All file reads will return full content (no cache).
       Files that were cached : [liste]

Quand tu vois ce message, tous les fichiers que tu avais lus
précédemment ne sont plus en cache. Si tu as besoin du contenu
d'un fichier pour continuer ton travail, tu dois le relire —
la lecture retournera le contenu complet (pas de cache).

### Orientation projet

Si PROJECT_INDEX.md existe à la racine, le lire en début de session
avant toute exploration du codebase. Il contient la structure du projet,
les exports principaux, les routes API, les variables d'environnement
et les dépendances. Ne pas lancer grep pour s'orienter si l'index
est disponible.

Pour régénérer l'index : `tko index`

### Commandes TKO utiles

- `tko read --aggressive <fichier>` — affiche uniquement les signatures
  (fn, struct, class, interface, exports) sans les corps de fonctions.
  Utiliser quand tu as besoin de la structure d'un fichier sans le détail.

- `tko smart <fichier>` — résumé heuristique en 2 lignes : type de module,
  nombre de fonctions publiques, types définis, dépendances principales.
  Utiliser pour évaluer rapidement un fichier avant de le lire en entier.

- `tko gain` — affiche les statistiques de compression de la session.
