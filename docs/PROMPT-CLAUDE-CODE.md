# Prompt Claude Code - Migration FinovRelance vers VPS Coolify

## Contexte

Tu recois le projet FinovRelance telecharge depuis Replit (fichier zip). C'est une application SaaS Flask/Python de gestion de comptes recevables. L'IA de Replit a deja nettoye les secrets hardcodes et verifie le requirements.txt.

Ton role : transformer ce projet Replit en projet Docker pret a deployer sur un VPS via Coolify, le pousser sur un repo GitHub prive, et faire en sorte que Coolify puisse le deployer sans intervention manuelle.

Le document de reference complet est dans `MIGRATION-FINOVRELANCE-VPS.md` (fourni avec le projet). Lis-le en entier avant de commencer.

## Architecture cible

- Dev local : docker-compose avec PostgreSQL local
- Production : Coolify sur VPS, deploiement auto depuis la branche main
- Domaines : finov-relance.com (marketing), app.finov-relance.com (SaaS), mta-sts.finov-relance.com (securite email)
- Un seul container Flask sert les deux domaines principaux via des blueprints

## Etapes a executer dans l'ordre

### Phase 1 : Decompresser et analyser

1. Decompresse le zip dans un dossier de travail
2. Lis le rapport genere par Replit (si present) pour connaitre :
   - Le point d'entree (main.py, app.py, etc.)
   - Les dependances systeme (depuis replit.nix)
   - La logique de selection DATABASE_URL
   - La liste des cron jobs
   - Le statut Flask-Migrate
3. Fais ta propre verification :
```bash
# Point d'entree Flask
grep -rn "Flask(__name__)" . --include="*.py"

# Config Gunicorn existante
find . -name "gunicorn*" -type f

# Dependances systeme Replit
cat replit.nix 2>/dev/null

# Verification secrets residuels
grep -rn "sk_live_\|sk_test_\|whsec_\|postgresql://.*neon\|supabase.co" . --include="*.py" --include="*.html" --include="*.js"

# Structure du projet
find . -type f -name "*.py" | head -50
find . -type d | grep -v __pycache__ | grep -v .cache | grep -v .upm | grep -v node_modules | grep -v .git
```

### Phase 2 : Nettoyage des fichiers Replit

Supprime ces fichiers et dossiers specifiques a Replit :
```bash
rm -f .replit
rm -f replit.nix
rm -rf .upm/
rm -rf .cache/
rm -rf .config/
rm -rf .local/
rm -rf __pycache__/
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null
```

NE supprime PAS :
- Les fichiers de l'application (*.py, templates/, static/, etc.)
- Le dossier migrations/ (si Flask-Migrate est utilise)
- Les fichiers de donnees statiques (images, CSS, JS du frontend)
- Le requirements.txt

### Phase 3 : Creer le Dockerfile

Cree un `Dockerfile` a la racine. Adapte selon les dependances systeme trouvees dans replit.nix.

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependances systeme
# ADAPTE cette liste selon ce que replit.nix contenait :
# - libpq-dev : obligatoire pour psycopg2
# - gcc : obligatoire pour compiler certaines librairies Python
# - wkhtmltopdf : si le projet genere des PDF
# - libcairo2, libpango-1.0-0 : si weasyprint est utilise
# - libjpeg-dev, libpng-dev : si Pillow est utilise
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["./docker-entrypoint.sh"]
```

**Verification critique :** Apres avoir lu le replit.nix et le requirements.txt, ajuste la liste `apt-get install` pour inclure TOUTES les dependances systeme necessaires. Si une librairie Python echoue au `pip install` a cause d'une dependance systeme manquante, le build Docker echouera.

### Phase 4 : Creer le docker-entrypoint.sh

```bash
#!/bin/bash
set -e

echo "=== FinovRelance - Demarrage ==="

# Migrations automatiques si Flask-Migrate est utilise
if [ -d "migrations" ]; then
    echo "Application des migrations de base de donnees..."
    flask db upgrade
    echo "Migrations appliquees."
fi

