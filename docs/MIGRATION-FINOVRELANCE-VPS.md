# Migration FinovRelance - De Replit vers VPS Coolify

## Contexte

FinovRelance est une application SaaS Flask/Python de gestion de comptes recevables, actuellement hebergee sur Replit (autoscale). L'objectif est de migrer l'ensemble du projet vers un VPS auto-heberge avec Coolify comme interface de gestion.

**La migration doit etre invisible pour les utilisateurs et admins.** Aucun temps d'arret, aucune perte de donnees, aucune action requise de leur part.

---

## Domaines actuels (Replit) et cibles (VPS)

### Domaines en place (a migrer)

| Domaine | Role actuel | Contenu |
|---|---|---|
| `workspace--tdubus.repl.co` | Dev Replit | Environnement de dev, jamais expose aux clients |
| `finov-relance.com` | Site marketing public | Accueil, tarifs, fonctionnalites, contact, guides, pages legales, SEO (sitemap, robots.txt). `www.finov-relance.com` redirige en 301. |
| `app.finov-relance.com` | Application SaaS | Dashboard, clients, campagnes, comptes recevables - la ou les clients se connectent |
| `mta-sts.finov-relance.com` | Securite email | Sert le fichier MTA-STS (RFC 8461) a `/.well-known/mta-sts.txt`. Force le TLS pour les courriels entrants vers `@finov-relance.com`. |

### Cibles de deploiement

| Environnement | URL | Usage |
|---|---|---|
| **Dev local** | `http://localhost:5000` | Developpement, tests, debug |
| **Staging VPS** | `https://test.finov-relance.com` (app) | Validation pre-production sur le VPS |
| **Production VPS - Marketing** | `https://finov-relance.com` | Site marketing public |
| **Production VPS - App** | `https://app.finov-relance.com` | Application SaaS clients |
| **Production VPS - MTA-STS** | `https://mta-sts.finov-relance.com` | Politique MTA-STS (securite email) |

**Important :** Le site marketing (`finov-relance.com`) et l'application (`app.finov-relance.com`) sont servis par la meme application Flask via des blueprints differents (`marketing_bp` pour le site public, les autres pour l'app). C'est un seul container Docker qui repond aux deux domaines.

Le sous-domaine `mta-sts.finov-relance.com` sert uniquement un fichier texte statique. Il peut etre gere par Coolify comme une ressource statique separee, ou par un mini-container Nginx dedie (voir section MTA-STS plus bas).

### Routage par domaine (critique)

Un seul container Flask sert deux domaines avec des contenus differents.
Le routage doit etre parfait :

- Requete vers `finov-relance.com` -> routes marketing uniquement (accueil, tarifs, fonctionnalites, contact, guide, SEO)
- Requete vers `app.finov-relance.com` -> routes applicatives (dashboard, clients, factures, campagnes, auth, etc.)
- Requete vers `www.finov-relance.com` -> redirect 301 vers `finov-relance.com`

Ce routage doit etre gere dans le code Flask via le header `Host` de la requete HTTP.
Claude Code doit implementer ou verifier un middleware/decorator qui :

1. Lit `request.host` sur chaque requete
2. Si le host est `finov-relance.com` (ou la valeur de MARKETING_URL) :
   - Autorise uniquement les routes du `marketing_bp`
   - Redirige vers `app.finov-relance.com` si un utilisateur tente d'acceder a /auth, /clients, /dashboard, etc.
3. Si le host est `app.finov-relance.com` (ou la valeur de APP_URL) :
   - Autorise toutes les routes applicatives
   - Redirige vers `finov-relance.com` si quelqu'un tape app.finov-relance.com/ (racine) sans etre connecte, OU affiche directement le login
4. En dev local (localhost:5000), tout fonctionne sans restriction de domaine

Approche recommandee - middleware Flask :

```python
from flask import request, redirect
import os

MARKETING_HOST = os.environ.get("MARKETING_URL", "http://localhost:5000").replace("https://", "").replace("http://", "")
APP_HOST = os.environ.get("APP_URL", "http://localhost:5000").replace("https://", "").replace("http://", "")

MARKETING_PATHS = ['/', '/fonctionnalites', '/tarifs', '/cas-usage', '/contact', '/guide', '/sitemap.xml', '/robots.txt']

@app.before_request
def route_by_domain():
    host = request.host.split(':')[0]  # Retirer le port si present

    # Dev local : pas de routage par domaine
    if host in ('localhost', '127.0.0.1'):
        return

    # Domaine marketing : bloquer les routes applicatives
    if host == MARKETING_HOST:
        path = request.path
        # Autoriser les routes marketing et les assets statiques
        if not any(path == p or path.startswith(p + '/') for p in MARKETING_PATHS) \
           and not path.startswith('/static/') \
           and not path.startswith('/guide/'):
            return redirect(f"https://{APP_HOST}{path}", code=301)

    # Domaine app : optionnel, rediriger la racine vers le login ou le dashboard
    # if host == APP_HOST and request.path == '/':
    #     return redirect('/auth/login')
```

Ce code est un point de depart. Claude Code doit l'adapter selon la structure reelle des blueprints
et le comportement actuel de l'app sur Replit (ou le marketing et l'app cohabitent deja sur le meme domaine).

### Transition test.finov-relance.com -> production

Pendant la phase de test, un seul domaine (`test.finov-relance.com`) sert tout le contenu
(marketing + app) comme c'est le cas actuellement sur Replit. Pas de routage par domaine a ce stade.

Quand la validation est terminee, la bascule vers la production se fait dans cet ordre :

1. Dans Coolify, REMPLACER le domaine `test.finov-relance.com` par les deux domaines de production :
   `finov-relance.com` et `app.finov-relance.com`
   (Coolify supporte plusieurs domaines sur la meme application)

2. Mettre a jour les variables d'environnement dans Coolify :
   APP_URL=https://app.finov-relance.com
   MARKETING_URL=https://finov-relance.com

