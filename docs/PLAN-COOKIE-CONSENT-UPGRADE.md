# Plan : Mise à jour du système de consentement cookies

## Objectif

Remplacer le système de cookies simpliste actuel (Accepter/Refuser) par le système granulaire du site Finova Solutions, avec 3 catégories de cookies et conformité Loi 25 / PIPEDA. Ensuite, ajouter Visitors Analytics dans les politiques légales.

## Référence

Projet source : `C:\Users\tonyd\Projects\site-web-finova\Site-Web-Finova`
- `client/src/hooks/use-cookie-consent.ts` — Logique de consentement (React)
- `client/src/components/cookie-consent-banner.tsx` — Bannière UI (React)
- `client/src/pages/politique-cookies.tsx` — Page politique cookies

## État actuel FinovRelance

### Fichiers concernés
- `marketing_site/static/js/cookie-consent.js` — JS simple : accepter/refuser, localStorage, log serveur
- `marketing_site/templates/*.html` — Bannière HTML dans chaque template (div #cookieConsent)
- `marketing_site/static/css/marketing.css` — Styles `.cookie-consent`
- `templates/legal/cookies.html` — Page politique cookies (Jinja2/Bootstrap, extends base.html)

### Comportement actuel
1. Bannière simple en bas : "Nous utilisons des cookies" + Accepter / Refuser
2. Stocke `finovRelanceCookieConsent` = "accepted" ou "declined" dans localStorage
3. Expiration 365 jours
4. Log côté serveur via `/auth/api/log-cookie-consent`
5. Aucune gestion granulaire (pas de distinction essential/statistics/marketing)
6. GTM et tous les scripts se chargent toujours, quel que soit le consentement

## Cible (copier le système Finova)

### Catégories de cookies
1. **Essentiels** (toujours actifs, non désactivables)
   - Préférences de thème
   - Consentement cookies (localStorage)
   - Session Flask (cookie de session)
2. **Statistiques** (optionnel)
   - Google Tag Manager / Google Analytics 4
   - Visitors Analytics (nouveau)
3. **Marketing** (optionnel)
   - Google Ads (si utilisé à l'avenir)

### Comportement cible
1. Bannière flottante en bas à droite (pas plein écran)
2. 3 boutons : "Tout accepter" / "Tout refuser" / "Personnaliser mes choix"
3. Mode personnalisé : toggle par catégorie (essentiels verrouillé ON, statistiques et marketing ON/OFF)
4. Acceptation implicite des CGU et politique de confidentialité
5. Stockage dans localStorage avec versioning (`consentVersion: '1.0'`)
6. GTM consent mode : `analytics_storage` et `ad_storage` respectent les choix
7. Visitors Analytics : chargé uniquement si statistiques accepté
8. Log côté serveur (garder l'endpoint existant, enrichir avec les préférences)

## Plan d'implémentation

### Étape 1 : Nouveau JS cookie-consent

Réécrire `marketing_site/static/js/cookie-consent.js` en JS vanilla (pas React) en copiant la logique de `use-cookie-consent.ts` :

- Structure de données :
```js
{
  preferences: { essential: true, statistics: false, marketing: false },
  cguAccepted: true,
  privacyAccepted: true,
  consentDate: "2026-04-01T...",
  consentVersion: "1.0"
}
```
- Clé localStorage : `finova_cookie_consent` (aligner avec Finova)
- Fonctions : `acceptAll()`, `rejectAll()`, `savePreferences(prefs)`, `openSettings()`
- Intégration GTM consent mode : `gtag('consent', 'update', {...})`
- Conditionner le chargement de Visitors Analytics au consentement statistiques

### Étape 2 : Nouvelle bannière HTML

Remplacer la bannière `<div id="cookieConsent">` dans chaque template par la nouvelle bannière copiant le design de Finova :

- Position : `fixed bottom-right`, max-width 420px
- Design : carte arrondie avec shadow, icônes FontAwesome (pas Lucide)
- Vue initiale : titre + description + 3 boutons
- Vue personnaliser : 3 catégories avec toggles (switches CSS, pas de lib)
- Liens : CGU, Confidentialité, Politique cookies
- Animation : fade-in/slide-up au chargement, fade-out au dismiss

Comme le HTML est dupliqué dans chaque template, créer un fichier include :
`marketing_site/templates/_cookie_consent.html`

Chaque template remplace son ancienne bannière par :
```jinja
{% include '_cookie_consent.html' %}
```

### Étape 3 : Conditionner les scripts au consentement

Modifier le chargement des scripts dans chaque template :

**GTM** (actuellement chargé inconditionnellement) :
- Garder le snippet GTM dans le `<head>` MAIS avec consent mode par défaut "denied"
- Le JS cookie-consent fait `gtag('consent', 'update', ...)` quand l'utilisateur accepte

**Visitors Analytics** :
- Retirer le script `<script src="https://cdn.visitors.now/v.js" ...>` du `<head>`
- Le charger dynamiquement dans le JS cookie-consent SEULEMENT si `preferences.statistics === true`
```js
if (preferences.statistics) {
  var s = document.createElement('script');
  s.src = 'https://cdn.visitors.now/v.js';
  s.setAttribute('data-token', 'a12454d2-5688-434c-90fc-20768462efd9');
  s.setAttribute('data-persist', '');
  document.head.appendChild(s);
}
```

### Étape 4 : Bouton "Gérer les cookies" dans le footer

Ajouter dans le footer de chaque page un lien "Gérer les cookies" qui rouvre la bannière :
```html
<a href="javascript:void(0)" onclick="openCookieSettings()">Gérer les cookies</a>
```

### Étape 5 : Mettre à jour la politique cookies

Fichier : `templates/legal/cookies.html`

Ajouter dans le tableau des cookies :

| Cookie | Type | Finalité | Durée | Catégorie |
|--------|------|----------|-------|-----------|
| `finova_cookie_consent` | localStorage | Stocke vos préférences de cookies | 365 jours | Essentiel |
| `visitor` | Cookie | Visitors Analytics - attribution des visiteurs | Persistant | Statistiques |
| `_ga`, `_ga_*` | Cookie | Google Analytics 4 - statistiques de navigation | 2 ans | Statistiques |
| Session Flask | Cookie | Maintien de la connexion utilisateur | Session | Essentiel |

Ajouter une section sur Visitors Analytics :
- Nom du service : Visitors (visitors.now)
- Finalité : analytics de trafic et attribution de revenus
- Données collectées : pages visitées, source de trafic, cookie visitor
- Durée : persistant
- Catégorie : Statistiques
- Opt-out : via le panneau de préférences cookies

### Étape 6 : Mettre à jour la politique de confidentialité

Fichier : `templates/legal/archives/` (créer nouvelle version)

Ajouter Visitors Analytics dans la liste des sous-traitants/outils :
- Visitors (visitors.now) - Analytics et attribution de revenus
- Données partagées : cookie visitor, metadata de session Stripe

### Étape 7 : Log serveur enrichi

Modifier l'endpoint `/auth/api/log-cookie-consent` pour accepter les préférences granulaires :
```json
{
  "preferences": { "essential": true, "statistics": true, "marketing": false },
  "cguAccepted": true,
  "privacyAccepted": true,
  "consentVersion": "1.0"
}
```

## Fichiers à modifier

| Fichier | Action |
|---------|--------|
| `marketing_site/static/js/cookie-consent.js` | Réécrire complètement |
| `marketing_site/static/css/marketing.css` | Ajouter styles nouvelle bannière |
| `marketing_site/templates/_cookie_consent.html` | Nouveau fichier (include) |
| `marketing_site/templates/index_v2.html` | Remplacer bannière + conditionner scripts |
| `marketing_site/templates/ads_v2.html` | Idem |
| `marketing_site/templates/tarifs.html` | Idem |
| `marketing_site/templates/fonctionnalites.html` | Idem |
| `marketing_site/templates/contact.html` | Idem |
| `marketing_site/templates/cas-usage.html` | Idem |
| `marketing_site/templates/guide.html` | Idem |
| `marketing_site/templates/guide_page.html` | Idem |
| `templates/legal/cookies.html` | Ajouter Visitors + tableau MAJ |
| `views/auth_views.py` | Enrichir endpoint log-cookie-consent |

## Ordre d'exécution

1. Créer `_cookie_consent.html` (bannière)
2. Réécrire `cookie-consent.js` (logique)
3. Ajouter styles dans `marketing.css`
4. Remplacer la bannière dans les 8 templates
5. Conditionner GTM + Visitors au consentement
6. Ajouter "Gérer les cookies" dans les footers
7. MAJ `templates/legal/cookies.html`
8. MAJ endpoint log serveur
9. Tester sur toutes les pages
10. Push

## Risques de régression

- GTM ne doit PLUS se charger sans consentement → impact sur les stats
- Visitors ne se charge pas sans consentement → le cookie `visitor` n'existe pas → metadata Stripe vide (acceptable)
- L'ancien `finovRelanceCookieConsent` dans localStorage des visiteurs existants doit être migré ou la bannière réapparaît (acceptable - on veut le nouveau consentement granulaire)
