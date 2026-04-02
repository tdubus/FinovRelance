# Plan d'implementation - Connecteur Pennylane

## Statut : PRET A CODER

---

## 1. Contexte

Pennylane est un logiciel de comptabilite francais (pennylane.com).
L'API v2 est documentee sur https://pennylane.readme.io.
Ce connecteur suit exactement le patron etabli par QuickBooks et Xero :
lecture seule (Pennylane -> FinovRelance), sync asynchrone en thread daemon,
un seul bouton "Synchroniser", memes modeles DB partages.

---

## 2. Specificites de l'API Pennylane vs QB/Xero

| Aspect | Pennylane | QuickBooks | Xero |
|---|---|---|---|
| Auth | OAuth 2.0 standard | OAuth 2.0 | OAuth 2.0 |
| Token lifetime | **24h** access, 90j refresh (rotating) | 1h access | 30min access, 90j refresh (rotating) |
| Auth URL | `https://app.pennylane.com/oauth/authorize` | `appcenter.intuit.com/connect/oauth2` | `login.xero.com/.../authorize` |
| Token URL | `https://app.pennylane.com/oauth/token` | `oauth.platform.intuit.com/.../bearer` | `identity.xero.com/connect/token` |
| API Base | `https://app.pennylane.com/api/external/v2` | `quickbooks.api.intuit.com/v3/company/{realm}` | `api.xero.com/api.xro/2.0` |
| Pagination | **Cursor-based** (`cursor` + `limit`, max 100) | Offset (`STARTPOSITION N MAXRESULTS 1000`) | Page-based (page 1000) |
| Rate limit | **25 req / 5 sec** par token (headers `ratelimit-*`) | Quasi-illimite (retry 429) | 60 req/min/tenant |
| Paiements | **Pas de ressource dediee** — `matched_transactions` par facture | `Payment` entity globale | Factures `PAID` |
| PDF download | **Pas d'endpoint dedie** — champ `public_file_url` (URL signee, expire 30min) | `/invoice/{id}/pdf` endpoint | `/Invoices/{id}` avec Accept: application/pdf |
| Webhooks | **Non disponibles** — change tracking via `/changelogs/*` | Non | Non |
| Montants | **Strings** (ex: `"1234.56"`) | Numbers | Numbers |
| Tenant ID | **Pas de tenant ID** — le token est lie au compte | `realmId` | `Xero-Tenant-Id` header |
| Test endpoint | `GET /me` | `GET /companyinfo/1` | `GET /organisation` |

---

## 3. Scopes OAuth requis

```
customers:readonly customer_invoices:readonly
```

- `customers:readonly` : lire les clients (company + individual)
- `customer_invoices:readonly` : lire les factures ET les matched_transactions (sous-ressource, meme scope)
- `transactions:readonly` n'est PAS necessaire (les matched_transactions sont accessibles via le scope customer_invoices)

---

## 4. Fichiers a creer

### 4.1 `pennylane_connector.py` (fichier principal ~800 lignes)

Classe `PennylaneConnector` calquee sur `XeroConnector` :

```python
class PennylaneConnector:
    AUTHORIZATION_URL = 'https://app.pennylane.com/oauth/authorize'
    TOKEN_URL = 'https://app.pennylane.com/oauth/token'
    API_BASE_URL = 'https://app.pennylane.com/api/external/v2'
    SCOPES = 'customers:readonly customer_invoices:readonly'
```

#### 4.1.1 OAuth

| Methode | Detail |
|---|---|
| `__init__(connection_id, company_id)` | Charge connection, valide company_id (IDOR), charge `PENNYLANE_CLIENT_ID`/`PENNYLANE_CLIENT_SECRET` depuis env |
| `get_authorization_url(state) -> str` | Construit URL avec `response_type=code`, `client_id`, `redirect_uri`, `scope`, `state` |
| `exchange_code_for_tokens(code, state) -> dict` | POST vers TOKEN_URL avec `grant_type=authorization_code`. Retourne `{access_token, refresh_token, expires_in}` |
| `refresh_access_token() -> bool` | POST vers TOKEN_URL avec `grant_type=refresh_token`. Met a jour les 2 tokens (rotating) + `token_expires_at` en DB |
| `test_connection() -> bool` | `GET /me` — valide que le token est fonctionnel |

**Note importante** : Pennylane n'a pas de `tenant_id`. Le token OAuth est directement lie au compte.
`company_id_external` stockera l'email ou l'id retourne par `GET /me`.

#### 4.1.2 API Client