3. Redeployer (le middleware de routage s'active grace aux variables)

4. Basculer le DNS dans Cloudflare

Impact du remplacement de domaine dans Coolify :
- Coolify regenere les certificats SSL via Let's Encrypt pour les nouveaux domaines
- Traefik (le reverse proxy de Coolify) met a jour sa configuration de routage
- Le container Docker lui-meme ne change pas, seul le reverse proxy redirige le trafic
- Il peut y avoir 1-2 minutes de delai pour la generation des certificats SSL
- Pendant ce delai, les visiteurs verront une erreur SSL (c'est pourquoi on bascule le DNS APRES le deploiement, pas avant)

### Workflow de deploiement

1. Dev local sur la machine du developpeur
2. Push sur GitHub (branche `main`)
3. Coolify detecte le push et deploie automatiquement sur le VPS

### Redirect WWW

La redirection `www.finov-relance.com` -> `finov-relance.com` (301) doit etre maintenue. Configurer dans Coolify ou dans Nginx/Caddy au niveau du reverse proxy.

---

## Architecture cible

```
GitHub (repo prive)
    |
    v
Coolify (sur VPS)
    |
    v
Docker (build depuis Dockerfile)
    |
    +---> App Flask (Gunicorn, port 5000)
    |       +---> finov-relance.com (marketing_bp)
    |       +---> app.finov-relance.com (tous les autres blueprints)
    |
    +---> PostgreSQL 16 (gere par Coolify ou externe Neon selon phase)
    |
    +---> MTA-STS (mini-container ou fichier statique)
              +---> mta-sts.finov-relance.com/.well-known/mta-sts.txt
```

---

## Ce que tu dois produire (checklist pour l'IA Replit)

### 1. Dockerfile

Creer un `Dockerfile` a la racine du projet avec ces specifications :

```dockerfile
# Image de base Python
FROM python:3.11-slim

# Variables d'environnement pour Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Repertoire de travail
WORKDIR /app

# Installer les dependances systeme necessaires
# Inclure : gcc, libpq-dev (pour psycopg2), curl, et tout ce que le projet utilise
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copier et installer les dependances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code de l'application
COPY . .

# Exposer le port
EXPOSE 5000

# Commande de demarrage
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "600", "--workers", "2", "--threads", "4", "main:app"]
```

**Adapter selon les besoins reels du projet :**
- Si le projet utilise `weasyprint`, `wkhtmltopdf`, `pillow` ou d'autres librairies avec des dependances systeme, les ajouter dans le `RUN apt-get install`
- Si le projet a un fichier `gunicorn.conf.py`, l'utiliser dans le CMD
- Verifier le point d'entree exact (`main:app`, `app:app`, `wsgi:app`, etc.) et ajuster
- Si le projet utilise des workers async (gevent, eventlet), les inclure dans requirements.txt et adapter le CMD

### 2. .dockerignore

Creer un `.dockerignore` a la racine :

```
.git
.gitignore
.env
.env.*
*.pyc
__pycache__
.replit
replit.nix
.upm
.cache
.config
.local
node_modules
.pytest_cache
*.db
*.sqlite3
venv/
.venv/
*.log
.DS_Store
Thumbs.db
README.md
docs/
tests/
*.md
!requirements.txt
```

### 3. Fichier .env.example

Creer un `.env.example` a la racine avec TOUTES les variables d'environnement necessaires, mais avec des placeholders. Aucune valeur reelle ne doit apparaitre.

```env
# === Base de donnees ===
DATABASE_URL=postgresql://user:password@host:5432/finovrelance
# En dev local, utiliser : postgresql://user:password@localhost:5432/finovrelance

# === Session Flask ===
SESSION_SECRET=GENERER_UNE_CLE_RANDOM_64_CHARS
FLASK_ENV=production
# En dev local, mettre : development

# === OAuth Microsoft ===
MICROSOFT_CLIENT_ID=votre-client-id
MICROSOFT_CLIENT_SECRET=votre-client-secret
MICROSOFT_REDIRECT_URI=https://app.finov-relance.com/profile/microsoft/callback
# En dev local : http://localhost:5000/profile/microsoft/callback
MICROSOFT_TENANT=votre-tenant-id

# === Stripe ===
STRIPE_SECRET_KEY=sk_live_xxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
# En dev local, utiliser les cles sk_test_ et pk_test_

# === Email systeme ===
MAIL_PASSWORD=votre-mot-de-passe-email

# === Business Central ===
BUSINESS_CENTRAL_CLIENT_ID=votre-client-id
BUSINESS_CENTRAL_CLIENT_SECRET=votre-client-secret

# === Xero ===
XERO_CLIENT_ID=votre-client-id
XERO_CLIENT_SECRET=votre-client-secret

# === Chiffrement ===
ENCRYPTION_MASTER_KEY=GENERER_UNE_CLE_AES256_BASE64

# === Cron et Backup ===
REPL_CRON_SECRET=GENERER_UN_TOKEN_RANDOM
BACKUP_SECRET_TOKEN=GENERER_UN_TOKEN_RANDOM

# === Supabase (backup) ===
SUPABASE_USER=votre-user
SUPABASE_PASSWORD=votre-password
SUPABASE_DATABASE_URL=postgresql://user:password@host:5432/postgres

# === Neon (reference directe si besoin) ===
NEON_DATABASE_URL=postgresql://user:password@host:5432/finovrelance

# === App config ===
APP_URL=https://app.finov-relance.com
MARKETING_URL=https://finov-relance.com
# En dev local, les deux pointent vers localhost :
# APP_URL=http://localhost:5000
# MARKETING_URL=http://localhost:5000

# === Domaines autorises (pour validation CORS/host si applicable) ===
ALLOWED_HOSTS=finov-relance.com,app.finov-relance.com,www.finov-relance.com
# En dev local : localhost,127.0.0.1
```

### 4. Script de migration de base de donnees : migrate_db.py

Creer un script `scripts/migrate_db.py` qui :

1. Se connecte a la base de production Neon (source)
2. Fait un dump complet (schema + donnees) avec `pg_dump`
3. Restaure dans la base cible (PostgreSQL sur le VPS ou local)

```python
#!/usr/bin/env python3
"""
Script de migration de la base de donnees FinovRelance.
Copie integrale de la base de production Neon vers la cible.

Usage :
  python scripts/migrate_db.py --source DATABASE_URL_SOURCE --target DATABASE_URL_TARGET

Prerequis :
  - pg_dump et psql installes localement
  - Acces reseau aux deux bases de donnees
"""

import subprocess
import sys
import argparse
import os
from datetime import datetime


def run_command(cmd, description, env=None):
    """Execute une commande shell et affiche le resultat."""
    print(f"\n{'='*60}")
    print(f"  {description}")
    print(f"{'='*60}")
    
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, env=merged_env
    )
    
    if result.returncode != 0:
        print(f"ERREUR : {result.stderr}")
        sys.exit(1)
    
    if result.stdout:
        print(result.stdout[:500])  # Tronquer pour lisibilite
    
    print("OK")
    return result


def main():
    parser = argparse.ArgumentParser(description="Migration DB FinovRelance")
    parser.add_argument("--source", required=True, help="URL de la base source (Neon prod)")
    parser.add_argument("--target", required=True, help="URL de la base cible (VPS ou local)")
    parser.add_argument("--dump-only", action="store_true", help="Seulement faire le dump, pas la restauration")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_file = f"finovrelance_dump_{timestamp}.sql"

    # Etape 1 : Dump de la base source
    run_command(
        f'pg_dump "{args.source}" --no-owner --no-acl --clean --if-exists -f {dump_file}',
        "Dump de la base de production (Neon)..."
    )

    print(f"\nDump sauvegarde dans : {dump_file}")
    
    if args.dump_only:
        print("Mode dump-only : arret ici.")
        return

    # Etape 2 : Restauration dans la base cible
    run_command(
        f'psql "{args.target}" -f {dump_file}',
        "Restauration dans la base cible..."
    )

    # Etape 3 : Verification
    run_command(
        f'psql "{args.target}" -c "SELECT COUNT(*) as nb_tables FROM information_schema.tables WHERE table_schema = \'public\';"',
        "Verification : nombre de tables dans la cible..."
    )

    run_command(
        f'psql "{args.target}" -c "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = \'public\' ORDER BY tablename;"',
        "Liste des tables migrees..."
    )

    print(f"\n{'='*60}")
    print("  MIGRATION TERMINEE AVEC SUCCES")
    print(f"{'='*60}")
    print(f"Dump conserve dans : {dump_file}")
    print("Supprime-le apres validation pour des raisons de securite.")


if __name__ == "__main__":
    main()
```

### 5. Script Docker entrypoint : docker-entrypoint.sh

Creer un `docker-entrypoint.sh` a la racine. Ce script s'execute au demarrage du container et gere les migrations automatiques.

```bash
#!/bin/bash
set -e

echo "=== FinovRelance - Demarrage ==="

# Si le projet utilise Flask-Migrate / Alembic, lancer les migrations
if [ -d "migrations" ]; then
    echo "Application des migrations de base de donnees..."
    flask db upgrade
    echo "Migrations appliquees."
fi

# Demarrer Gunicorn
echo "Demarrage de Gunicorn..."
exec gunicorn --bind 0.0.0.0:5000 \
    --timeout 600 \
    --workers 2 \
    --threads 4 \
    --access-logfile - \
    --error-logfile - \
    main:app
```

Rendre le fichier executable :
```bash
chmod +x docker-entrypoint.sh
```

Mettre a jour le Dockerfile pour utiliser ce script :
```dockerfile
# Remplacer la ligne CMD par :
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["./docker-entrypoint.sh"]
```

### 6. docker-compose.yml (pour dev local)

Creer un `docker-compose.yml` pour le dev local :

```yaml
version: "3.8"

services:
  app:
    build: .
    ports:
      - "5000:5000"
    env_file:
      - .env
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - .:/app  # Hot reload en dev
    environment:
      - FLASK_ENV=development

  db:
    image: postgres:16
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: finovrelance
      POSTGRES_PASSWORD: localdevpassword
      POSTGRES_DB: finovrelance
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U finovrelance"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  pgdata:
```

### 7. .gitignore

Mettre a jour le `.gitignore` pour inclure au minimum :

```gitignore
# Environnement
.env
.env.*
!.env.example

# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
venv/
.venv/

# Replit (ne plus utilise)
.replit
replit.nix
.upm/
.cache/
.config/

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db

# Dumps DB (securite)
*.sql
*.dump

# Logs
*.log

# Docker
docker-compose.override.yml
```

### 8. Fichier CONSIGNES-DEV-LOCAL.txt

Creer un fichier `CONSIGNES-DEV-LOCAL.txt` a la racine :

```
============================================================
  CONSIGNES DE CONFIGURATION - DEVELOPPEMENT LOCAL
  FinovRelance
============================================================

PREREQUIS :
- Docker Desktop installe et en marche
- Python 3.11+ (si tu veux tester hors Docker)
- PostgreSQL 16 client (psql, pg_dump) installe
- Git configure avec acces au repo GitHub prive

------------------------------------------------------------
1. CLONER LE REPO
------------------------------------------------------------
git clone git@github.com:VOTRE-ORG/finovrelance.git
cd finovrelance

------------------------------------------------------------
2. CONFIGURER LES VARIABLES D'ENVIRONNEMENT
------------------------------------------------------------
cp .env.example .env

Ouvre le fichier .env et remplis TOUTES les valeurs.

Pour le dev local, ajuste ces valeurs specifiquement :
- DATABASE_URL=postgresql://finovrelance:localdevpassword@localhost:5432/finovrelance
- FLASK_ENV=development
- APP_URL=http://localhost:5000

------------------------------------------------------------
3. CONFIGURER OAUTH MICROSOFT (OBLIGATOIRE)
------------------------------------------------------------
Va dans Azure Portal > App registrations > ton app FinovRelance.

Dans "Authentication" > "Redirect URIs", AJOUTE cette URL :
  http://localhost:5000/profile/microsoft/callback

Ne supprime PAS les URLs de production, ajoute celle-ci en plus.

Dans ton .env local, mets :
  MICROSOFT_REDIRECT_URI=http://localhost:5000/profile/microsoft/callback

------------------------------------------------------------
4. CONFIGURER STRIPE (OPTIONNEL POUR DEV)
------------------------------------------------------------
Va dans le dashboard Stripe > mode Test.

Utilise les cles de test (commencent par sk_test_ et pk_test_).

Pour les webhooks en local, installe le Stripe CLI :
  stripe login
  stripe listen --forward-to localhost:5000/stripe/v2/checkout/webhook

Le CLI va te donner un webhook secret (whsec_...).
Mets-le dans ton .env local comme STRIPE_WEBHOOK_SECRET.

------------------------------------------------------------
5. CONFIGURER XERO / BUSINESS CENTRAL (OPTIONNEL)
------------------------------------------------------------
Meme principe que Microsoft OAuth.
Ajoute les redirect URIs localhost dans chaque portail developeur.

Xero : https://developer.xero.com > tes apps > redirect URIs
  Ajouter : http://localhost:5000/profile/xero/callback

Business Central : Azure Portal > l'app Business Central
  Ajouter : http://localhost:5000/profile/business-central/callback

(Verifie les paths exacts dans le code, blueprints oauth_callback_bp)

------------------------------------------------------------
6. LANCER L'APPLICATION
------------------------------------------------------------
Option A - Avec Docker (recommande) :
  docker compose up --build

  L'app sera disponible sur http://localhost:5000

Option B - Sans Docker :
  pip install -r requirements.txt
  # Assure-toi que PostgreSQL tourne en local
  python main.py

------------------------------------------------------------
7. IMPORTER LES DONNEES DE PRODUCTION
------------------------------------------------------------
Si tu as besoin des donnees de prod pour tester :

  python scripts/migrate_db.py \
    --source "postgresql://USER:PASS@NEON_HOST:5432/finovrelance" \
    --target "postgresql://finovrelance:localdevpassword@localhost:5432/finovrelance"

IMPORTANT : Ne commite JAMAIS un dump SQL dans le repo.
Supprime le fichier .sql apres utilisation.

------------------------------------------------------------
8. CRON JOBS
------------------------------------------------------------
En local, les cron jobs ne tournent pas automatiquement.
Pour tester un cron job manuellement :

  curl -H "Authorization: Bearer TON_REPL_CRON_SECRET" http://localhost:5000/jobs/NOM_DU_JOB

------------------------------------------------------------
NOTES DE SECURITE
------------------------------------------------------------
- Le fichier .env ne doit JAMAIS etre commite (verifie .gitignore)
- Les dumps SQL (.sql) ne doivent JAMAIS etre commites
- Ne mets jamais de secrets dans le code source
- En cas de doute, fais : git diff --cached (avant un commit)
  pour verifier qu'aucun secret n'est inclus

============================================================
```

### 9. Script de deploiement initial : scripts/deploy_first_time.sh

Ce script est destine a etre execute UNE SEULE FOIS lors de la migration initiale sur le VPS. Il migre la base de donnees de production.

```bash
#!/bin/bash
set -e

# =============================================================
#  FinovRelance - Deploiement initial (migration depuis Neon)
#  A executer UNE SEULE FOIS sur le VPS apres le premier deploy
# =============================================================

echo ""
echo "============================================"
echo "  FinovRelance - Migration initiale"
echo "============================================"
echo ""

# Verifications
if [ -z "$DATABASE_URL_SOURCE" ]; then
    echo "ERREUR : Variable DATABASE_URL_SOURCE non definie."
    echo "C'est l'URL de ta base Neon de production."
    echo ""
    echo "Usage :"
    echo "  export DATABASE_URL_SOURCE='postgresql://user:pass@neon-host:5432/finovrelance'"
    echo "  export DATABASE_URL_TARGET='postgresql://user:pass@localhost:5432/finovrelance'"
    echo "  bash scripts/deploy_first_time.sh"
    exit 1
fi

if [ -z "$DATABASE_URL_TARGET" ]; then
    echo "ERREUR : Variable DATABASE_URL_TARGET non definie."
    echo "C'est l'URL de ta nouvelle base PostgreSQL sur le VPS."
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="/tmp/finovrelance_migration_${TIMESTAMP}.sql"

echo "1/4 - Dump de la base de production Neon..."
pg_dump "$DATABASE_URL_SOURCE" \
    --no-owner \
    --no-acl \
    --clean \
    --if-exists \
    -f "$DUMP_FILE"
echo "     Dump OK : $DUMP_FILE"

echo ""
echo "2/4 - Restauration dans la base cible..."
psql "$DATABASE_URL_TARGET" -f "$DUMP_FILE"
echo "     Restauration OK"

echo ""
echo "3/4 - Verification..."
TABLE_COUNT=$(psql "$DATABASE_URL_TARGET" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';")
echo "     Tables dans la cible : $TABLE_COUNT"

USER_COUNT=$(psql "$DATABASE_URL_TARGET" -t -c "SELECT COUNT(*) FROM \"user\";" 2>/dev/null || echo "N/A")
echo "     Utilisateurs migres : $USER_COUNT"

echo ""
echo "4/4 - Nettoyage..."
rm -f "$DUMP_FILE"
echo "     Dump supprime."

echo ""
echo "============================================"
echo "  MIGRATION TERMINEE"
echo "============================================"
echo ""
echo "Prochaines etapes :"
echo "  1. Teste l'application sur https://test.finov-relance.com"
echo "  2. Verifie que les users peuvent se connecter"
echo "  3. Verifie les connecteurs OAuth (Microsoft, Xero, BC)"
echo "  4. Quand tout est valide, bascule le DNS de app.finov-relance.com"
echo ""
```

### 10. Configuration Coolify : coolify.json (optionnel, aide-memoire)

Coolify n'utilise pas de fichier de config dans le repo. La configuration se fait dans l'interface web. Creer un fichier `COOLIFY-CONFIG.md` comme aide-memoire :

```markdown
# Configuration Coolify - FinovRelance

## Etape 1 : Creer une nouvelle application

- Source : GitHub (repo prive)
- Branche : main
- Build Pack : Dockerfile
- Dockerfile location : /Dockerfile
- Port : 5000

## Etape 2 : Variables d'environnement

Dans Coolify > Application > Environment Variables, ajouter TOUTES les variables
listees dans .env.example avec les valeurs de PRODUCTION.

Variables critiques a ne pas oublier :
- DATABASE_URL (pointer vers la DB PostgreSQL du VPS)
- SESSION_SECRET
- ENCRYPTION_MASTER_KEY
- Toutes les cles OAuth (Microsoft, Stripe, Xero, BC)
- APP_URL=https://test.finov-relance.com (staging) ou https://app.finov-relance.com (prod)
- MICROSOFT_REDIRECT_URI=https://test.finov-relance.com/profile/microsoft/callback

## Etape 3 : Domaines

L'application Flask repond aux deux domaines (marketing + app) depuis le meme container.
Dans Coolify, ajouter les deux domaines dans la configuration de l'application :

Phase staging :
  - test.finov-relance.com

Phase production (apres validation) :
  - finov-relance.com (site marketing)
  - app.finov-relance.com (application SaaS)

Pour chaque domaine :
  - SSL : Activer Let's Encrypt
  - Force HTTPS : Oui

Redirect WWW :
  Configurer une redirection 301 de www.finov-relance.com vers finov-relance.com.
  Option 1 : Dans Coolify, ajouter www.finov-relance.com comme domaine alias avec redirect.
  Option 2 : Dans Cloudflare, creer une Page Rule : www.finov-relance.com/* -> 301 -> https://finov-relance.com/$1

## Etape 3b : MTA-STS (securite email)

Le sous-domaine mta-sts.finov-relance.com doit servir un fichier statique a :
  https://mta-sts.finov-relance.com/.well-known/mta-sts.txt

Contenu du fichier mta-sts.txt (adapter selon ta politique actuelle) :
  version: STSv1
  mode: enforce
  mx: *.mail.protection.outlook.com
  max_age: 604800

Options de deploiement dans Coolify :

  Option A - Container Nginx statique dedie (recommande) :
    Creer un nouveau service dans Coolify de type "Static Site" ou Docker.
    Utiliser un mini Dockerfile :

    FROM nginx:alpine
    COPY mta-sts.txt /usr/share/nginx/html/.well-known/mta-sts.txt

    Domaine : mta-sts.finov-relance.com
    SSL : Let's Encrypt

  Option B - Route Flask dans l'app principale :
    Ajouter une route dans le marketing_bp ou un blueprint dedie :

    @app.route('/.well-known/mta-sts.txt')
    def mta_sts():
        content = """version: STSv1\nmode: enforce\nmx: *.mail.protection.outlook.com\nmax_age: 604800"""
        return content, 200, {'Content-Type': 'text/plain'}

    Puis ajouter mta-sts.finov-relance.com comme domaine supplementaire dans Coolify.

  Option C - Cloudflare Worker (si tu utilises deja Cloudflare) :
    Pas besoin de toucher au VPS. Creer un Worker sur le sous-domaine mta-sts
    qui retourne le contenu du fichier.

Creer un dossier `mta-sts/` a la racine du repo avec :
  - Dockerfile (option A)
  - mta-sts.txt

## Etape 4 : Base de donnees - Migration complete avec donnees de production

C'est l'etape la plus critique. La base de donnees doit contenir TOUTES les donnees
de production (users, clients, factures, campagnes, tokens OAuth chiffres, abonnements Stripe, etc.)
pour que la migration soit invisible.

### 4a. Creer la base PostgreSQL dans Coolify

1. Dans Coolify, aller dans Resources > New > Database > PostgreSQL
2. Version : 16
3. Laisser Coolify generer les identifiants OU definir manuellement :
   - Database name : finovrelance
   - Username : finovrelance_app  (PAS postgres, PAS superuser)
   - Password : generer un mot de passe fort (32+ caracteres)
4. Configuration reseau :
   - La base doit etre accessible UNIQUEMENT en interne (pas d'exposition publique)
   - Coolify cree un reseau Docker interne entre les services
5. Noter l'URL interne generee par Coolify. Elle ressemble a :
   postgresql://finovrelance_app:MOT_DE_PASSE@NOM_SERVICE_COOLIFY:5432/finovrelance

### 4b. Ouvrir un acces temporaire pour la migration

Pour importer les donnees depuis Neon, il faut temporairement pouvoir acceder
a la base du VPS depuis l'exterieur (ou depuis le VPS lui-meme).

Option A - Depuis le VPS directement (recommande, plus securitaire) :
  Se connecter en SSH au VPS et executer les commandes depuis la.
  Avantage : pas besoin d'exposer le port PostgreSQL.

Option B - Exposer temporairement le port :
  Dans Coolify > la ressource PostgreSQL > Settings > Ports
  Mapper temporairement le port 5432 vers un port externe (ex: 54321).
  IMPORTANT : retirer ce mapping apres la migration.

### 4c. Procedure de migration pas-a-pas

Prerequis : avoir pg_dump et psql installes sur la machine qui execute la migration.
Sur le VPS, ils sont probablement deja installes. Sinon :
  sudo apt-get install -y postgresql-client-16

Variables a preparer :
  SOURCE = URL de la base Neon de production (celle dans DATABASE_URL actuel sur Replit)
  TARGET = URL de la nouvelle base PostgreSQL sur le VPS (celle de l'etape 4a)

--- Etape 1 : Arreter l'application Replit ---

Avant de faire le dump, arreter l'app sur Replit pour eviter des ecritures
pendant la migration. Ca garantit que le dump est un snapshot coherent.

Dans Replit : Deployments > Stop (ou mettre en pause l'autoscale).

--- Etape 2 : Dump de la base source (Neon) ---

Se connecter en SSH au VPS, puis :

  export SOURCE="postgresql://USER:PASS@NEON_HOST:5432/finovrelance?sslmode=require"
  
  pg_dump "$SOURCE" \
    --no-owner \
    --no-acl \
    --clean \
    --if-exists \
    --format=plain \
    -f /tmp/finovrelance_prod.sql

Explications des flags :
  --no-owner     : ne pas inclure les commandes de changement de proprietaire
                   (le user sera different sur le VPS)
  --no-acl       : ne pas inclure les permissions specifiques a Neon
  --clean        : ajouter des DROP TABLE avant chaque CREATE TABLE
                   (pour pouvoir relancer le script si besoin)
  --if-exists    : eviter les erreurs si les tables n'existent pas encore
  --format=plain : fichier SQL lisible (permet de verifier le contenu)

Verifier la taille du dump :
  ls -lh /tmp/finovrelance_prod.sql

--- Etape 3 : Verifier le dump (optionnel mais recommande) ---

Ouvrir le fichier et verifier qu'il contient bien les tables attendues :
  head -100 /tmp/finovrelance_prod.sql
  grep "CREATE TABLE" /tmp/finovrelance_prod.sql | wc -l

Comparer avec le nombre de tables sur Neon :
  psql "$SOURCE" -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"

Les deux chiffres doivent correspondre.

--- Etape 4 : Restaurer dans la base cible (VPS) ---

  export TARGET="postgresql://finovrelance_app:MOT_DE_PASSE@localhost:5432/finovrelance"
  
  # Si la base Coolify utilise un reseau Docker interne, utiliser le nom du service :
  # export TARGET="postgresql://finovrelance_app:MOT_DE_PASSE@NOM_SERVICE:5432/finovrelance"
  # OU se connecter via le port expose temporairement :
  # export TARGET="postgresql://finovrelance_app:MOT_DE_PASSE@localhost:54321/finovrelance"

  psql "$TARGET" -f /tmp/finovrelance_prod.sql

Si des erreurs apparaissent :
  - "role does not exist" : normal avec --no-owner, ca n'affecte pas les donnees
  - "table does not exist" pour un DROP : normal avec --if-exists sur une base vide
  - Erreurs de type "already exists" : la base n'etait pas vide, relancer avec --clean
  - Erreurs de permission : verifier que le user a les droits CREATE/INSERT

--- Etape 5 : Verifier la migration ---

  # Nombre de tables
  psql "$TARGET" -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"

  # Liste des tables
  psql "$TARGET" -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"

  # Nombre d'utilisateurs
  psql "$TARGET" -c "SELECT COUNT(*) FROM \"user\";"

  # Nombre de factures (adapter le nom de la table)
  psql "$TARGET" -c "SELECT COUNT(*) FROM invoice;" 2>/dev/null || echo "Table invoice non trouvee, verifier le nom exact"

  # Nombre de clients
  psql "$TARGET" -c "SELECT COUNT(*) FROM client;" 2>/dev/null || echo "Table client non trouvee, verifier le nom exact"

  # Verifier qu'un user admin existe
  psql "$TARGET" -c "SELECT id, email, is_admin FROM \"user\" WHERE is_admin = true LIMIT 5;"

Comparer chaque chiffre avec la base source. Ils doivent etre identiques.

--- Etape 6 : Nettoyer ---

  rm -f /tmp/finovrelance_prod.sql

  Si le port PostgreSQL a ete expose temporairement (Option B de l'etape 4b) :
    Retourner dans Coolify > PostgreSQL > Settings > retirer le mapping de port externe.

--- Etape 7 : Configurer l'application pour utiliser la nouvelle base ---

  Dans Coolify > Application FinovRelance > Environment Variables :
    DATABASE_URL = l'URL interne de la base PostgreSQL du VPS (etape 4a)

  NE PAS mettre DATABASE_URL_PROD. Une seule variable DATABASE_URL suffit.
  Coolify injecte la bonne valeur selon l'environnement.

  Redeployer l'application.

--- Etape 8 : Test de validation ---

  1. Ouvrir https://test.finov-relance.com/auth/login
  2. Se connecter avec un compte admin existant
  3. Verifier que le dashboard affiche les bonnes donnees (clients, factures, etc.)
  4. Verifier qu'un utilisateur standard peut aussi se connecter
  5. Creer une note ou un rappel de test pour valider les ecritures

  Si tout fonctionne : la migration DB est terminee.

### 4d. Points de vigilance specifiques a FinovRelance

Tokens OAuth chiffres :
  Les tokens Microsoft, Xero et Business Central sont chiffres en AES-256
  avec ENCRYPTION_MASTER_KEY. La meme cle doit etre configuree dans Coolify.
  Si la cle change, tous les tokens deviennent illisibles et les utilisateurs
  devront reconnecter leurs comptes. Utiliser la MEME cle que sur Replit.

Sessions utilisateurs :
  Les sessions Flask sont liees au SESSION_SECRET. Si le secret change,
  tous les utilisateurs seront deconnectes a la migration. C'est acceptable
  (ils se reconnectent une fois), mais si tu veux eviter ca, utilise le meme
  SESSION_SECRET que sur Replit.

Abonnements Stripe :
  Les abonnements Stripe vivent chez Stripe, pas dans la base locale.
  La migration ne les affecte pas. Par contre, les webhooks Stripe
  doivent pointer vers la nouvelle URL. Mettre a jour dans le dashboard Stripe :
    Developers > Webhooks > modifier l'endpoint URL vers :
    https://app.finov-relance.com/stripe/v2/checkout/webhook
  (ou le path exact du unified_webhook_bp)

Backup Supabase :
  Apres la migration, verifier que le backup automatique vers Supabase
  fonctionne toujours. Les secrets SUPABASE_* doivent etre configures dans Coolify.

### 4e. Procedure de rollback (si ca tourne mal)

Si la migration echoue ou si des problemes sont detectes apres la bascule :

  1. Dans Coolify, changer DATABASE_URL pour pointer de nouveau vers Neon
  2. Redeployer l'application
  3. L'app utilise de nouveau la base Neon comme avant
  4. Les donnees ecrites sur le VPS pendant le test seront perdues
     (c'est pourquoi on teste sur test.finov-relance.com d'abord)

  Si la bascule DNS vers app.finov-relance.com est deja faite :
  1. Repointer le DNS vers Replit
  2. Relancer l'app sur Replit
  3. Les utilisateurs retrouvent l'application comme avant

  Garder Neon actif pendant au moins 2 semaines apres la migration reussie,
  comme filet de securite. Ne supprimer le projet Neon qu'apres validation complete.

## Etape 5 : Health Check

- Path : /auth/login (ou une route qui retourne 200)
- Interval : 30s

## Etape 6 : Deploy

1. Verifier que le build Docker passe
2. Verifier que l'app repond sur test.finov-relance.com
3. Executer la migration DB (scripts/deploy_first_time.sh)
4. Tester les fonctionnalites critiques

## DNS (Cloudflare)

Phase staging - Ajouter dans Cloudflare :
  test      A ou CNAME   IP-DU-VPS

Phase production - Ajouter/modifier dans Cloudflare :
  @         A            IP-DU-VPS          (finov-relance.com - site marketing)
  www       CNAME        finov-relance.com   (redirect 301 via Page Rule ou Coolify)
  app       CNAME        finov-relance.com   (app.finov-relance.com - application)
  mta-sts   CNAME        finov-relance.com   (mta-sts.finov-relance.com - si servi par le VPS)

Ne pas oublier les enregistrements MX, SPF, DKIM, DMARC et le TXT _mta-sts existants.
Ces enregistrements DNS email ne changent PAS lors de la migration (ils pointent vers Microsoft, pas vers Replit).

Proxy Cloudflare : desactiver le proxy orange si Coolify gere le SSL.
Sinon, activer le proxy et configurer SSL mode Full (Strict).
```

### 11. Fichier .github/dependabot.yml

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
```

### 12. Nettoyage des fichiers Replit

**Fait par Claude Code apres le telechargement, PAS par Replit.**
Voir PROMPT-CLAUDE-CODE.md, Phase 2.

Fichiers a supprimer : .replit, replit.nix, .upm/, .cache/, .config/, .local/

### 13. Verification de securite pre-push

Avant de pousser le code sur GitHub, verifier qu'AUCUN secret n'est dans le code :

```bash
# Rechercher des patterns de secrets dans le code
grep -rn "sk_live" . --include="*.py" --include="*.js" --include="*.html"
grep -rn "sk_test" . --include="*.py" --include="*.js" --include="*.html"
grep -rn "whsec_" . --include="*.py" --include="*.js" --include="*.html"
grep -rn "postgresql://" . --include="*.py" --exclude-dir=scripts
grep -rn "ENCRYPTION_MASTER_KEY\s*=" . --include="*.py" | grep -v "os.environ"
grep -rn "SESSION_SECRET\s*=" . --include="*.py" | grep -v "os.environ"
```

Si un de ces greps retourne un resultat suspect (une vraie valeur hardcodee), corriger avant de push.

Verifier aussi que le code utilise partout `os.environ.get()` ou `os.environ[]` pour lire les secrets, et jamais des valeurs en dur.

---

## Ordre d'execution pour la migration

### Phase 1 : Preparation sur Replit (PROMPT-REPLIT.md)

Executee par l'IA de Replit :
1. Verifier et completer requirements.txt
2. Scanner et corriger les secrets hardcodes
3. Verifier que toutes les variables passent par os.environ
4. Identifier le point d'entree, les dependances systeme, les cron jobs
5. Generer le rapport de preparation
6. Telecharger le projet en ZIP

### Phase 2 : Transformation par Claude Code (PROMPT-CLAUDE-CODE.md)

Executee par Claude Code sur le projet telecharge :
1. Decompresser et analyser le rapport Replit
2. Supprimer les fichiers Replit (.replit, replit.nix, .upm, etc.)
3. Creer Dockerfile, docker-entrypoint.sh, .dockerignore
4. Creer docker-compose.yml (dev local)
5. Creer .env.example avec toutes les variables
6. Creer les scripts de migration DB
7. Creer les fichiers de documentation (CONSIGNES-DEV-LOCAL.txt, COOLIFY-CONFIG.md)
8. Creer la config MTA-STS
9. Mettre a jour .gitignore
10. Creer .github/dependabot.yml
11. Tester le build Docker
12. Push sur GitHub (repo prive, branche main)

### Phase 3 : Configuration Coolify (manuelle)

Executee par Tony dans l'interface Coolify :
1. Connecter le repo GitHub dans Coolify
2. Creer l'application avec le Dockerfile
3. Configurer TOUTES les variables d'environnement
4. Configurer le domaine `test.finov-relance.com`
5. Deployer

### Phase 4 : Migration de la base de donnees (voir COOLIFY-CONFIG.md, Etape 4)

1. Creer la base PostgreSQL 16 sur le VPS via Coolify
2. Arreter l'app sur Replit
3. Depuis le VPS en SSH, faire le dump de Neon et restaurer dans la nouvelle base
4. Mettre a jour DATABASE_URL dans Coolify pour pointer vers la DB locale du VPS
5. Redeployer l'application
6. Verifier les donnees (users, clients, factures)

### Phase 5 : Validation

1. Tester la connexion utilisateur sur test.finov-relance.com
2. Tester l'envoi de courriel (OAuth Microsoft)
3. Tester les connecteurs comptables (Xero, Business Central)
4. Tester Stripe (mode test d'abord)
5. Tester les cron jobs
6. Tester le backup Supabase

### Phase 6 : Bascule vers production (migration finale)

Quand tout est valide sur test.finov-relance.com :

1. Dans Coolify, REMPLACER le domaine de l'application :
   - Retirer : test.finov-relance.com
   - Ajouter : finov-relance.com
   - Ajouter : app.finov-relance.com
   - SSL : Let's Encrypt sur les deux domaines
   - Attendre que les certificats soient generes (1-2 min)

2. Mettre a jour les variables d'environnement dans Coolify :
   - APP_URL=https://app.finov-relance.com
   - MARKETING_URL=https://finov-relance.com
   - MICROSOFT_REDIRECT_URI=https://app.finov-relance.com/profile/microsoft/callback
   - ALLOWED_HOSTS=finov-relance.com,app.finov-relance.com,www.finov-relance.com

3. Redeployer l'application (le middleware de routage par domaine s'active)

4. ENSUITE SEULEMENT, basculer le DNS dans Cloudflare :
   - `finov-relance.com` (A record) -> IP du VPS
   - `app.finov-relance.com` (CNAME) -> finov-relance.com ou IP du VPS
   - `www.finov-relance.com` (CNAME) -> finov-relance.com (+ Page Rule redirect 301)
   - `mta-sts.finov-relance.com` -> IP du VPS (si servi par le VPS)
   - NE PAS toucher aux enregistrements MX, SPF, DKIM, DMARC, _mta-sts TXT

   L'ordre est important : Coolify doit etre pret AVANT que le DNS bascule,
   sinon les visiteurs arrivent sur le VPS mais Coolify n'a pas les certificats
   et le routage n'est pas configure.

5. Mettre a jour les redirect URIs dans les portails tiers :
   - Azure Portal (Microsoft OAuth) : https://app.finov-relance.com/profile/microsoft/callback
   - Xero Developer : https://app.finov-relance.com/profile/xero/callback
   - Business Central : adapter selon le path
   - Stripe Dashboard > Developers > Webhooks : https://app.finov-relance.com/stripe/v2/checkout/webhook

6. Tests de validation post-bascule :
   - https://finov-relance.com charge le site marketing (accueil, tarifs, fonctionnalites)
   - https://finov-relance.com/auth/login redirige vers https://app.finov-relance.com/auth/login
   - https://www.finov-relance.com redirige en 301 vers https://finov-relance.com
   - https://app.finov-relance.com charge le login
   - https://app.finov-relance.com/ (racine) affiche le login ou redirige vers le dashboard
   - https://mta-sts.finov-relance.com/.well-known/mta-sts.txt retourne la politique MTA-STS
   - Un utilisateur existant peut se connecter via app.finov-relance.com
   - Les courriels sortants fonctionnent
   - Le sitemap.xml et robots.txt repondent sur finov-relance.com
   - Un visiteur sur finov-relance.com ne voit PAS de contenu applicatif (dashboard, etc.)
   - Un visiteur sur app.finov-relance.com ne voit PAS le site marketing

7. Garder test.finov-relance.com dans le DNS pendant 1 semaine comme fallback.
   Si un probleme survient, remettre test.finov-relance.com dans Coolify
   et repointer le DNS vers Replit.

---

## Points d'attention critiques

### Variables d'environnement a adapter entre Replit et Docker

Dans Replit, les secrets sont injectes automatiquement. Dans Docker/Coolify, ils sont geres par l'interface Coolify. Le code Python doit lire les variables via `os.environ.get()` ou `os.environ[]`.

**Verifier que le code ne fait PAS :**
```python
# MAUVAIS - specifique a Replit
import replit
db_url = replit.db["DATABASE_URL"]

# MAUVAIS - valeur en dur
db_url = "postgresql://user:pass@neon-host:5432/db"
```

**Le code doit faire :**
```python
# BON - standard, fonctionne partout
import os
db_url = os.environ.get("DATABASE_URL")
```

### Selection de la base de donnees (dev vs prod)

Le code actuel utilise `DATABASE_URL_PROD` en production et `DATABASE_URL` en dev. Dans la nouvelle architecture, il n'y a qu'une seule variable `DATABASE_URL` injectee par Coolify selon l'environnement. Simplifier la logique si possible.

Si le code fait :
```python
db_url = os.environ.get("DATABASE_URL_PROD") or os.environ.get("DATABASE_URL")
```

Ca reste compatible. Mais dans Coolify, on injectera seulement `DATABASE_URL` avec la bonne valeur.

### OAuth Redirect URIs

C'est le point le plus critique de la migration. Les redirect URIs doivent correspondre EXACTEMENT a ce que l'app recoit.

Pour le staging, mettre a jour dans :
- Azure Portal (Microsoft) : `https://test.finov-relance.com/profile/microsoft/callback`
- Xero Developer : `https://test.finov-relance.com/profile/xero/callback`
- Business Central : adapter selon le path du blueprint

Pour la production finale :
- Remettre `https://app.finov-relance.com/...` partout

### Cron Jobs

Sur Replit, les crons sont appeles par le service cron de Replit via des endpoints HTTP avec un token. Sur le VPS, il faut reconfigurer :

Option A - Cron interne au container (recommande pour commencer) :
  Ajouter un `cron` dans le Dockerfile ou un service sidecar.

Option B - Coolify scheduled tasks (si supporte).

Option C - Cron sur le VPS host qui appelle les endpoints HTTP de l'app.

Le plus simple pour la migration : garder les memes endpoints HTTP et les appeler via `curl` depuis un cron sur le VPS host :

```cron
# /etc/crontab ou crontab de l'utilisateur sur le VPS
*/15 * * * * curl -s -H "Authorization: Bearer TOKEN" https://test.finov-relance.com/jobs/sync > /dev/null 2>&1
0 3 * * * curl -s -H "Authorization: Bearer TOKEN" https://test.finov-relance.com/backup/run > /dev/null 2>&1
```

Adapter les paths et frequences selon les jobs existants dans le code.

### Gunicorn - Point d'entree

Verifier le fichier exact qui contient l'objet Flask `app`. C'est generalement `main.py`, `app.py`, ou `wsgi.py`. Le CMD du Dockerfile doit correspondre :

- Si `app` est dans `main.py` : `main:app`
- Si `app` est dans `app.py` : `app:app`
- Si un fichier `wsgi.py` existe : `wsgi:app`

### Workers Gunicorn

Sur un VPS 4 Go RAM, 2 workers avec 4 threads est un bon point de depart. Si le VPS a plus de RAM, la formule classique est `(2 x nb_cpu) + 1` workers.

---

## Resume des fichiers a creer/modifier

| Fichier | Action | Description |
|---|---|---|
| `Dockerfile` | CREER | Build Docker de l'app |
| `.dockerignore` | CREER | Exclure les fichiers inutiles du build |
| `docker-entrypoint.sh` | CREER | Script de demarrage du container |
| `docker-compose.yml` | CREER | Dev local avec PostgreSQL |
| `.env.example` | CREER | Template des variables d'environnement |
| `.gitignore` | MODIFIER | Ajouter les exclusions Docker et securite |
| `scripts/migrate_db.py` | CREER | Script Python de migration DB |
| `scripts/deploy_first_time.sh` | CREER | Script bash de migration initiale VPS |
| `CONSIGNES-DEV-LOCAL.txt` | CREER | Guide de setup dev local |
| `COOLIFY-CONFIG.md` | CREER | Aide-memoire configuration Coolify |
| `.github/dependabot.yml` | CREER | Mises a jour de securite automatiques |
| `CLAUDE.md` | CREER | Configuration Claude Code - contexte projet |
| `.claude/settings.json` | CREER | Permissions Claude Code (commite, partage equipe) |
| `.claude/commands/deploy-check.md` | CREER | Commande /deploy-check |
| `.claude/commands/db-check.md` | CREER | Commande /db-check |
| `.claude/commands/security-scan.md` | CREER | Commande /security-scan |
| `mta-sts/Dockerfile` | CREER | Mini-container pour mta-sts.finov-relance.com |
| `mta-sts/mta-sts.txt` | CREER | Fichier politique MTA-STS (RFC 8461) |
| `.replit` | SUPPRIMER | Plus necessaire |
| `replit.nix` | SUPPRIMER | Plus necessaire |

---

## Checklist de validation finale

Avant de considerer la migration comme terminee :

- [ ] L'app build en Docker sans erreur
- [ ] L'app demarre et repond sur le port 5000
- [ ] La base de donnees est migree avec toutes les tables et donnees
- [ ] Les utilisateurs existants peuvent se connecter
- [ ] L'envoi de courriels fonctionne (OAuth Microsoft Graph)
- [ ] Les connecteurs comptables fonctionnent (Xero, Business Central)
- [ ] Stripe fonctionne (paiements, webhooks, portail)
- [ ] Les cron jobs tournent (sync, backup, refresh tokens)
- [ ] Le backup vers Supabase fonctionne
- [ ] HTTPS actif avec certificat valide sur tous les domaines
- [ ] finov-relance.com sert le site marketing correctement
- [ ] finov-relance.com/auth/login redirige vers app.finov-relance.com/auth/login
- [ ] www.finov-relance.com redirige en 301 vers finov-relance.com
- [ ] app.finov-relance.com sert l'application correctement
- [ ] Le routage par domaine fonctionne (marketing vs app separes)
- [ ] mta-sts.finov-relance.com/.well-known/mta-sts.txt repond correctement
- [ ] sitemap.xml et robots.txt accessibles sur finov-relance.com
- [ ] Aucun secret dans le code source
- [ ] Le repo GitHub est prive
- [ ] Le .env n'est pas dans le repo
- [ ] Les dumps SQL ne sont pas dans le repo
- [ ] Coolify deploie automatiquement au push sur main
- [ ] Les enregistrements DNS email (MX, SPF, DKIM, DMARC) n'ont pas ete modifies
- [ ] CLAUDE.md et .claude/ sont presents dans le repo
