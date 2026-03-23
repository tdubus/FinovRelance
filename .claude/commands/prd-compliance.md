---
name: prd-compliance
description: >
  Vérification de conformité fonctionnelle entre le PRD (Product Requirements Document) et
  l'application développée. Déclencher ce skill dès qu'un PRD est présent dans le projet et
  que l'utilisateur demande de vérifier la conformité, valider les fonctionnalités, faire un
  audit fonctionnel, ou s'assurer que le développement correspond aux specs. Agit comme un
  analyste fonctionnel senior qui audite méthodiquement chaque exigence, vérifie la logique
  métier, détecte les incohérences, les cas non gérés, et les fonctionnalités manquantes.
  Utiliser aussi quand l'utilisateur dit "vérifie si tout est bien développé" ou "est-ce qu'on
  a oublié quelque chose" ou "fais une revue de ce qu'on a livré".
---

# Skill — Vérification de Conformité PRD (Analyste Fonctionnel)

## Vue d'ensemble

Ce skill agit comme un **analyste fonctionnel senior**. Son rôle : s'assurer que chaque
fonctionnalité du PRD est implémentée correctement, logiquement, et complètement — en
tenant compte des cas limites et de l'expérience utilisateur réelle.

> ℹ️ **Analogie** : c'est l'équivalent d'un inspecteur de chantier qui vérifie que la
> maison construite correspond aux plans architecturaux, que rien n'a été oublié, et que
> ce qui a été fait est logique et utilisable.

**Toujours lire ce fichier en entier avant de commencer la vérification.**

---

## Étape 0 — Localisation et lecture du PRD

```bash
# Chercher le PRD dans le projet
find . -name "*.md" -o -name "*.pdf" -o -name "*.docx" | grep -i "prd\|spec\|requirements\|cahier"
ls docs/ 2>/dev/null
ls .docs/ 2>/dev/null
```

**Si PRD introuvable :**
Demander à l'utilisateur : "Je ne trouve pas de PRD dans le projet. Pouvez-vous me le partager
ou me préciser où il se trouve ?"

**Si PRD trouvé :**
Lire attentivement en entier avant toute analyse. Construire mentalement une liste de toutes
les exigences, organisées par module/fonctionnalité.

---

## Étape 1 — Cartographie des exigences

Avant d'analyser le code, extraire et structurer toutes les exigences du PRD :

```
Pour chaque fonctionnalité listée dans le PRD :
- Identifiant ou titre
- Comportement attendu (ce que ça doit faire)
- Acteur concerné (qui peut faire cette action)
- Règles métier spécifiques
- Cas limites mentionnés
- Interface attendue (si décrite)
```

Cette cartographie sera la référence pour l'audit.

---

## Catégories de vérification

### 1. CONFORMITÉ FONCTIONNELLE DE BASE

Pour chaque fonctionnalité du PRD, vérifier dans le code :

**Existence :**
- La fonctionnalité est-elle implémentée du tout ?
- Si absente : est-ce mentionné comme non commencé, ou a-t-elle été oubliée ?

**Complétude :**
- Tous les sous-cas décrits dans le PRD sont-ils couverts ?
- Les règles métier sont-elles toutes respectées ?
- Les contraintes décrites (formats, limites, validations) sont-elles appliquées ?

**Exemple de grille par fonctionnalité :**
```
Fonctionnalité : [Nom tel que décrit dans le PRD]
├── ✅ Implémentée ?          [oui/non/partiel]
├── ✅ Conforme au comportement attendu ?  [oui/non/partiel]
├── ✅ Tous les cas couverts ?  [oui/non/manquants: ...]
├── ✅ Règles métier respectées ?  [oui/non/écart: ...]
└── 🔍 Notes : [observations spécifiques]
```

---

### 2. LOGIQUE MÉTIER ET COHÉRENCE

**Règles de gestion :**
- Les calculs sont-ils corrects (ex. totaux, taxes, remises, cumuls) ?
- Les statuts et transitions d'état sont-ils corrects (ex. commande : brouillon → confirmée → expédiée → livrée) ?
- Les permissions sont-elles correctes (qui peut voir/modifier quoi) ?
- Les workflows d'approbation sont-ils dans le bon ordre ?
- Les dates de calcul (échéances, délais, durées) sont-elles gérées correctement ?

**Cohérence inter-fonctionnalités :**
- Deux fonctionnalités ne se contredisent-elles pas dans leur comportement ?
- Les données créées par une fonctionnalité sont-elles correctement utilisées par une autre ?
- Les modifications dans un module se propagent-elles correctement dans les autres modules liés ?

