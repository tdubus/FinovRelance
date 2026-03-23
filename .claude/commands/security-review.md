---
name: security-review
description: >
  Audit de sécurité complet pour applications web et SaaS hébergées sur VPS. Déclencher ce skill
  dès que l'utilisateur mentionne sécurité, audit, vulnérabilité, CSRF, XSS, CORS, authentification,
  chiffrement, données sensibles, PII, API exposée, ou demande "est-ce que mon app est sécurisée".
  Couvre tout ce qui est vérifiable dans le code et sur un serveur VPS : OWASP Top 10, sessions,
  headers HTTP, chiffrement, infrastructure serveur, conteneurs Docker, pipeline CI/CD, supply chain,
  et les exigences techniques SOC 2 / ISO 27001 / Loi 25 applicables au niveau applicatif.
  Utiliser aussi avant tout déploiement en production ou quand on manipule des données personnelles.
---

# Skill — Security Review (Application + VPS)

## Vue d'ensemble

Ce skill audite tout ce qui est **vérifiable dans le code et sur le serveur VPS** : vulnérabilités
applicatives, configuration serveur, conteneurs, pipeline CI/CD, et les exigences techniques
des certifications SOC 2 / ISO 27001 qui se traduisent en code ou en configuration.

Les procédures organisationnelles (politiques RH, revues d'accès, formations) sont hors périmètre.

**Toujours lire ce fichier en entier avant de commencer l'audit.**

> ℹ️ "PII" = données personnelles identifiables. "Vecteur d'attaque" = point d'entrée exploitable.

---

## Étape 0 — Collecte du contexte

Demander si nécessaire :
1. **Type d'app** : API REST, app full-stack, SPA, mobile avec backend ?
2. **Auth** : JWT, sessions, OAuth, clés API ?
3. **Données sensibles** : paiement, santé, PII, données financières ?
4. **Stack serveur** : OS, reverse proxy (Nginx/Caddy), Docker, Coolify ?
5. **Framework** : Express, FastAPI, Laravel, Next.js, etc. ?

---

## PARTIE A — SÉCURITÉ APPLICATIVE (OWASP Top 10)

### A1. INJECTIONS (OWASP A03)

> Une injection = insérer du code malveillant dans une requête pour tromper l'application

**SQL Injection :**
- Toutes les requêtes DB utilisent-elles des paramètres préparés (prepared statements) ?
- Aucune concaténation directe de variables utilisateur dans du SQL ?
  ```js
  // ❌ DANGEREUX
  db.query(`SELECT * FROM users WHERE email = '${req.body.email}'`)
  // ✅ SÉCURISÉ
  db.query('SELECT * FROM users WHERE email = $1', [req.body.email])
  ```
- ORM utilisé correctement (pas de `.raw()` ou `.literal()` avec données utilisateur) ?
- Procédures stockées également paramétrées ?

**NoSQL Injection :**
- Validation que les objets reçus ne contiennent pas d'opérateurs MongoDB (`$where`, `$gt`, etc.) ?
- Sanitisation des objets avant passage à `find()`, `update()` ?

**XSS — Cross-Site Scripting :**
- Toutes les données affichées dans le HTML sont-elles échappées ?
- `dangerouslySetInnerHTML`, `v-html`, `innerHTML`, `document.write`, `eval()` avec données utilisateur ?
- Markdown ou HTML riche : utilisation d'un sanitizer (ex. DOMPurify) ?
- Attributs HTML dynamiques échappés (`href`, `onclick`, `src`) ?

**Command Injection :**
- Appels à `exec()`, `spawn()`, `system()`, `shell_exec()` avec données utilisateur ?
- Noms de fichiers fournis par l'utilisateur passés directement au filesystem ?

**Autres injections :**
- Template injection (Handlebars, Jinja2, Pug) avec données non sanitisées ?
- Parsers XML avec entités externes activées (XXE) ?
- SSRF : l'app peut-elle être forcée à faire des requêtes vers des URLs internes via un champ utilisateur ?
  ```
  Exemple : un champ "URL d'avatar" qui accepte http://169.254.169.254/
  → Sur un cloud, expose les credentials IAM du serveur
  → Sur un VPS, permet de scanner le réseau interne
  ```

---

### A2. AUTHENTIFICATION ET MOTS DE PASSE (OWASP A07)

**Mots de passe :**
- Hachage avec bcrypt, Argon2, ou scrypt ? (MD5/SHA1/SHA256 seuls = insuffisant)
- Facteur de coût bcrypt ≥ 12 ?
- Mots de passe jamais dans les logs ?
- Longueur minimale ≥ 12 caractères imposée côté serveur ?

**2FA :**
- MFA disponible, obligatoire pour les comptes admin ?
- TOTP implémenté correctement (pas de codes statiques) ?
- Codes de récupération stockés chiffrés ?
- Aucun bypass de 2FA via une route alternative ?

**Brute force :**
- Rate limiting sur `/login` (max 5 tentatives / 15 min / IP) ?
- Blocage temporaire du compte après N échecs ?
- Réponse générique (ne pas révéler si l'email existe ou non) ?

**Reset de mot de passe :**
- Tokens à usage unique avec expiration ≤ 1h ?
- Token invalidé immédiatement après utilisation ?

**OAuth / SSO :**
- Validation du `state` parameter (protection CSRF OAuth) ?
- Validation de l'`audience` et de l'`issuer` dans les tokens ?
- Secrets OAuth jamais exposés côté client ?

---

### A3. SESSIONS ET TOKENS JWT

**Sessions classiques :**
- ID de session aléatoire ≥ 128 bits ?
- Session régénérée après login (évite la fixation de session) ?
- Session invalidée côté serveur au logout (pas juste suppression du cookie) ?
- Timeout d'inactivité configuré ?

**JWT :**
- Algorithme `HS256` ou `RS256` — jamais `none` ?
  ```js
  // ❌ CRITIQUE : n'importe qui peut forger un token
  jwt.verify(token, secret, { algorithms: ['none'] })
  ```
- Secret JWT ≥ 256 bits aléatoires ?
- Payload ne contient pas de données sensibles (décodable sans secret) ?
- Expiration `exp` définie et vérifiée ?
- Stratégie de révocation en place (blacklist, rotation, refresh tokens) ?
- Refresh tokens stockés en HttpOnly cookie uniquement ?

**Cookies :**
- Flags `HttpOnly`, `Secure`, `SameSite=Strict/Lax` présents ?
- Aucune donnée sensible en clair dans les cookies ?

---

### A4. CSRF ET CORS

**CSRF :**
- Token CSRF sur toutes les requêtes modifiant des données (POST, PUT, DELETE, PATCH) ?
- Token unique par session et validé côté serveur ?
- Header `Origin`/`Referer` vérifié sur les requêtes sensibles ?

**CORS :**
- `Access-Control-Allow-Origin: *` absent sur les routes authentifiées ?
  ```js
  // ❌ DANGEREUX sur routes avec auth
  res.header('Access-Control-Allow-Origin', '*')
  // ✅ Liste blanche explicite
  const allowed = ['https://monapp.com']
  ```
- `Allow-Credentials: true` jamais combiné avec wildcard ?
- Méthodes et headers autorisés limités au strict nécessaire ?

---

### A5. CSP ET HEADERS HTTP

**Content Security Policy :**
- Header `Content-Security-Policy` présent sur toutes les réponses HTML ?
- Pas de `unsafe-inline` ni `unsafe-eval` dans `script-src` ?
- `script-src` restreint aux domaines de confiance ?
- Nonces ou hashes pour les scripts inline légitimes ?
- Directives couvertes : `script-src`, `style-src`, `img-src`, `font-src`, `connect-src`, `frame-ancestors`, `form-action` ?

**Headers de sécurité — tous obligatoires :**
```
Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
Content-Security-Policy: [voir ci-dessus]
```

**Vérification rapide :**
```bash
curl -I https://monapp.com | grep -E "Strict-Transport|Content-Security|X-Frame|X-Content|Referrer"
# Ou : https://securityheaders.com
```

---

### A6. DONNÉES SENSIBLES ET CHIFFREMENT (OWASP A02)

**En transit :**
- HTTPS forcé partout (HTTP → HTTPS redirect) ?
- TLS 1.2 minimum, TLS 1.3 idéal ?
- HSTS activé ?
- Certificat valide, non expiré, auto-renouvelé (Let's Encrypt) ?

**Au repos :**
- Données PII chiffrées dans la DB (noms, emails, téléphones, adresses) ?
- Numéros de carte jamais stockés — tokenisation Stripe/Braintree uniquement ?
- Clés de chiffrement séparées des données chiffrées ?
- Algorithme : AES-256-GCM uniquement (jamais DES, 3DES, RC4) ?

**Logs :**
- Aucun mot de passe, token, clé API dans les logs ?
- Données personnelles masquées dans les logs (ex. email partiellement caché) ?
- Accès aux fichiers de log restreint sur le serveur ?

**Secrets :**
- Aucune clé ou mot de passe hardcodé dans le code source ?
- Fichier `.env` dans `.gitignore` — vérifié dans l'historique git aussi ?
  ```bash
  git log --all --full-history -- .env
  git grep -i "password\|secret\|api_key" $(git log --pretty=format:"%H")
  ```
- Variables d'environnement injectées via le système de déploiement (Coolify, Docker secrets) ?

---

### A7. CONTRÔLE D'ACCÈS (OWASP A01)

- Middleware d'authentification sur chaque route protégée ?
- Vérification de rôle/permission sur chaque action, côté serveur ?
- IDOR : vérifie-t-on que l'objet demandé appartient à l'utilisateur connecté ?
  ```js
  // ❌ N'importe quel user connecté peut accéder à n'importe quel document
  const doc = await Doc.findById(req.params.id)
  // ✅ Vérification de propriété
  const doc = await Doc.findOne({ _id: req.params.id, userId: req.user.id })
  ```
- Principe du moindre privilège par rôle ?
- Routes admin séparées avec protection renforcée ?
- Documentation API (Swagger, GraphQL introspection) désactivée en production ?
- Endpoints de debug ou healthcheck n'exposant pas de données internes ?

---

### A8. CE QUE LE NAVIGATEUR (F12) RÉVÈLE

- Clés API dans le bundle JS (visibles par tout le monde) ?
- Logique de sécurité ou vérification de rôle uniquement côté client (contournable) ?
- Routes admin ou endpoints internes visibles dans le JS ?
- Source maps (`.map`) accessibles en production (révèle le code source complet) ?
- Stack traces ou versions de framework dans les réponses d'erreur ?
- `localStorage` utilisé pour tokens ou données sensibles (accessible via XSS) ?

---

### A9. UPLOAD DE FICHIERS

- Types MIME validés côté serveur (pas juste l'extension) ?
- Taille maximale appliquée côté serveur ?
- Fichiers stockés hors du webroot et jamais exécutés directement ?
- Noms de fichiers sanitisés (path traversal : `../../../etc/passwd`) ?
- Scan antivirus sur les uploads si données critiques ?

---

### A10. DÉPENDANCES VULNÉRABLES (OWASP A06)

```bash
# Node.js
npm audit --audit-level=moderate

# Python
pip-audit

# Licences
npx license-checker --summary --excludePrivatePackages
```

- Vulnérabilités critiques/hautes identifiées et corrigées ?
- Dependabot ou Renovate activé sur le repo ?

---

## PARTIE B — INFRASTRUCTURE VPS

### B1. RÉSEAU ET EXPOSITION

- Seuls les ports nécessaires ouverts (80, 443 + SSH si besoin) ?
  ```bash
  # Vérifier les ports ouverts depuis l'extérieur
  nmap -sV monserveur.com
  ```
- Ports DB (5432, 3306, 27017), Redis (6379), services internes jamais exposés publiquement ?
- Firewall configuré (ufw, iptables) avec règles documentées ?
  ```bash
  ufw status verbose
  ```
- SPF, DKIM, DMARC configurés sur le domaine email (anti-spoofing) ?
  ```
  SPF   : "v=spf1 include:sendgrid.net ~all"
  DKIM  : signature cryptographique des emails sortants
  DMARC : "v=DMARC1; p=reject; rua=mailto:dmarc@mondomaine.com"
  ```
- Protection DDoS au niveau CDN (Cloudflare ou équivalent) ?
- Rate limiting global par IP au niveau Nginx/Caddy en complément du rate limiting applicatif ?

---

### B2. SÉCURITÉ SERVEUR

- Accès SSH par clé uniquement — authentification par mot de passe désactivée ?
  ```bash
  grep "PasswordAuthentication" /etc/ssh/sshd_config  # doit être "no"
  grep "PermitRootLogin" /etc/ssh/sshd_config          # doit être "no"
  ```
- Port SSH non standard ou accès restreint par IP whitelist ?
- Fail2ban configuré (protection brute force SSH et HTTP) ?
  ```bash
  fail2ban-client status
  ```
- Application tournant en utilisateur non-root dédié ?
- Mises à jour de sécurité OS automatiques activées ?
  ```bash
  # Debian/Ubuntu
  dpkg -l unattended-upgrades
  ```
- Version du serveur web cachée dans Nginx/Caddy (pas de header `Server: nginx/1.18`) ?
- Pages d'erreur personnalisées (pas de stack trace exposée) ?
- Logs d'accès et d'erreur centralisés et avec rotation configurée ?
- Backups automatisés, chiffrés, stockés hors du VPS principal, et testés ?

---

### B3. CONTENEURS ET DOCKER

- Images basées sur des images officielles et récentes ?
- Images scannées pour les CVE ?
  ```bash
  trivy image monimage:latest
  docker scout cves monimage:latest
  ```
- Conteneurs tournant en utilisateur non-root ?
  ```dockerfile
  # ❌ Root par défaut
  FROM node:20
  # ✅ Non-root
  FROM node:20-alpine
  USER node
  ```
- Secrets jamais dans les variables d'environnement Docker en clair dans `docker-compose.yml` commité ?
- Registre privé pour les images maison ?
- Réseau Docker configuré pour isoler les services (pas tout sur le bridge par défaut) ?

---

### B4. PIPELINE CI/CD ET SUPPLY CHAIN

- Secrets de production jamais loggés dans les logs CI ?
  ```yaml
  # ❌ Affiche le secret en clair dans les logs
  - run: echo ${{ secrets.DB_PASSWORD }}
  ```
- Secrets stockés dans le gestionnaire natif du CI (GitHub Secrets, GitLab CI Variables) et non dans le code ?
- Actions GitHub tierces épinglées par hash SHA (pas par tag mutable) :
  ```yaml
  # ❌ Tag modifiable — peut être remplacé par du code malveillant
  uses: actions/checkout@v3
  # ✅ Hash immuable
  uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11
  ```
- Branch protection activée (PR obligatoire, CI vert avant merge en production) ?
- `npm ci` plutôt que `npm install` (respecte strictement le lockfile) ?
- `package-lock.json` / `yarn.lock` / `poetry.lock` commité et versionné ?
- Scan de secrets dans le code activé dans le pipeline :
  ```bash
  npx secretlint "**/*"
  ```

---

## PARTIE C — EXIGENCES TECHNIQUES SOC 2 / ISO 27001 / LOI 25

> Ces items sont les exigences de certification qui se traduisent en **configuration ou code**,
> contrairement aux exigences organisationnelles (politiques, procédures) qui sont hors périmètre.

### C1. TRAÇABILITÉ ET LOGS D'AUDIT

*(SOC 2 CC7, ISO 27001 A.12)*

- Logs d'audit applicatifs enregistrant qui a fait quoi et quand (création, modification, suppression de données sensibles) ?
- Logs incluant : timestamp, user ID, action, ressource concernée, IP source ?
- Logs protégés contre la modification (écriture append-only, stockage séparé) ?
- Rétention des logs ≥ 12 mois (exigence SOC 2 CC7) ?
- Logs d'accès aux données sensibles (PII, données financières) traçables par utilisateur ?
  ```js
  // Exemple de log d'audit structuré
  logger.info({
    event: 'document.accessed',
    userId: req.user.id,
    resourceId: doc.id,
    ip: req.ip,
    timestamp: new Date().toISOString()
  })
  ```

---

### C2. CHIFFREMENT ET PROTECTION DES DONNÉES

*(SOC 2 C1, ISO 27001 A.10, Loi 25)*

- Chiffrement au repos pour toutes les PII (pas juste les mots de passe) ?
- Chiffrement en transit TLS 1.2+ sur toutes les communications, y compris internes (app ↔ DB) ?
- Clés de chiffrement stockées séparément des données (pas dans la même DB) ?
- Rotation des clés de chiffrement possible sans recréer toute la DB (architecture key-versioning) ?
- Données de test et de développement anonymisées — jamais de données de production en dev ?

---

### C3. CONTRÔLE D'ACCÈS TECHNIQUE

*(SOC 2 CC6, ISO 27001 A.9)*

- MFA obligatoire implémenté dans l'application pour les rôles admin et privilégiés ?
- Sessions expirées automatiquement après inactivité (timeout configurable) ?
- Logs de connexion (succès et échecs) enregistrés avec IP et timestamp ?
- Tentatives de connexion échouées loggées et alertes déclenchées au-delà d'un seuil ?
- Principe du moindre privilège appliqué dans les rôles de l'application ?
- Isolation multi-tenant vérifiable : un tenant ne peut jamais accéder aux données d'un autre ?

---

### C4. DISPONIBILITÉ ET RÉSILIENCE

*(SOC 2 A1)*

- Health check endpoint en place (sans exposer d'infos internes) ?
- Monitoring de disponibilité configuré avec alertes (UptimeRobot, Betterstack, Datadog) ?
- Stratégie de backup avec RPO défini et testé (combien de données peut-on perdre au max) ?
- Backups automatiques testés régulièrement (un backup non testé n'est pas un backup) ?
- Processus de rollback documenté et testé pour les déploiements ?

---

### C5. GESTION DES VULNÉRABILITÉS

*(SOC 2 CC7, ISO 27001 A.12)*

- Scan de vulnérabilités des dépendances intégré dans le pipeline CI (bloque le déploiement si critique) ?
- Scan de sécurité SAST intégré dans le pipeline :
  ```bash
  # Node.js
  npx eslint --plugin security .
  # Python
  bandit -r .
  ```
- Images Docker rescannées à chaque build (pas juste à la création) ?
- Certificats SSL surveillés pour l'expiration (alerte 30 jours avant) ?

---

### C6. PROTECTION DES DONNÉES PERSONNELLES (LOI 25 / RGPD)

> Loi 25 Québec s'applique à toute organisation traitant des données de résidents québécois.

**Implémentation technique requise dans l'application :**

- Fonctionnalité d'export des données utilisateur (droit à la portabilité) — format JSON ou CSV ?
- Fonctionnalité de suppression complète du compte et de toutes les données associées (droit à l'effacement) ?
- Consentement granulaire implémenté pour les cookies non essentiels (analytics, marketing) — pas de pré-cochage ?
- Cookies non essentiels bloqués techniquement avant acceptation (pas juste affichage de la bannière) ?
- Durée de rétention des données appliquée automatiquement (suppression ou anonymisation après X mois) ?
- Données personnelles masquées dans les logs (emails, téléphones, noms) ?
- Accès aux PII limité aux rôles qui en ont besoin (principe du besoin d'en connaître, vérifié dans le code) ?

---

## PARTIE D — OUTILS DE VÉRIFICATION

```bash
# Headers HTTP de sécurité
curl -I https://monapp.com
# Ou : https://securityheaders.com

# SSL/TLS
# https://www.ssllabs.com/ssltest/

# Ports ouverts
nmap -sV monserveur.com

# Dépendances vulnérables
npm audit --audit-level=moderate
pip-audit

# Secrets dans le code
npx secretlint "**/*"
trufflehog git file://. --since-commit HEAD~10

# Images Docker
trivy image monimage:latest

# Firewall
ufw status verbose
fail2ban-client status

# Config SSH
grep -E "PasswordAuthentication|PermitRootLogin|Port" /etc/ssh/sshd_config
```

---

## Format du rapport de sécurité

```
## Rapport de Sécurité — [Nom du projet] — [Date]

### Score de sécurité global : [X/10]
### Couverture SOC 2 / ISO 27001 (technique) : [X/10]

---

### 🚨 Critique (corriger IMMÉDIATEMENT — bloque la mise en production)
[Problème | Localisation | Impact | Correction]

### ⚠️ Important (corriger avant certification ou prochain déploiement majeur)
[Problème | Localisation | Impact | Correction]

### ℹ️ Améliorations recommandées
[Problème | Impact | Correction]

### ✅ Bonnes pratiques déjà en place

### Roadmap
Phase 1 — Avant prochain déploiement :
Phase 2 — Avant certification SOC 2 / ISO 27001 :
```

---

## Règles de comportement du skill

1. **Se limiter à ce qui est vérifiable** : code, config serveur, pipeline — pas de recommandations organisationnelles
2. **Toujours expliquer le risque** : "pourquoi c'est dangereux" avant "comment corriger"
3. **Exemples concrets** : montrer le code vulnérable ET la correction
4. **Prioriser** : un SQL injection sur le login est plus urgent qu'un header manquant
5. **Vulgariser** : expliquer les termes techniques pour un non-développeur
6. **Jamais de fausse assurance** : ce skill ne remplace pas un pentest par un expert certifié
