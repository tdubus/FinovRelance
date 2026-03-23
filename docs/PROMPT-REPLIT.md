# Prompt Replit - Preparation du projet FinovRelance pour export

## Contexte

Je migre FinovRelance de Replit vers un VPS auto-heberge (Coolify + Docker). Un collegue (Claude Code) va recevoir le projet telecharge, le nettoyer, le dockeriser et le pousser sur GitHub.

Ton role a toi, c'est de preparer le projet pour que le telechargement soit COMPLET et PROPRE. Rien ne doit manquer. Le projet doit pouvoir fonctionner tel quel une fois telecharge.

## Ce que tu dois faire

### 1. Verifier que requirements.txt est complet et a jour

Genere un `requirements.txt` qui contient TOUTES les dependances du projet, avec les versions pinnnees.

Methode :
```bash
pip freeze > requirements.txt
```

Verifie que le fichier contient au minimum ces librairies (adapte selon ce qui est installe) :
- flask
- gunicorn
- psycopg2-binary (ou psycopg2)
- sqlalchemy (si utilise)
- flask-migrate (si utilise)
- alembic (si utilise)
- stripe
- msal (Microsoft Auth Library) ou requests-oauthlib
- cryptography (pour AES-256, ENCRYPTION_MASTER_KEY)
- xero-python (si utilise)
- apscheduler (si utilise pour les cron jobs)
- Toute autre librairie importee dans le code

Fais un scan rapide :
```bash
grep -rh "^import \|^from " *.py **/*.py | sort -u
```
Compare avec requirements.txt. Si un import n'a pas de librairie correspondante dans requirements.txt, ajoute-la.

### 2. Verifier qu'AUCUN secret n'est hardcode dans le code source

Scanne tout le projet :
```bash
grep -rn "sk_live_" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "sk_test_" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "whsec_" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "pk_live_" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "postgresql://" . --include="*.py" --include="*.html" --include="*.js" --include="*.json"
grep -rn "neon.tech" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "supabase.co" . --include="*.py" --include="*.html" --include="*.js"
grep -rn "ENCRYPTION_MASTER_KEY.*=.*['\"]" . --include="*.py" | grep -v "os.environ"
grep -rn "SESSION_SECRET.*=.*['\"]" . --include="*.py" | grep -v "os.environ"
grep -rn "CLIENT_SECRET.*=.*['\"]" . --include="*.py" | grep -v "os.environ"
```

Si un grep retourne une valeur reelle (pas un placeholder, pas un os.environ.get) :
- Remplace la valeur hardcodee par un `os.environ.get("NOM_DE_LA_VARIABLE")`
- Assure-toi que la variable est dans les Secrets Replit

Fais-moi un rapport de chaque secret trouve et corrige.

### 3. Verifier que TOUTES les variables d'environnement passent par os.environ

Le projet utilise 21 secrets (voir liste ci-dessous). Chacun doit etre lu via `os.environ.get()` ou `os.environ[]` dans le code Python. Aucune valeur par defaut sensible.

Liste des 21 secrets :
1. DATABASE_URL
2. SESSION_SECRET
3. MICROSOFT_CLIENT_ID
4. MICROSOFT_CLIENT_SECRET
5. MICROSOFT_REDIRECT_URI
6. MICROSOFT_TENANT
7. STRIPE_SECRET_KEY
8. STRIPE_PUBLISHABLE_KEY
9. STRIPE_WEBHOOK_SECRET
10. MAIL_PASSWORD
11. BUSINESS_CENTRAL_CLIENT_ID
12. BUSINESS_CENTRAL_CLIENT_SECRET
13. ENCRYPTION_MASTER_KEY
14. REPL_CRON_SECRET
15. BACKUP_SECRET_TOKEN
16. SUPABASE_USER
17. SUPABASE_PASSWORD
18. SUPABASE_DATABASE_URL
19. XERO_CLIENT_ID
20. XERO_CLIENT_SECRET
21. NEON_DATABASE_URL

Pour chacun, verifie qu'il est lu correctement dans le code :
```bash
grep -rn "NOM_DU_SECRET" . --include="*.py" | head -5
```

Si un secret est lu d'une maniere specifique a Replit (ex: `import replit`, `replit.db`, ou un mecanisme custom), remplace par `os.environ.get("NOM")`.

### 4. Verifier la variable DATABASE_URL_PROD

Le code utilise possiblement cette logique pour choisir la base de donnees :
```python
db_url = os.environ.get("DATABASE_URL_PROD") or os.environ.get("DATABASE_URL")
```

C'est compatible avec la migration. Ne change pas cette logique, mais confirme-moi exactement comment la selection de la DB est faite dans le code (quel fichier, quelle ligne).

### 5. Identifier le point d'entree de l'application

Dis-moi :
- Quel fichier contient `app = Flask(__name__)` ? (generalement main.py, app.py, ou __init__.py)
- Quel fichier est lance par Gunicorn ? (regarde dans .replit ou dans la config Gunicorn)
- Si un fichier `gunicorn.conf.py` ou `gunicorn_config.py` existe, montre-moi son contenu.