# Demarrage Gunicorn
# ADAPTE le point d'entree selon le rapport Replit :
# - main:app si app est dans main.py
# - app:app si app est dans app.py
# - wsgi:app si un fichier wsgi.py existe
echo "Demarrage de Gunicorn..."
exec gunicorn --bind 0.0.0.0:5000 \
    --timeout 600 \
    --workers 2 \
    --threads 4 \
    --access-logfile - \
    --error-logfile - \
    main:app
```

Rends-le executable :
```bash
chmod +x docker-entrypoint.sh
```

### Phase 5 : Creer le .dockerignore

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
*.sql
*.dump
docker-compose.yml
docker-compose.override.yml
MIGRATION-FINOVRELANCE-VPS.md
PROMPT-REPLIT.md
PROMPT-CLAUDE-CODE.md
CONSIGNES-DEV-LOCAL.txt
COOLIFY-CONFIG.md
```

### Phase 6 : Creer le docker-compose.yml (dev local)

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
      - .:/app
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

### Phase 7 : Creer le .env.example

Cree un `.env.example` avec TOUTES les variables d'environnement du projet. Utilise des placeholders, aucune valeur reelle.

Scanne le code pour trouver toutes les variables lues :
```bash
grep -roh "os\.environ\.get(\s*['\"][^'\"]*['\"]" . --include="*.py" | sort -u
grep -roh "os\.environ\[['\"][^'\"]*['\"]\]" . --include="*.py" | sort -u
```

Le .env.example doit contenir au minimum les 21 secrets documentes, plus toute variable supplementaire trouvee dans le code. Organise par section avec des commentaires.

Inclure les variantes dev local en commentaire pour chaque variable qui change entre dev et prod (DATABASE_URL, MICROSOFT_REDIRECT_URI, FLASK_ENV, APP_URL, etc.).

### Phase 8 : Creer les scripts de migration DB

Cree le dossier `scripts/` avec deux fichiers :

**scripts/migrate_db.py** - Script Python de migration (voir MIGRATION-FINOVRELANCE-VPS.md section 4)

**scripts/deploy_first_time.sh** - Script bash pour la migration initiale sur le VPS (voir MIGRATION-FINOVRELANCE-VPS.md section 9)

Rends le script bash executable :
```bash
chmod +x scripts/deploy_first_time.sh
```

### Phase 9 : Creer les fichiers de documentation

**CONSIGNES-DEV-LOCAL.txt** - Guide complet pour setup dev local (voir MIGRATION-FINOVRELANCE-VPS.md section 8). Inclure :
- Les redirect URIs a ajouter dans Azure Portal, Xero, Stripe pour localhost
- Les commandes pour lancer en Docker et sans Docker
- La procedure d'import des donnees de prod
- Les notes de securite

**COOLIFY-CONFIG.md** - Aide-memoire pour la configuration Coolify (voir MIGRATION-FINOVRELANCE-VPS.md section 10). Inclure la procedure COMPLETE de migration DB (section Etape 4 du document).

### Phase 10 : Creer la config MTA-STS

Cree un dossier `mta-sts/` a la racine avec :

**mta-sts/Dockerfile** :
```dockerfile
FROM nginx:alpine
COPY mta-sts.txt /usr/share/nginx/html/.well-known/mta-sts.txt
```

**mta-sts/mta-sts.txt** :
```
version: STSv1
mode: enforce
mx: *.mail.protection.outlook.com
max_age: 604800
```

Si le rapport Replit indique que MTA-STS est deja gere par une route Flask dans l'app, note-le dans COOLIFY-CONFIG.md et ne cree pas le dossier mta-sts/ (la route Flask suffira).

### Phase 11 : Mettre a jour le .gitignore

Remplace ou cree le `.gitignore` :
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

# Replit
.replit
replit.nix
.upm/
.cache/
.config/
.local/

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db

# Securite - ne jamais commiter
*.sql
*.dump
*.bak

# Logs
*.log

# Docker
docker-compose.override.yml
```

### Phase 12 : Creer .github/dependabot.yml

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
```

### Phase 12b : Creer les fichiers de configuration Claude Code