**Exemple de cas à vérifier pour une app comptable :**
```
- Si une facture est annulée, les montants dus sont-ils recalculés ?
- Si un client change de conditions de paiement, les factures futures en tiennent-elles compte ?
- Si un paiement partiel est enregistré, le solde restant est-il correct ?
```

---

### 3. CAS LIMITES ET SITUATIONS EXCEPTIONNELLES

> Les cas limites sont ceux qui arrivent rarement mais qui plantent tout quand ils ne sont pas gérés.

**Valeurs limites :**
- Que se passe-t-il si un champ est vide / null / 0 ?
- Que se passe-t-il avec une valeur très grande (ex. montant de 999,999,999 $) ?
- Que se passe-t-il avec une valeur négative là où seul le positif est attendu ?
- Que se passe-t-il avec des caractères spéciaux dans les champs texte ?
- Que se passe-t-il avec des chaînes très longues dépassant la limite DB ?

**Concurrence et synchronisation :**
- Que se passe-t-il si deux utilisateurs modifient le même enregistrement simultanément ?
- Que se passe-t-il si le même formulaire est soumis deux fois rapidement ?
- Que se passe-t-il si la connexion est coupée en milieu de transaction ?

**États intermédiaires :**
- Que se passe-t-il si on navigue en arrière dans un wizard/formulaire multi-étapes ?
- Que se passe-t-il si on ferme et rouvre le navigateur en milieu de processus ?
- Que se passe-t-il si un fichier uploadé est corrompu ou vide ?

**Données manquantes :**
- L'app gère-t-elle gracieusement les listes vides (écran vide ou message approprié) ?
- L'app gère-t-elle le cas où des données référencées ont été supprimées ?

---

### 4. VALIDATIONS ET MESSAGES D'ERREUR

**Validations côté serveur :**
- Tous les champs obligatoires sont-ils validés côté serveur (pas juste côté client) ?
- Les formats sont-ils validés (email, téléphone, code postal, IBAN, etc.) ?
- Les contraintes métier sont-elles validées (ex. date de fin > date de début) ?
- Les limites sont-elles validées (longueur maximale, valeur maximale) ?
- Les relations sont-elles validées (ex. un client doit exister avant de créer une commande) ?

**Messages d'erreur :**
- Les messages d'erreur sont-ils en français (ou dans la langue cible) et compréhensibles par un utilisateur non technique ?
- Les messages indiquent-ils clairement CE QUI ne va pas et COMMENT le corriger ?
- Les erreurs de validation par champ sont-elles affichées au bon endroit (à côté du champ) ?
- Un message d'erreur générique "quelque chose s'est mal passé" sans plus de détails est-il acceptable ?

**Validations côté client :**
- Les validations visuelles apparaissent-elles en temps réel ou seulement à la soumission ?
- Les champs obligatoires sont-ils clairement identifiés avant la soumission ?

---

### 5. EXPÉRIENCE UTILISATEUR ET LOGIQUE DE NAVIGATION

**Flux utilisateur :**
- L'ordre des étapes est-il logique pour l'utilisateur ?
- Y a-t-il des actions que l'utilisateur ne peut pas annuler alors qu'il le devrait (ou inversement) ?
- Les confirmations sont-elles demandées avant les actions irréversibles (suppression, envoi) ?
- L'utilisateur peut-il revenir en arrière sans perdre ses données ?

**États et feedback :**
- L'utilisateur est-il informé du résultat de ses actions (succès, erreur, en cours) ?
- Les états de chargement sont-ils indiqués (spinner, skeleton, message) ?
- Après une action réussie, l'utilisateur est-il redirigé au bon endroit ?
- Les listes se rafraîchissent-elles après une création/modification/suppression ?

**Cohérence d'interface :**
- Les libellés sont-ils cohérents entre le PRD et l'interface réelle (même terminologie) ?
- Les boutons d'action principaux sont-ils bien visibles et à l'endroit attendu ?
- Les raccourcis ou comportements attendus selon les conventions web sont-ils respectés (ex. Enter pour valider, Escape pour fermer) ?

---

### 6. DROITS D'ACCÈS ET RÔLES

