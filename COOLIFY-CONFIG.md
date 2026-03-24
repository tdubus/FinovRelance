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

NOTE : MTA-STS est deja gere par une route Flask dans views.py (route /.well-known/mta-sts.txt).
Le dossier mta-sts/ n'a PAS ete cree car la route Flask suffit.
Si on souhaite un container Nginx dedie a la place, utiliser le dossier mta-sts/ avec :

  FROM nginx:alpine
  COPY mta-sts.txt /usr/share/nginx/html/.well-known/mta-sts.txt

Domaine : mta-sts.finov-relance.com
SSL : Let's Encrypt

## Etape 4 : Migration de la base de donnees

### Prerequis
- PostgreSQL 16 configure dans Coolify (service PostgreSQL)
- Acces reseau a la base Neon de production (source)
- Le container FinovRelance deploye et fonctionnel

### Procedure

1. Entrer dans le terminal du container FinovRelance via Coolify :
   Coolify > Application > Terminal (ou SSH)

2. Definir les variables de connexion :
   export DATABASE_URL_SOURCE="postgresql://USER:PASS@NEON_HOST:5432/finovrelance"
   export DATABASE_URL_TARGET="postgresql://USER:PASS@VPS_HOST:5432/finovrelance"

3. Executer le script de migration :
   bash scripts/deploy_first_time.sh

4. Verifier que la migration est complete :
   - Se connecter a l'application
   - Verifier les compteurs (utilisateurs, clients, factures)

5. Mettre a jour DATABASE_URL dans les variables Coolify pour pointer
   vers la base locale du VPS (plus Neon).

## Etape 5 : Cron jobs

Les cron jobs doivent etre configures dans cron-job.org ou equivalent.
Chaque job appelle un endpoint HTTP avec un header d'authentification.

Header : X-Job-Token: <CRON_SECRET>

Endpoints (POST) :
- /jobs/apply_pending_changes
- /jobs/database_backup
- /jobs/refresh_email_tokens
- /jobs/refresh_accounting_tokens
- /jobs/sync_email_v3
- /jobs/cleanup_old_logs

Base URL : https://app.finov-relance.com

## Etape 6 : Bascule en production

Quand le staging (test.finov-relance.com) est valide :

1. Dans Coolify : remplacer test.finov-relance.com par finov-relance.com + app.finov-relance.com
2. Ajouter MARKETING_URL=https://finov-relance.com dans les variables
3. Mettre a jour APP_URL=https://app.finov-relance.com
4. Mettre a jour MICROSOFT_REDIRECT_URI=https://app.finov-relance.com/profile/microsoft/callback
5. Redeployer (le routage par domaine s'active)
6. Basculer le DNS dans Cloudflare (A record vers IP du VPS)
7. Mettre a jour les redirect URIs OAuth dans Azure Portal, Xero, BC
8. Mettre a jour le webhook endpoint dans Stripe Dashboard
9. Mettre a jour les URLs dans cron-job.org