Claude Code utilise des fichiers de configuration pour comprendre le projet a chaque session.
Cree les fichiers suivants a la racine du repo :

**CLAUDE.md** (racine du projet) :

Ce fichier est charge automatiquement par Claude Code au debut de chaque session.
Il doit contenir le contexte projet, les conventions, et les commandes utiles.

Genere un CLAUDE.md adapte au projet reel en analysant le code. Voici la structure a suivre :

```markdown
# FinovRelance

## Description
Application SaaS Flask/Python de gestion de comptes recevables.
Deux domaines servis par la meme app : finov-relance.com (marketing) et app.finov-relance.com (SaaS).

## Stack technique
- Backend : Flask, Python 3.11, Gunicorn
- Base de donnees : PostgreSQL 16 (Neon en legacy, VPS en production)
- ORM : [SQLAlchemy / autre - adapter selon le code]
- Migrations : [Flask-Migrate / Alembic / create_all - adapter]
- Paiement : Stripe (abonnements, webhooks)
- Email : Microsoft Graph API (OAuth), Gmail SMTP
- OAuth : Microsoft, Xero, Business Central
- Chiffrement : AES-256 (tokens OAuth stockes)
- Deploiement : Docker, Coolify, VPS

## Structure du projet
[Claude Code : liste ici les dossiers principaux trouves dans le projet.
Utilise la commande `find . -type d -maxdepth 2` pour generer la structure reelle.]

## Blueprints Flask
- marketing_bp (/) : site marketing public, SEO
- auth_bp (/auth) : login, 2FA, inscription, mot de passe
- client_bp (/clients) : gestion des clients
- receivable_bp (/receivables) : comptes recevables
- company_bp (/company) : parametres entreprise, connecteurs
- email_bp (/emails) : gabarits et envois de courriels
- campaign_bp (/campaigns) : campagnes d'envoi massif
- invoice_bp (/invoices) : gestion des factures
- reminder_bp (/reminders) : rappels automatiques
- jobs_bp (/jobs) : cron jobs
- admin_bp (/admin) : panneau super-admin
- stripe_checkout_v2_bp, stripe_portal_bp : paiement Stripe
[Completer avec la liste reelle trouvee dans le code]

## Routage par domaine
- finov-relance.com -> marketing_bp uniquement (middleware before_request)
- app.finov-relance.com -> toutes les routes applicatives
- localhost -> pas de restriction (dev)
- Le routage est active par la variable MARKETING_URL

## Variables d'environnement
21 secrets configures via Coolify (prod) ou .env (dev local).
Voir .env.example pour la liste complete.
Ne JAMAIS hardcoder de secret dans le code. Toujours utiliser os.environ.get().

## Conventions de code
- Python : PEP 8
- Langue du code : anglais (noms de variables, fonctions, classes)
- Langue du contenu utilisateur : francais quebecois
- Pas de cadratins (—) dans le contenu genere, utiliser des virgules ou tirets simples
- Templates : Jinja2 dans le dossier templates/
- Assets statiques : dossier static/

## Commandes utiles
- Demarrer en local : `docker compose up --build`
- Demarrer sans Docker : `python main.py` (avec PostgreSQL local)
- Lancer les migrations : `flask db upgrade` (si Flask-Migrate)
- Build Docker : `docker build -t finovrelance .`
- Tester le build : `docker build -t finovrelance-test .`
- Migration DB prod : `python scripts/migrate_db.py --source URL --target URL`

## Securite
- Verifier avant chaque commit : `grep -rn "sk_live_\|postgresql://.*neon" . --include="*.py"`
- Le .env ne doit JAMAIS etre commite
- Les tokens OAuth sont chiffres avec ENCRYPTION_MASTER_KEY (AES-256)
- Les sessions dependent de SESSION_SECRET

## Documentation
- MIGRATION-FINOVRELANCE-VPS.md : plan complet de migration
- CONSIGNES-DEV-LOCAL.txt : setup dev local
- COOLIFY-CONFIG.md : configuration Coolify + migration DB
```