| Methode | Detail |
|---|---|
| `make_api_request(method, endpoint, params=None) -> dict` | Refresh token inline si `is_token_valid() == False`. Headers: `Authorization: Bearer {token}`. Gere le rate limiting via `_handle_rate_limit()` |
| `_handle_rate_limit(response)` | Lit headers `ratelimit-remaining` et `retry-after`. Si `remaining < 3` : `time.sleep()` proactif. Si 429 : `time.sleep(retry-after)` |
| `_paginate_cursor(endpoint, params=None) -> generator` | Itere avec `cursor` + `limit=100`. Yield chaque batch. Arrete quand `has_more == False` ou `next_cursor == None`. Respecte rate limit entre chaque page |

**Session HTTP** : Utilisera `create_pennylane_session()` (voir 5.3) avec retry sur 429/5xx.

#### 4.1.3 sync_customers(company_id, sync_log_id) -> Tuple[int, int]

**Endpoint** : `GET /customers` avec pagination cursor, limit=100

**Mapping Pennylane -> Client local** :

| Pennylane | Client (local) | Notes |
|---|---|---|
| `name` (company) ou `first_name + last_name` (individual) | `name` | |
| `name` ou `external_reference` | `code_client` | Priorite: `external_reference` si present, sinon `name` |
| `emails[0]` | `email` | Premier email du tableau |
| `phone` | `phone` | |
| `billing_address` -> `address, postal_code, city, country_alpha2` | `address` | Concatenation en string |
| `payment_conditions` | `payment_terms` | Mapping: `"30_days"` -> `"Net 30"`, etc. |
| `billing_language` | `language` | `"fr_FR"` -> `"fr"`, `"en_GB"` -> `"en"` |
| `id` (Pennylane integer) | — | Stocke en memoire pour resolution client dans sync_invoices |

**Deduplication** : par `code_client` + `company_id` (UniqueConstraint existant).
Upsert : update si existe, create sinon. `collector_id = None` pour nouveaux clients.

**Verifications pre-sync** :
- `CompanySyncUsage.check_company_sync_limit()`
- `company.assert_client_capacity()`
- `sync_log.is_stop_requested()` a chaque page

#### 4.1.4 sync_invoices(company_id, sync_log_id) -> Tuple[int, int]

**Endpoint** : `GET /customer_invoices` avec pagination cursor, limit=100

**Filtrage** : `filter=[{"field":"draft","operator":"eq","value":false}]`
(exclut les brouillons). Le filtrage par statut `paid`/`unpaid` n'est PAS disponible
cote API — il faut filtrer cote client.

**Logique cote client** :
- Garder les factures ou `paid == false` ET `remaining_amount_with_tax > 0`
- Factures `paid == true` deja presentes localement : **supprimer** (meme logique QB/Xero)
- Ignorer les `status` suivants : `"draft"`, `"cancelled"`, `"credit_note"`, `"proforma"`, `"shipping_order"`, `"purchasing_order"`, `"estimate_pending"`, `"estimate_accepted"`, `"estimate_invoiced"`, `"estimate_denied"`
- Garder uniquement : `"upcoming"`, `"late"`, `"partially_paid"`, `"incomplete"`

**Mapping Pennylane -> Invoice local** :

| Pennylane | Invoice (local) | Notes |
|---|---|---|
| `invoice_number` | `invoice_number` | |
| `remaining_amount_with_tax` | `amount` | **String -> Decimal** |
| `amount` (ou `currency_amount` si devise non-EUR) | `original_amount` | Total TTC original. **String -> Decimal** |
| `date` | `invoice_date` | ISO 8601 -> date |
| `deadline` | `due_date` | ISO 8601 -> date |
| `id` (integer) | `invoice_id_external` | **Stocke comme string** pour PDF download |
| `customer.id` | — | Resolution vers `client_id` local via index en memoire |

**Resolution client** : Construire un dict `{pennylane_customer_id: local_client_id}` au debut de sync_invoices
en croisant les clients deja synchronises.

**Deduplication** : par `invoice_number` + `client_id` + `company_id` (UniqueConstraint existant).

#### 4.1.5 sync_payments(company_id, sync_log_id) -> int

**Strategie** : Pennylane n'a pas de ressource Payment globale.
Les paiements sont des `matched_transactions` rattachees a chaque facture.

**Approche en 2 etapes** :

1. **Detecter les factures payees** via change tracking :
   `GET /changelogs/customer_invoices` avec `start_date` = derniere sync (ou 4 semaines max)
   Filtrer les changes avec `operation` in (`insert`, `update`).
   Recuperer les IDs de factures modifiees.

