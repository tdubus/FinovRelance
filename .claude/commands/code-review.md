---
name: code-review
description: >
  Révision de code ultra-complète pour projets SaaS et applications web. Déclencher ce skill
  dès que l'utilisateur demande une révision de code, un audit de qualité, une vérification
  avant déploiement, ou mentionne des problèmes de performance, de lisibilité ou de maintenabilité.
  Couvre : code mort, doublons, imports inutilisés, fonctions inutilisées, complexité excessive,
  dette technique, conventions de nommage, structure de fichiers, couplage, cohésion, et toutes
  les bonnes pratiques de développement professionnel. Utiliser aussi quand l'utilisateur dit
  "vérifie mon code", "analyse le projet", "qu'est-ce qui ne va pas dans ce code", ou "clean up".
---

# Skill — Code Review Ultra-Complet

## Vue d'ensemble

Ce skill effectue une révision de code exhaustive, structurée en catégories. L'objectif est
de détecter **tout ce qui nuit à la qualité, maintenabilité, lisibilité et robustesse** du code —
avant même de parler de sécurité ou de performance (couverts par d'autres skills).

**Toujours lire ce fichier en entier avant de commencer la révision.**

---

## Étape 0 — Collecte du contexte

Avant de commencer, demander si nécessaire :

1. **Périmètre** : tout le projet, un module spécifique, ou un fichier ?
2. **Stack** : langages, frameworks, runtime (ex. Node.js/Express, Python/FastAPI, React, etc.)
3. **Niveau de sévérité souhaité** : audit complet vs revue rapide ?
4. **Contraintes** : y a-t-il des dépendances imposées, un style de code existant à respecter ?

Si le projet est dans le contexte actuel (fichiers partagés ou répertoire connu), commencer
directement sans demander.

---

## Étape 1 — Exploration de la structure

```bash
# Cartographier la structure du projet
find . -type f \( -name "*.js" -o -name "*.ts" -o -name "*.py" -o -name "*.jsx" -o -name "*.tsx" \) \
  | grep -v node_modules | grep -v .git | grep -v dist | grep -v build

# Compter les lignes par fichier (détecter les fichiers trop longs)
wc -l $(find . -type f -name "*.js" | grep -v node_modules) | sort -rn | head -30

# Voir les dépendances déclarées
cat package.json 2>/dev/null || cat requirements.txt 2>/dev/null || cat Pipfile 2>/dev/null
```

---

## Catégories de révision

### 1. CODE MORT ET INUTILISÉ

**Ce qu'on cherche :**
- Fonctions déclarées mais jamais appelées nulle part
- Variables déclarées mais jamais lues
- Paramètres de fonctions jamais utilisés dans le corps
- Blocs `if (false)` ou conditions toujours vraies/fausses (code inatteignable)
- Branches `else` jamais atteintes
- Fichiers entiers jamais importés ou référencés
- Classes ou méthodes de classe jamais instanciées/appelées
- Constantes jamais utilisées
- Commentaires `// TODO` ou `// FIXME` oubliés depuis longtemps
- Code commenté laissé en place (doit être supprimé ou mis en issue tracker)
- Migrations de base de données obsolètes ou dupliquées

**Outils de détection :**
```bash
# JS/TS : imports non utilisés
npx eslint --rule '{"no-unused-vars": "error"}' .

# Python : imports non utilisés
python -m pyflakes . 2>&1 | grep "imported but unused"

# Recherche de fonctions définies mais jamais appelées (approche manuelle)
grep -rn "function " src/ | awk '{print $2}' > defined_functions.txt
```

---

### 2. IMPORTS ET DÉPENDANCES

**Ce qu'on cherche :**
- Imports déclarés en haut du fichier mais jamais utilisés
- Import de toute une librairie quand on n'utilise qu'une seule fonction (`import _ from 'lodash'` vs `import { debounce } from 'lodash'`)
- Dépendances dans `package.json` / `requirements.txt` jamais importées dans le code (dépendances fantômes)
- Dépendances dupliquées avec des noms différents (ex. `moment` et `dayjs` pour la même chose)
- Import circulaires (A importe B qui importe A)
- Chemins d'import incohérents (parfois relatif `../../utils`, parfois alias `@/utils`)
- Versions de dépendances non fixées (`^`, `~`, `*`) dans un contexte de production

**Vérification :**
```bash
# Packages installés mais non utilisés
npx depcheck

# Vérifier les imports circulaires
npx madge --circular src/
```

---

### 3. DOUBLONS ET REDONDANCES

**Ce qu'on cherche :**
- Fonctions qui font exactement la même chose mais nommées différemment
- Blocs de code copié-collé identiques ou quasi-identiques (> 5 lignes répétées)
- Constantes avec la même valeur définie à plusieurs endroits
- Même appel API ou même requête DB faite dans plusieurs composants sans abstraction
- Configuration dupliquée (ex. URL de base définie dans 3 fichiers différents)
- Logique métier dupliquée entre frontend et backend sans partage
- Styles CSS ou classes Tailwind identiques appliquées à plusieurs endroits sans composant partagé

**Détection :**
```bash
# Détecter du code dupliqué
npx jscpd src/ --min-lines 5 --min-tokens 50

# Python
pip install pylint && pylint --disable=all --enable=duplicate-code src/
```

---

### 4. COMPLEXITÉ ET LISIBILITÉ

**Ce qu'on cherche :**

**Complexité cyclomatique** (trop de chemins d'exécution dans une fonction) :
- Fonctions avec > 10 `if/else/switch/for/while` imbriqués
- Fonctions de plus de 50 lignes (signal qu'elle fait trop de choses)
- Fichiers de plus de 300 lignes (signal qu'il faut découper)
- Imbrication trop profonde (> 3-4 niveaux d'indentation)

**Lisibilité :**
- Noms de variables trop courts ou non descriptifs (`x`, `temp`, `data`, `res`, `obj`)
- Noms de fonctions trompeurs (fonction nommée `getUser` qui fait aussi un update)
- Magic numbers sans constante nommée (`if (status === 3)` → que veut dire 3 ?)
- Magic strings répétées (`if (role === 'admin')` partout sans constante)
- Commentaires obsolètes qui décrivent quelque chose d'autre que ce que fait le code
- Logique négative difficile à lire (`if (!isNotValid)`)
- Ternaires imbriqués illisibles

**Promesses et async :**
- Mélange de callbacks, Promises et async/await dans le même projet
- `async` sur une fonction qui n'a aucun `await` à l'intérieur
- `await` sur quelque chose qui n'est pas une Promise
- Promesses non catchées (floating promises)

---

### 5. STRUCTURE ET ARCHITECTURE

**Ce qu'on cherche :**
- **Couplage fort** : un module qui importe directement depuis 15 autres modules
- **Cohésion faible** : un fichier qui contient des fonctions sans lien logique entre elles
- **Violation de la séparation des responsabilités** : logique métier dans les contrôleurs HTTP, ou requêtes DB dans les composants React
- **Dépendance inversée non respectée** : modules de haut niveau dépendant de détails d'implémentation
- **God object** : une classe ou un module qui fait tout
- **Absence de couche service** : toute la logique directement dans les routes/controllers
- **Fichiers index.js / barrel exports** mal structurés créant des imports circulaires
- **Structure de dossiers incohérente** avec le reste du projet

---

### 6. GESTION DES ERREURS

**Ce qu'on cherche :**
- `try/catch` vide (l'erreur est avalée silencieusement)
- `catch(e) { console.log(e) }` sans ré-émission ni gestion réelle
- Absence totale de gestion d'erreur sur des appels réseau ou DB
- Erreurs retournées à l'utilisateur avec des stack traces complètes (risque sécurité)
- Codes HTTP incorrects (retourner 200 pour une erreur, 500 pour une validation)
- Pas de gestion des cas `null` / `undefined` avant utilisation
- Optional chaining `?.` absent là où une valeur peut être nulle
- Absence de valeurs par défaut pour les paramètres optionnels

---

### 7. CONVENTIONS ET COHÉRENCE

**Ce qu'on cherche :**
- Mélange de styles de nommage (`camelCase` vs `snake_case` vs `PascalCase` dans le même contexte)
- Incohérence dans la structure des retours de fonctions (parfois objet, parfois tableau, parfois valeur brute)
- Fichiers JS avec extension `.js` et `.ts` mélangés sans raison
- Quotes simples vs doubles incohérentes
- Semicolons présents ou absents de façon incohérente
- Indentation mixte (tabs et espaces)
- Absence de `.editorconfig` ou `.eslintrc` (pas de règles formalisées)
- Absence de Prettier ou formatage automatique

---

### 8. TESTS

**Ce qu'on cherche :**
- Absence totale de tests
- Tests qui ne testent rien (assertions toujours vraies)
- Tests trop dépendants de l'implémentation (fragiles aux refactorisations)
- Couverture de code < 60% sur la logique métier critique
- Absence de tests pour les cas limites (valeurs nulles, chaînes vides, tableaux vides)
- Tests qui appellent de vraies APIs externes (doivent être mockées)
- Tests qui dépendent d'un ordre d'exécution spécifique
- Fichiers de test avec du code de production réel copié dedans

---

### 9. CONFIGURATION ET ENVIRONNEMENT

**Ce qu'on cherche :**
- Variables d'environnement hardcodées dans le code (URLs, clés, mots de passe)
- Fichier `.env` commité dans le repo (catastrophe sécurité)
- Absence de fichier `.env.example` pour documenter les variables nécessaires
- Différences non documentées entre config dev / staging / production
- `console.log` de debug laissés en production
- Flags de feature non nettoyés après déploiement
- Configuration répétée dans plusieurs fichiers au lieu d'un fichier centralisé

---

### 10. DÉPENDANCES ET MAINTENANCE

**Ce qu'on cherche :**
- Packages avec des vulnérabilités connues (`npm audit` / `pip-audit`)
- Packages très anciens avec des alternatives modernes mieux maintenues
- Packages abandonnés (dernier commit > 2 ans, pas de réponse aux issues)
- `node_modules` ou `venv` commités dans le repo
- `package-lock.json` ou `yarn.lock` absent (builds non reproductibles)
- Utilisation de fonctions dépréciées par les librairies

---

## Format du rapport de révision

Structurer le rapport ainsi :

```
## Rapport de Code Review — [Nom du projet] — [Date]

### Résumé exécutif
[2-3 phrases sur l'état général du code et les priorités]

### 🔴 Critique (à corriger avant tout déploiement)
[Liste avec fichier:ligne et description]

### 🟡 Important (à corriger dans le prochain sprint)
[Liste avec fichier:ligne et description]

### 🟢 Améliorations recommandées (dette technique)
[Liste avec fichier:ligne et description]

### ✅ Points positifs
[Ce qui est bien fait]

### Plan d'action suggéré
[Ordre de priorité avec effort estimé]
```

---

## Règles de comportement du skill

1. **Toujours donner des exemples concrets** : nommer le fichier, la ligne, la fonction concernée
2. **Proposer la correction** : ne pas juste signaler le problème, montrer comment le corriger
3. **Calibrer selon le contexte** : une startup early-stage et une app en production n'ont pas les mêmes standards
4. **Ne pas être dogmatique** : certaines "violations" sont des choix délibérés valides — demander si le doute existe
5. **Prioriser** : toujours indiquer ce qui bloque vs ce qui est cosmétique
6. **Adapter le vocabulaire** : expliquer les termes techniques de façon claire pour un non-développeur