Adapte le contenu en analysant le code reel du projet. Ne copie pas ce template tel quel.
Par exemple :
- Remplace [SQLAlchemy / autre] par l'ORM reel utilise
- Liste la vraie structure de dossiers
- Liste les vrais blueprints trouves dans le code
- Adapte les commandes selon le point d'entree reel

**.claude/settings.json** (settings partages avec l'equipe, commites dans le repo) :

```json
{
  "permissions": {
    "allow": [
      "Bash(docker compose:*)",
      "Bash(docker build:*)",
      "Bash(flask db:*)",
      "Bash(pip install:*)",
      "Bash(python scripts/*)",
      "Bash(grep:*)",
      "Bash(find:*)",
      "Bash(cat:*)",
      "Bash(ls:*)",
      "Bash(git:*)"
    ],
    "deny": [
      "Bash(rm -rf /)*",
      "Bash(curl:*:--upload-file)*"
    ]
  }
}
```

**.claude/commands/deploy-check.md** (commande custom /deploy-check) :

```markdown
Verifie que le projet est pret pour un deploiement :
1. Scanne le code pour des secrets hardcodes (sk_live_, postgresql://, etc.)
2. Verifie que .env n'est pas dans le staging git
3. Verifie que requirements.txt est a jour vs les imports
4. Verifie que le build Docker passe
5. Genere un rapport OK / PROBLEMES TROUVES
```

**.claude/commands/db-check.md** (commande custom /db-check) :

```markdown
Verifie l'etat de la base de donnees :
1. Lis DATABASE_URL depuis les variables d'environnement
2. Connecte-toi et compte le nombre de tables dans le schema public
3. Compte le nombre d'utilisateurs dans la table "user"
4. Verifie que les tables critiques existent : user, client, invoice, campaign, company
5. Affiche un rapport avec les compteurs
```

**.claude/commands/security-scan.md** (commande custom /security-scan) :

```markdown
Scan de securite complet du projet :
1. Cherche des secrets hardcodes dans le code Python, HTML, JS
2. Cherche des URLs de base de donnees en dur
3. Verifie que tous les os.environ.get() ont des noms de variables valides
4. Verifie que .gitignore bloque .env, *.sql, *.dump
5. Verifie que docker-compose.yml n'expose pas de ports sensibles
6. Genere un rapport avec les problemes trouves et les corrections suggerees
```

Mettre a jour le `.gitignore` pour inclure les fichiers Claude Code locaux :
```gitignore
# Claude Code (fichiers locaux, ne pas commiter)
.claude/settings.local.json
```

Note : `.claude/settings.json` et `.claude/commands/` SONT commites (partages avec l'equipe).
`.claude/settings.local.json` n'est PAS commite (preferences personnelles).

### Phase 13 : Implementer le routage par domaine

C'est une piece critique. En production, un seul container Flask sert deux domaines differents :
- `finov-relance.com` -> site marketing uniquement
- `app.finov-relance.com` -> application SaaS

Le routage doit etre gere dans le code Flask via le header Host de la requete.

Verifie d'abord si un mecanisme de routage par domaine existe deja :
```bash
grep -rn "request.host\|request.url_root\|SERVER_NAME\|url_for.*_external" . --include="*.py"
```

Si aucun mecanisme n'existe, implementer un middleware `@app.before_request` qui :

1. Lit `request.host` (sans le port)
2. Compare avec les variables MARKETING_URL et APP_URL (en retirant le schema https://)
3. Si le host correspond au domaine marketing (`finov-relance.com`) :
   - Autorise les routes du `marketing_bp` : /, /fonctionnalites, /tarifs, /cas-usage, /contact, /guide, /guide/<slug>, /sitemap.xml, /robots.txt
   - Autorise /static/ (assets CSS/JS/images)
   - Redirige toute autre route vers `https://app.finov-relance.com` + le meme path (301)
4. Si le host correspond au domaine app (`app.finov-relance.com`) :
   - Autorise toutes les routes applicatives
   - La racine / peut afficher le login ou rediriger vers /auth/login selon le comportement actuel
5. En dev local (localhost, 127.0.0.1) : aucune restriction, tout fonctionne comme avant

Exemple d'implementation :

```python
import os
from flask import request, redirect
from urllib.parse import urlparse

def extract_host(url):
    """Extrait le hostname d'une URL, sans schema ni port."""
    if not url:
        return None
    parsed = urlparse(url if '://' in url else f'https://{url}')
    return parsed.hostname

MARKETING_HOST = extract_host(os.environ.get("MARKETING_URL", ""))
APP_HOST = extract_host(os.environ.get("APP_URL", ""))

# Routes autorisees sur le domaine marketing
MARKETING_PREFIXES = ('/', '/fonctionnalites', '/tarifs', '/cas-usage', '/contact',
                      '/guide', '/sitemap.xml', '/robots.txt', '/static/')

@app.before_request
def route_by_domain():
    # Pas de routage en dev local
    host = request.host.split(':')[0]
    if host in ('localhost', '127.0.0.1') or not MARKETING_HOST:
        return

    # Domaine marketing : restreindre aux routes publiques
    if host == MARKETING_HOST:
        path = request.path
        is_marketing_route = any(
            path == prefix or path.startswith(prefix.rstrip('/') + '/')
            for prefix in MARKETING_PREFIXES
        )
        if not is_marketing_route:
            return redirect(f"https://{APP_HOST}{path}", code=301)
```

Points importants :
- Ce middleware doit etre enregistre APRES l'initialisation de l'app Flask et des blueprints
- Il ne bloque rien en dev local (tout fonctionne sur localhost comme avant)
- Il ne s'active que quand MARKETING_URL est defini (donc pas en staging sur test.finov-relance.com)
- Pendant la phase de test (test.finov-relance.com), MARKETING_URL n'est pas defini -> pas de routage -> tout est accessible comme sur Replit
- En production, MARKETING_URL=https://finov-relance.com active le routage

Placer ce code dans le fichier approprié (la ou l'app Flask est initialisee, probablement main.py ou app.py).

### Phase 14 : Verification de securite finale

Avant le push, execute ces verifications :

```bash
# Aucun secret dans le code
grep -rn "sk_live_\|sk_test_\|whsec_\|pk_live_" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "postgresql://.*:.*@.*neon\|postgresql://.*:.*@.*supabase" . --include="*.py" --exclude-dir=scripts
grep -rn "ENCRYPTION_MASTER_KEY\s*=\s*['\"]" . --include="*.py" | grep -v "os.environ"
grep -rn "SESSION_SECRET\s*=\s*['\"]" . --include="*.py" | grep -v "os.environ"

# Pas de .env dans le staging
test ! -f .env || echo "ALERTE : fichier .env present, ne pas commiter"

# Pas de dump SQL
find . -name "*.sql" -o -name "*.dump" | grep -v node_modules
```

Si un de ces tests echoue, corrige avant de continuer.

### Phase 15 : Test du build Docker local

Avant de push, verifie que le build passe :
```bash
docker build -t finovrelance-test .
```

Si le build echoue :
- Dependance systeme manquante : ajouter dans le Dockerfile (apt-get install)
- Package Python echoue : verifier requirements.txt
- Fichier manquant dans le COPY : verifier .dockerignore

Le container n'a pas besoin de demarrer completement (il n'aura pas de DB), mais le build doit reussir.

### Phase 16 : Initialiser le repo Git et push

```bash
# Initialiser Git (si pas deja fait)
git init
git branch -M main

# Ajouter le remote (repo prive, deja cree sur GitHub)
git remote add origin git@github.com:VOTRE-ORG/finovrelance.git

# Verifier que .gitignore fait son travail
git status
# Confirmer qu'aucun .env, .sql, .dump, ou secret n'apparait

# Premier commit
git add .
git commit -m "Migration FinovRelance : Replit -> Docker/Coolify

- Ajout Dockerfile et docker-entrypoint.sh
- Ajout routage par domaine (middleware before_request)
- Ajout docker-compose.yml pour dev local
- Ajout .env.example avec 21+ variables
- Ajout scripts de migration DB (migrate_db.py, deploy_first_time.sh)
- Ajout CONSIGNES-DEV-LOCAL.txt et COOLIFY-CONFIG.md
- Ajout MTA-STS (mta-sts/)
- Ajout .github/dependabot.yml
- Nettoyage fichiers Replit (.replit, replit.nix, .upm, etc.)
- Verification securite : aucun secret dans le code"

# Push
git push -u origin main
```

### Phase 17 : Rapport final

A la fin, genere ce rapport :

```
=== RAPPORT CLAUDE CODE - MIGRATION FINOVRELANCE ===

1. NETTOYAGE REPLIT
   Fichiers supprimes : [liste]

2. DOCKERFILE
   Image de base : python:3.11-slim
   Dependances systeme : [liste]
   Point d'entree : [main:app / app:app / ...]

3. FICHIERS CREES
   [x] Dockerfile
   [x] docker-entrypoint.sh
   [x] .dockerignore
   [x] docker-compose.yml
   [x] .env.example (X variables)
   [x] .gitignore
   [x] scripts/migrate_db.py
   [x] scripts/deploy_first_time.sh
   [x] CONSIGNES-DEV-LOCAL.txt
   [x] COOLIFY-CONFIG.md
   [x] mta-sts/Dockerfile
   [x] mta-sts/mta-sts.txt
   [x] .github/dependabot.yml
   [x] CLAUDE.md
   [x] .claude/settings.json
   [x] .claude/commands/deploy-check.md
   [x] .claude/commands/db-check.md
   [x] .claude/commands/security-scan.md

4. VERIFICATION SECURITE
   Secrets hardcodes trouves : [0 / liste]
   .env dans le repo : [non]
   Dumps SQL dans le repo : [non]

5. ROUTAGE PAR DOMAINE
   Middleware implemente : [oui/non]
   Fichier : [main.py / app.py / ...]
   Comportement :
     finov-relance.com -> routes marketing seulement
     app.finov-relance.com -> routes applicatives
     localhost -> tout accessible (dev)
     test.finov-relance.com -> tout accessible (staging, MARKETING_URL non defini)
   Routes marketing autorisees : [liste]

6. BUILD DOCKER
   Status : [succes / echec + details]

7. GIT PUSH
   Repo : [URL]
   Branche : main
   Commit : [hash]

8. POINTS D'ATTENTION POUR COOLIFY
   [tout ce que le developpeur doit savoir pour configurer Coolify]
   [variables critiques, domaines, redirect URIs, etc.]

9. PROCHAINES ETAPES
   1. Connecter le repo dans Coolify
   2. Configurer les variables d'environnement (voir COOLIFY-CONFIG.md)
   3. Configurer le domaine test.finov-relance.com (staging)
   4. Deployer et tester
   5. Executer la migration DB (scripts/deploy_first_time.sh)
   6. Valider toutes les fonctionnalites
   7. Dans Coolify : remplacer test.finov-relance.com par finov-relance.com + app.finov-relance.com
   8. Ajouter MARKETING_URL=https://finov-relance.com dans les variables
   9. Redeployer (le routage par domaine s'active)
   10. Basculer le DNS dans Cloudflare
   11. Mettre a jour les redirect URIs OAuth et webhooks Stripe

=== FIN DU RAPPORT ===
```

## Regles absolues

1. **Aucun secret dans le code.** Si tu trouves un secret que Replit a manque, remplace-le par os.environ.get() et signale-le dans le rapport.
2. **Le build Docker doit passer.** Ne push pas si le build echoue.
3. **Le repo doit etre prive.** Verifie avant de push.
4. **Aucun .env, .sql, .dump dans le commit.** Verifie avec git status avant le git add.
5. **Toutes les dependances doivent etre presentes.** Si une librairie est importee dans le code mais absente de requirements.txt, ajoute-la.
6. **Le docker-entrypoint.sh doit utiliser le bon point d'entree.** Verifie dans le code, pas dans la doc.
7. **Garde le MIGRATION-FINOVRELANCE-VPS.md dans le repo.** C'est la reference pour la suite de la migration.