### 6. Identifier les dependances systeme

Regarde dans `replit.nix` et liste toutes les dependances systeme qui y sont declarees. Par exemple :
- postgresql, libpq
- wkhtmltopdf, weasyprint
- pillow, libjpeg, libpng
- cairo, pango (si weasyprint)
- Tout autre package systeme

Ces dependances devront etre installees dans le Dockerfile. Donne-moi la liste complete.

### 7. Identifier les fichiers statiques et templates

Confirme la structure des dossiers suivants :
```bash
ls -la static/
ls -la templates/
ls -la uploads/ 2>/dev/null
ls -la media/ 2>/dev/null
```

S'il y a un dossier `uploads/` ou `media/` avec des fichiers utilisateur (logos, documents importes, etc.), signale-le. Ces fichiers devront etre migres separement.

### 8. Lister les cron jobs

Montre-moi tous les endpoints de type cron/job dans le projet :
```bash
grep -rn "cron\|job\|schedule\|periodic" . --include="*.py" -l
```

Et donne-moi :
- Le contenu du blueprint `jobs_bp`
- Le contenu du blueprint `backup_bp`
- Le contenu du blueprint `refresh_tokens_bp`
- Le contenu du blueprint `refresh_accounting_bp`
- Le contenu du blueprint `sync_emails_v3_bp`

Pour chaque job, je veux savoir :
- L'URL de l'endpoint
- La methode HTTP (GET/POST)
- La frequence d'execution actuelle sur Replit
- Le header d'authentification requis

### 9. Verifier Flask-Migrate / Alembic

Le projet utilise-t-il Flask-Migrate ou Alembic pour les migrations de schema ?
```bash
ls -la migrations/ 2>/dev/null
grep -rn "flask.migrate\|flask_migrate\|alembic" . --include="*.py"
```

Si oui, montre-moi le contenu du dossier `migrations/`. Ce dossier doit etre inclus dans le telechargement.

Si non, comment le schema est-il gere ? (SQLAlchemy create_all, scripts SQL manuels, autre ?)

### 10. Generer un rapport de structure

```bash
find . -type f -name "*.py" | head -50
find . -type f -name "*.html" | head -30
find . -type d | grep -v __pycache__ | grep -v .cache | grep -v .upm | grep -v node_modules | grep -v .git
```

### 11. Verifier le fichier MTA-STS

Le fichier de politique MTA-STS est-il servi par l'application Flask actuellement ?
```bash
grep -rn "mta.sts\|mta-sts\|well-known" . --include="*.py"
```

Si oui, montre-moi la route et le contenu du fichier servi.
Si non, c'est gere ailleurs (Cloudflare, autre) et ca ne concerne pas le telechargement.

### 12. NE PAS FAIRE

- NE supprime PAS les fichiers .replit, replit.nix, .upm, etc. Ca sera fait par Claude Code apres le telechargement.
- NE cree PAS de Dockerfile, docker-compose, .dockerignore. Ca sera fait par Claude Code.
- NE modifie PAS la structure du projet. On veut le projet tel quel, juste avec les secrets nettoyes.
- NE touche PAS aux Secrets Replit eux-memes.

### 13. Rapport final

A la fin, donne-moi un rapport structure avec :

```
=== RAPPORT DE PREPARATION - FINOVRELANCE ===

1. POINT D'ENTREE
   Fichier : [main.py / app.py / ...]
   Objet Flask : [app / create_app() / ...]
   Gunicorn config : [oui/non, contenu si oui]

2. REQUIREMENTS.TXT
   Nombre de packages : [X]
   Packages manquants ajoutes : [liste]
   Status : [OK / corrections faites]

3. SECRETS HARDCODES
   Trouves : [X]
   Corriges : [liste des fichiers et lignes modifies]
   Status : [OK / aucun trouve]

4. VARIABLES D'ENVIRONNEMENT
   Methode de lecture : [os.environ.get / os.environ / autre]
   Specifique a Replit : [oui/non, details]
   Logique DATABASE_URL : [description exacte]

5. DEPENDANCES SYSTEME (depuis replit.nix)
   [liste]

6. FICHIERS STATIQUES
   static/ : [oui/non, taille]
   templates/ : [oui/non, nombre]
   uploads/media/ : [oui/non, taille, contenu]

7. CRON JOBS
   [pour chaque job : URL, methode, frequence, auth]

8. MIGRATIONS DB
   Flask-Migrate : [oui/non]
   Dossier migrations/ : [oui/non]
   Methode de schema : [description]

9. MTA-STS
   Gere par Flask : [oui/non]
   Route : [si oui]

10. FICHIERS A ATTENTION SPECIALE
    [tout fichier qui pourrait poser probleme a la migration]

=== FIN DU RAPPORT ===
```

Une fois ce rapport genere et toutes les corrections faites, le projet sera pret a etre telecharge via le bouton "Download as zip" de Replit.