2. **Pour chaque facture modifiee ayant `paid == true` ou `status == "paid"`** :
   `GET /customer_invoices/{id}/matched_transactions`
   Chaque matched_transaction devient un `ReceivedPayment`.

**Mapping matched_transaction -> ReceivedPayment** :

| Pennylane matched_transaction | ReceivedPayment (local) | Notes |
|---|---|---|
| `id` | `external_payment_id` | Format: `"PENNYLANE_MT_{id}"` |
| Facture parent `id` | `external_invoice_id` | ID Pennylane de la facture |
| Facture parent `invoice_number` | `invoice_number` | |
| Facture parent `date` | `invoice_date` | |
| Facture parent `deadline` | `invoice_due_date` | |
| Facture parent `amount` | `original_invoice_amount` | String -> Decimal |
| `date` | `payment_date` | |
| `amount` | `payment_amount` | String -> Decimal |
| `'pennylane'` | `source` | Constante |

**Deduplication** : UniqueConstraint existant sur `(company_id, source, external_payment_id, invoice_number)`.

**Sync incrementale** : Stocker `processed_at` du dernier changement traite.
Utiliser comme `start_date` au prochain sync. Premier run : pas de `start_date` (4 semaines d'historique).

**Fallback si change tracking insuffisant** : Iterer les factures locales connues
et verifier leur statut via `GET /customer_invoices/{id}` — si `paid == true`,
recuperer les matched_transactions.

#### 4.1.6 download_invoice_pdf(invoice_id_external) -> bytes

**Pennylane n'a pas d'endpoint PDF dedie.**

Approche :
1. `GET /customer_invoices/{invoice_id_external}` — recuperer le champ `public_file_url`
2. Si `public_file_url` est non-null : `GET {public_file_url}` (sans auth, URL signee) — retourne le PDF bytes
3. Si `public_file_url` est null : lever une exception explicite

**Note** : L'URL expire apres 30 minutes. On la recupere a la volee a chaque demande de telechargement.

---

## 5. Fichiers a modifier

### 5.1 `models.py`

**Ligne 2109** — Ajouter `'pennylane'` a la liste `allowed_types` :

```python
allowed_types = ['quickbooks', 'sage', 'xero', 'wave', 'freshbooks', 'business_central', 'odoo', 'pennylane']
```

Aucun nouveau modele requis. Aucune nouvelle colonne.
`AccountingConnection` supporte deja tout ce dont Pennylane a besoin :
- `system_type = 'pennylane'`
- `system_name = 'Pennylane'`
- `company_id_external` = ID ou email du compte Pennylane (retourne par `GET /me`)
- Tokens chiffres AES-256 via les property existantes
- `is_sandbox = False` (pas de sandbox Pennylane via OAuth standard)

### 5.2 `views/company_views.py`

Ajouter 4 routes sur `company_bp` (apres le bloc Xero, avant le bloc Odoo) :

| Route | Methode | Fonction | Calquee sur |
|---|---|---|---|
| `/company/pennylane-connect` | GET | `pennylane_connect()` | `xero_connect()` L3225 |
| `/company/pennylane/callback` | GET | `pennylane_callback()` | `xero_callback()` L3275 |
| `/company/pennylane-disconnect/<int:connection_id>` | POST | `pennylane_disconnect()` | `xero_disconnect()` L3375 |
| `/company/pennylane-sync` | POST | `pennylane_sync()` | `xero_sync()` L3411 |

**`pennylane_connect()`** :
- Verifie `can_access_company_settings()` + `check_accounting_access()`
- Instancie `PennylaneConnector()` sans connection_id
- Genere state via `secrets.token_urlsafe(32)`
- Stocke `session['pennylane_state']` et `session['pennylane_company_id']`
- Redirect vers `get_authorization_url(state)`

**`pennylane_callback()`** :
- Valide state vs `session['pennylane_state']`
- Valide `UserCompany` (protection IDOR)
- `exchange_code_for_tokens(code, state)` -> tokens
- Cree ou update `AccountingConnection(system_type='pennylane', system_name='Pennylane')`
- `token_expires_at = utcnow() + timedelta(seconds=expires_in)` (24h)
- `company_id_external` = valeur retournee par echange de tokens ou `GET /me`
- Cleanup session

**`pennylane_disconnect()`** :
- Copie exacte du pattern Xero : `is_active=False`, null tokens, null sync_stats
- AuditLog

**`pennylane_sync()`** :
- Verifie `CompanySyncUsage.check_company_sync_limit()`
- `CompanySyncUsage.increment_company_sync_count()`
- `ensure_monitoring_started()`
- Thread daemon avec `run_sync()` :
  1. `sync_customers(company_id, sync_log_id)`
  2. Check manual stop
  3. `sync_invoices(company_id, sync_log_id)`
  4. Check manual stop
  5. `sync_payments(company_id, sync_log_id)`
  6. Update `SyncLog`, `connection.last_sync_at`, `sync_stats`
  7. `send_notification()` + `AuditLog`

**Modifier `_get_connector_context()`** (L138-245) :
- Ajouter `'pennylane'` dans le dict `name` (L166-171) : `'pennylane': 'Pennylane'`
- Ajouter `'pennylane'` dans le dict `description` (L179-184) : `'pennylane': 'Synchronisez automatiquement vos clients et factures depuis Pennylane'`
- Ajouter `'pennylane'` dans la liste L217 : `for system_type in ['quickbooks', 'xero', 'business_central', 'odoo', 'pennylane']:`
- Ajouter dans les dicts L221-226 et L234-239

### 5.3 `utils/http_client.py`

Ajouter apres `create_xero_session()` (L358) :

```python
def create_pennylane_session() -> RobustHTTPSession:
    """Session optimisee pour Pennylane API v2
    
    Rate limit: 25 requetes par fenetre de 5 secondes.
    Timeout standard, retry sur 429 et erreurs serveur.
    """
    return RobustHTTPSession(
        timeout=30,
        max_retries=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]
    )
```

### 5.4 `jobs/refresh_accounting_tokens.py`

**Ligne 242** — Ajouter le dispatch Pennylane dans `_refresh_connection_token()` :

```python
elif connection.system_type == 'pennylane':
    from pennylane_connector import PennylaneConnector
    connector = PennylaneConnector(connection_id=connection.id, company_id=connection.company_id)
    return connector.refresh_access_token()
```

**Note** : Le token Pennylane dure 24h. Le cron tourne toutes les 30min.
`needs_token_refresh()` (seuil 30min avant expiration) declenchera le refresh
~23h30 apres la derniere obtention. Pas de changement au seuil necessaire.

### 5.5 `views/invoice_views.py`

**Ligne 213** — Ajouter le bloc Pennylane dans `download_invoice_pdf()`, avant le `else` :

```python
elif connection.system_type == 'pennylane':
    if not invoice.invoice_id_external:
        flash('Cette facture n\'a pas ete synchronisee avec Pennylane.', 'warning')
        return redirect(url_for('client.detail_client', id=invoice.client_id))

    from pennylane_connector import PennylaneConnector
    connector = PennylaneConnector(connection.id, company.id)
    pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)
```

### 5.6 `templates/company/settings.html`

**Boutons pour connecteur actif** (apres le bloc Xero/Odoo) :

Ajouter un bloc `{% elif conn.type == 'pennylane' %}` avec :
- Bouton "Synchroniser maintenant" (`id="sync-btn-pennylane"`, `onclick="triggerPennylaneSync()"`)
- Formulaire de deconnexion POST avec CSRF token et confirmation

**Bouton "Connecter" dans la grille** :

Ajouter un bloc `{% elif connector.type == 'pennylane' %}` avec :
- Lien vers `url_for('company.pennylane_connect')` si `accounting_permission.allowed`
- Bouton disabled sinon

**JavaScript** — Ajouter `triggerPennylaneSync()` :

Fonction calquee sur `triggerXeroSync()` :
- Desactive le bouton, affiche spinner via `textContent`
- `fetch()` POST vers `company.pennylane_sync` avec CSRF header
- `location.reload()` en cas de succes
- Restaure le bouton en cas d'erreur

### 5.7 `templates/clients/_invoice_table.html`

**Lignes 67 et 156** — Ajouter `'pennylane'` a la liste des systemes supportant le PDF :

```html
{% set supports_pdf = accounting_system_type in ['quickbooks', 'xero', 'odoo', 'pennylane'] and invoice.invoice_id_external %}
```

### 5.8 `templates/clients/detail.html`

**Ligne 1466** — Ajouter `'pennylane'` :

```html
{% if has_accounting_connection and accounting_system_type in ['quickbooks', 'xero', 'odoo', 'business_central', 'pennylane'] %}
```

### 5.9 Logo

Ajouter `static/pennylane-logo.png` — Le logo Pennylane.
Convention de nommage : `{system_type}-logo.png` (coherent avec les autres).

### 5.10 Variables d'environnement

Ajouter dans `.env.example` et configurer dans Coolify :

```
PENNYLANE_CLIENT_ID=votre-client-id
PENNYLANE_CLIENT_SECRET=votre-client-secret
```

---

## 6. Ce qui ne change PAS

- **Aucune modification aux connecteurs existants** (QuickBooks, Xero, Business Central, Odoo)
- **Aucun nouveau modele DB** — `AccountingConnection`, `SyncLog`, `ReceivedPayment`, `CompanySyncUsage` sont tous reutilises
- **Aucune nouvelle colonne DB** — tous les champs necessaires existent deja
- **Aucune migration DB** — seule modification : ajouter `'pennylane'` au validateur Python (pas en DB)
- **Le cron job existant** gere deja le refresh multi-connecteurs — un seul `elif` a ajouter
- **Le sync_monitor existant** fonctionne deja pour tout type de connexion

---

## 7. Rate limiting client-side (specifique Pennylane)

Le rate limit Pennylane est le plus strict de tous les connecteurs (25 req / 5 sec).

Implementation dans `PennylaneConnector._handle_rate_limit(response)` :

- Lire `ratelimit-remaining` de chaque reponse
- Si `status_code == 429` : lire `retry-after`, sleep, retourner signal de retry
- Si `remaining <= 3` : lire `ratelimit-reset` (timestamp unix), calculer le delai, sleep proactivement
- Integre dans `make_api_request()` apres chaque appel

---

## 8. Gestion des erreurs

Pattern identique a Xero :

- **OAuth** : try/except autour de `exchange_code_for_tokens`, flash message + redirect
- **Token refresh** : echec silencieux dans le cron, log error. Echec inline dans `make_api_request` : raise
- **Sync** : try/except global dans le thread, met `SyncLog.status = 'failed'`, envoie notification d'erreur
- **DB** : `db.session.rollback()` dans chaque catch, puis re-raise ou return partiel
- **Manual stop** : `sync_log.is_stop_requested()` verifiee a chaque page de pagination
- **API errors** : Pennylane retourne des erreurs structurees — les logger avec le message complet

---

## 9. Securite

- **IDOR** : Validation `connection.company_id == company_id` dans le constructeur (comme Xero)
- **CSRF** : State token `secrets.token_urlsafe(32)` stocke en session, valide au callback
- **Tokens** : Chiffres AES-256 via `encryption_service` (property existantes)
- **Permissions** : `can_access_company_settings()` + `check_accounting_access()` sur chaque route
- **UserCompany** : Verification au callback que l'utilisateur a toujours acces a la company
- **Read-only** : Le connecteur ne fait que lire (scopes `readonly`). Aucune ecriture vers Pennylane
- **Secrets** : `PENNYLANE_CLIENT_ID` et `PENNYLANE_CLIENT_SECRET` via `os.environ.get()` uniquement
- **Session cleanup** : Les cles `pennylane_*` sont supprimees de la session apres callback
- **Rate limit** : Respect strict des headers pour eviter le bannissement

---

## 10. Ordre d'implementation

1. `models.py` — Ajouter `'pennylane'` au validateur (1 ligne)
2. `utils/http_client.py` — Ajouter `create_pennylane_session()` (~10 lignes)
3. `pennylane_connector.py` — Fichier complet (~800 lignes)
4. `views/company_views.py` — 4 routes + modifier `_get_connector_context()` (~400 lignes)
5. `jobs/refresh_accounting_tokens.py` — Ajouter elif pennylane (~4 lignes)
6. `views/invoice_views.py` — Ajouter bloc pennylane PDF (~7 lignes)
7. `templates/company/settings.html` — Boutons + JS (~40 lignes)
8. `templates/clients/_invoice_table.html` — Ajouter `'pennylane'` (2 lignes)
9. `templates/clients/detail.html` — Ajouter `'pennylane'` (1 ligne)
10. `static/pennylane-logo.png` — Logo
11. `.env.example` — Documenter les 2 variables

---

## 11. Prerequis cote Pennylane

Avant de pouvoir tester :
1. Contacter `partnerships@pennylane.com` pour obtenir un sandbox OAuth
2. Enregistrer l'app avec le redirect URI : `https://app.finov-relance.com/company/pennylane/callback`
3. Obtenir `PENNYLANE_CLIENT_ID` et `PENNYLANE_CLIENT_SECRET`
4. Configurer les scopes autorises : `customers:readonly customer_invoices:readonly`