**Modèle de permissions :**
- Tous les rôles décrits dans le PRD sont-ils implémentés ?
- Chaque rôle voit-il uniquement ce qu'il est censé voir ?
- Les menus et boutons sont-ils masqués pour les rôles qui n'ont pas l'accès (pas juste désactivés côté serveur) ?
- Un utilisateur sans les droits peut-il accéder directement à une URL protégée ?
- Les données multi-tenant sont-elles correctement isolées (un utilisateur ne voit pas les données d'un autre client) ?

**Héritage et délégation de droits :**
- Un administrateur peut-il effectuer toutes les actions d'un utilisateur standard ?
- Les droits délégués (ex. un manager peut agir au nom de son équipe) sont-ils correctement implémentés ?

---

### 7. INTÉGRATIONS ET DONNÉES EXTERNES

**APIs et services tiers :**
- Toutes les intégrations décrites dans le PRD sont-elles implémentées ?
- Les webhooks entrants sont-ils correctement traités (idempotence, validation de signature) ?
- Les données synchronisées depuis l'extérieur sont-elles correctement mappées ?

**Import / Export :**
- Les formats d'import décrits dans le PRD sont-ils tous supportés ?
- Les exports produisent-ils les bons champs, dans le bon ordre, avec le bon formatage ?
- Les fichiers exportés s'ouvrent-ils correctement dans Excel, si applicable ?
- Les imports gèrent-ils les erreurs ligne par ligne (rapport d'erreurs) ?

---

### 8. NOTIFICATIONS ET COMMUNICATIONS

**Emails et notifications :**
- Tous les emails décrits dans le PRD sont-ils envoyés au bon moment ?
- Les destinataires sont-ils les bons (utilisateur, admin, client, etc.) ?
- Le contenu de l'email correspond-il à ce qui était prévu ?
- Les notifications in-app apparaissent-elles au bon moment et disparaissent-elles une fois lues ?
- Les préférences de notification de l'utilisateur sont-elles respectées ?

---

### 9. RAPPORTS ET TABLEAUX DE BORD

**Données affichées :**
- Les métriques et KPIs sont-ils calculés correctement ?
- Les filtres de dates produisent-ils les bonnes données (fuseaux horaires, inclusivité des bornes) ?
- Les agrégations (totaux, moyennes, comptages) sont-elles correctes ?
- Les graphiques reflètent-ils fidèlement les données (bonne échelle, bonne légende) ?

**Cas particuliers dans les rapports :**
- Que se passe-t-il si la période sélectionnée ne contient aucune donnée ?
- Les rapports avec beaucoup de données sont-ils paginés ou exportables ?
- Les données affichées tiennent-elles compte des permissions de l'utilisateur connecté ?

---

### 10. DONNÉES DE RÉFÉRENCE ET CONFIGURATION

**Paramétrage :**
- Toutes les données de configuration décrites dans le PRD sont-elles modifiables par les admins ?
- Les valeurs par défaut sont-elles correctes ?
- Les modifications de configuration prennent-elles effet immédiatement ou après rechargement ?

**Données de référence :**
- Les listes déroulantes contiennent-elles les bonnes valeurs ?
- Les données de référence sont-elles modifiables si le PRD le prévoit ?
- L'impact de la modification d'une donnée de référence sur les données existantes est-il géré ?

---

## Format du rapport de conformité PRD

```
## Rapport de Conformité PRD — [Nom du projet] — [Date]

### Résumé de conformité
- Fonctionnalités dans le PRD : X
- Fonctionnalités implémentées complètement : X (XX%)
- Fonctionnalités partiellement implémentées : X
- Fonctionnalités manquantes : X

---

### Fonctionnalités — Statut détaillé

#### ✅ [Nom Fonctionnalité] — CONFORME
[Description courte de ce qui a été vérifié]

#### ⚠️ [Nom Fonctionnalité] — PARTIELLE
[Ce qui est fait / Ce qui manque / Écarts par rapport au PRD]

#### ❌ [Nom Fonctionnalité] — MANQUANTE ou NON CONFORME
[Description du problème / Impact / Recommandation]

---

### Problèmes fonctionnels détectés (hors PRD)
[Comportements illogiques ou bugs fonctionnels non mentionnés dans le PRD]

### Cas limites non gérés
[Liste des situations non couvertes qui pourraient causer des problèmes]

### Recommandations
[Ordre de priorité pour compléter/corriger]
```

---

## Règles de comportement du skill

1. **Rester factuel** : se baser uniquement sur ce qui est dans le PRD et ce qui est dans le code
2. **Distinguer "non conforme au PRD" et "amélioration suggérée"** : ne pas mélanger les deux
3. **Être l'avocat de l'utilisateur final** : demander "est-ce que ça aurait du sens pour quelqu'un qui n'a jamais vu l'app ?"
4. **Signaler les incohérences dans le PRD lui-même** : si le PRD est contradictoire ou flou sur un point
5. **Proposer des solutions** : ne pas juste pointer les problèmes, suggérer comment les régler
6. **Adapter le rapport** : un PRD de 5 pages → rapport concis. Un PRD de 50 pages → rapport exhaustif
7. **Vulgariser** : écrire pour que le chef de projet non-technique comprenne exactement ce qui manque
