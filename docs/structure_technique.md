# Structure Technique — FinovRelance

## Domaines

| Environnement | URL |
|---|---|
| **Développement** | `workspace--tdubus.repl.co` |
| **Production — Site marketing** | `finov-relance.com` (`www.finov-relance.com` → redirige en 301) |
| **Production — Application** | `app.finov-relance.com` |

---

## Serveur

- **Gunicorn** sur `0.0.0.0:5000` → port externe `80`
- Timeout : **600 secondes** (10 min, pour les backups et opérations longues)
- `reload = True`, `reuse_port = True`
- Déploiement Replit : mode **autoscale**

---

## Base de données

- **PostgreSQL 16** (Neon) dans les deux environnements
- Dev : secret `DATABASE_URL` du Repl de développement
- Prod : secret `DATABASE_URL_PROD` (ou `DATABASE_URL` si non défini) du déploiement
- Backup séparé vers **Supabase** (secret `SUPABASE_DATABASE_URL`)

---

## Email sortant

- Expéditeur système par défaut : `noreply@finovrelance.com`
- Envoi via **Microsoft Graph API** (OAuth) ou **Gmail SMTP** selon le connecteur de l'utilisateur

---

## OAuth Microsoft

- **Dev** : `https://workspace--tdubus.repl.co/profile/microsoft/callback`
- **Prod** : secret `MICROSOFT_REDIRECT_URI`

---

## Modules et URL réelles

| Blueprint | Préfixe URL | Rôle |
|---|---|---|
| `marketing_bp` | `/` | Site public (accueil, tarifs, etc.) |
| `main_bp` | `/` | Dashboard, routes générales |
| `auth_bp` | `/auth` | Login, 2FA, inscription, mot de passe |
| `client_bp` | `/clients` | Gestion des clients |
| `receivable_bp` | `/receivables` | Comptes recevables |
| `company_bp` | `/company` | Paramètres entreprise, connecteurs |
| `import_bp` | `/import` | Historique des imports |
| `email_bp` | `/emails` | Gabarits et envois de courriels |
| `note_bp` | `/notes` | Notes et communications |
| `reminder_bp` | `/reminders` | Rappels automatiques |
| `invoice_bp` | `/invoices` | Gestion des factures |
| `campaign_bp` | `/campaigns` | Campagnes d'envoi massif |
| `profile_bp` | `/profile` | Profil utilisateur + OAuth Microsoft |
| `users_bp` | `/users` | Gestion des utilisateurs |
| `admin_bp` | `/admin` | Panneau super-admin |
| `onboarding_bp` | `/onboarding` | Inscription et essai gratuit |
| `stripe_checkout_v2_bp` | `/stripe/v2/checkout` | Paiement Stripe |
| `stripe_portal_bp` | `/stripe/v2` | Portail client Stripe |
| `unified_webhook_bp` (Stripe) | `/` | Webhooks Stripe entrants |
| `oauth_callback_bp` | `/` | Callbacks OAuth (connecteurs comptables) |
| `notification_bp` | `/` | Notifications système |
| `jobs_bp` | `/jobs` | Cron jobs (sync, maintenance) |
| `backup_bp` | `/` | Backup base de données → Supabase |
| `refresh_tokens_bp` | `/` | Refresh tokens email |
| `refresh_accounting_bp` | `/` | Refresh tokens connecteurs comptables |
| `sync_emails_v3_bp` | `/` | Sync emails Outlook entrants |
| `import_progress_bp` | `/` | Progression des imports en temps réel |

---

## Routes publiques — Site marketing

```
GET      /                    Accueil
GET      /fonctionnalites     Fonctionnalités
GET      /tarifs              Tarifs
GET      /cas-usage           Cas d'usage
GET/POST /contact             Formulaire de contact
GET      /guide               Documentation
GET      /guide/<slug>        Article de guide
GET      /sitemap.xml         SEO
GET      /robots.txt          SEO
```

---

## Routes d'authentification — `/auth`

```
GET/POST  /auth/login
GET/POST  /auth/verify-2fa
POST      /auth/resend-2fa
GET/POST  /auth/forgot-password
GET/POST  /auth/reset-password/<token>
GET/POST  /auth/register
GET       /auth/logout
GET/POST  /auth/change-password
GET       /auth/switch-company/<id>
```

---

## Secrets configurés (21)

| Secret | Usage |
|---|---|
| `SESSION_SECRET` | Clé secrète Flask |
| `MICROSOFT_CLIENT_ID` | OAuth Microsoft |
| `MICROSOFT_CLIENT_SECRET` | OAuth Microsoft |
| `MICROSOFT_REDIRECT_URI` | Callback OAuth Microsoft |
| `MICROSOFT_TENANT` | Tenant Azure AD |
| `STRIPE_SECRET_KEY` | API Stripe |
| `STRIPE_PUBLISHABLE_KEY` | Clé publique Stripe |
| `STRIPE_WEBHOOK_SECRET` | Validation webhooks Stripe |
| `MAIL_PASSWORD` | Mot de passe email système |
| `BUSINESS_CENTRAL_CLIENT_ID` | OAuth Business Central |
| `BUSINESS_CENTRAL_CLIENT_SECRET` | OAuth Business Central |
| `ENCRYPTION_MASTER_KEY` | Chiffrement AES-256 (tokens OAuth stockés) |
| `REPL_CRON_SECRET` | Authentification des cron jobs |
| `BACKUP_SECRET_TOKEN` | Authentification des backups |
| `SUPABASE_USER` | Accès Supabase (backups) |
| `SUPABASE_PASSWORD` | Accès Supabase (backups) |
| `SUPABASE_DATABASE_URL` | URL base Supabase |
| `XERO_CLIENT_ID` | OAuth Xero |
| `XERO_CLIENT_SECRET` | OAuth Xero |
| `NEON_DATABASE_URL` | URL base Neon (référence directe) |
| `DATABASE_URL` | URL base de données principale |
