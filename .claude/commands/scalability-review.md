---
name: scalability-review
description: >
  Audit de scalabilité et performance pour applications web et SaaS. Déclencher ce skill
  dès que l'utilisateur mentionne performance, scalabilité, montée en charge, lenteur,
  optimisation, base de données lente, requêtes SQL lentes, caching, indexation, mémoire,
  temps de réponse, ou prépare une app pour gérer plus d'utilisateurs ou de données.
  Couvre : architecture, base de données, requêtes SQL, caching, file d'attente, ressources serveur,
  frontend, CDN, et tous les points de congestion d'une application à fort volume.
  Utiliser aussi quand l'utilisateur demande "est-ce que mon app peut gérer X utilisateurs".
---

# Skill — Scalabilité et Performance Ultra-Complet

## Vue d'ensemble

Ce skill audite la capacité d'une application à **croître sans se dégrader** — en termes
de volume de données, d'utilisateurs simultanés, et de requêtes par seconde.

> ℹ️ **Scalabilité** = capacité à gérer 10x, 100x plus de charge sans réécrire l'app.
> **Performance** = vitesse de réponse à charge normale.
> Les deux sont liés mais distincts.

**Toujours lire ce fichier en entier avant de commencer l'audit.**

---

## Étape 0 — Collecte du contexte

Demander si nécessaire :
1. **Base de données** : PostgreSQL, MySQL, MongoDB, Redis, SQLite ?
2. **Volume actuel** : combien d'utilisateurs, de lignes en DB, de requêtes/jour ?
3. **Volume cible** : quel objectif de croissance (10x, 100x) ?
4. **Stack** : framework backend, ORM utilisé, hébergement ?
5. **Points de douleur identifiés** : y a-t-il des lenteurs déjà observées ?

---

## Catégories d'audit

### 1. BASE DE DONNÉES — INDEXATION

> Un index = comme un index dans un livre. Sans lui, la DB lit toute la table pour trouver une ligne.

**Ce qu'on cherche :**

**Index manquants :**
- Toutes les colonnes utilisées dans des clauses `WHERE` fréquentes ont-elles un index ?
- Toutes les colonnes utilisées dans des `JOIN` ont-elles un index ?
- Colonnes de tri fréquentes (`ORDER BY`) indexées ?
- Clés étrangères (foreign keys) toujours indexées ?
- Colonnes de filtrage dans les listes paginées indexées ?

**Index mal conçus :**
- Index sur une colonne booléenne seule (peu d'utilité — trop peu de valeurs distinctes) ?
- Index sur des colonnes trop souvent modifiées (coût d'écriture élevé) ?
- Index composites dans le bon ordre (la colonne la plus sélective en premier) ?
- Index non utilisés (à supprimer — ils ralentissent les écritures) ?

**Vérification :**
```sql
-- PostgreSQL : trouver les requêtes lentes
SELECT query, mean_exec_time, calls
FROM pg_stat_statements
ORDER BY mean_exec_time DESC LIMIT 20;

-- Trouver les index manquants suggérés
SELECT * FROM pg_stat_user_tables WHERE seq_scan > 0 ORDER BY seq_scan DESC;

-- Analyser une requête spécifique
EXPLAIN ANALYZE SELECT * FROM orders WHERE user_id = 123 AND status = 'pending';
-- Chercher "Seq Scan" → potentiel index manquant
-- Chercher "Index Scan" → bien, l'index est utilisé
```

---

### 2. BASE DE DONNÉES — REQUÊTES SQL

**Requêtes N+1 (problème le plus fréquent) :**
> N+1 = faire 1 requête pour obtenir une liste, puis N requêtes pour les détails de chaque élément
```js
// ❌ N+1 : 1 requête pour les commandes + 1 par commande pour l'utilisateur
const orders = await Order.findAll()
for (const order of orders) {
  order.user = await User.findById(order.userId) // 💀 N requêtes
}

// ✅ 1 seule requête avec JOIN ou eager loading
const orders = await Order.findAll({ include: [{ model: User }] })
```

**Requêtes problématiques :**
- `SELECT *` au lieu de sélectionner uniquement les colonnes nécessaires ?
- Requêtes sans `LIMIT` sur des tables qui vont grandir ?
- Sous-requêtes non corrélées pouvant être remplacées par des JOIN ?
- `LIKE '%terme%'` (scan complet de table) sur des colonnes sans full-text index ?
- Fonctions appliquées sur des colonnes indexées dans le WHERE (annule l'index) :
  ```sql
  -- ❌ L'index sur created_at n'est pas utilisé
  WHERE YEAR(created_at) = 2024
  -- ✅ L'index est utilisé
  WHERE created_at BETWEEN '2024-01-01' AND '2024-12-31'
  ```
- Transactions trop longues qui bloquent la table ?
- Requêtes dans des boucles (toujours préférer les opérations batch) ?
- `COUNT(*)` sur de grandes tables sans condition (très lent) ?

**Pagination :**
- `OFFSET` élevé sur de grandes tables (lent — la DB lit et ignore les premières lignes) ?
  ```sql
  -- ❌ Lent à page 1000 : lit 100,000 lignes pour en retourner 100
  SELECT * FROM items ORDER BY id LIMIT 100 OFFSET 99900
  -- ✅ Keyset pagination (cursor-based)
  SELECT * FROM items WHERE id > :last_id ORDER BY id LIMIT 100
  ```

---

### 3. BASE DE DONNÉES — ARCHITECTURE

**Structure des tables :**
- Tables avec trop de colonnes (> 50 colonnes) → candidat à la décomposition ?
- Données JSON/blob stockées dans une colonne alors qu'elles sont filtrées/triées ?
- Données archivables dans la même table que les données actives (table qui grossit indéfiniment) ?
- Soft delete (`deleted_at`) sans index partiel sur les lignes non supprimées ?

**Connexions DB :**
- Connection pooling configuré (ne pas ouvrir une nouvelle connexion par requête) ?
- Taille du pool adaptée au nombre de workers du serveur ?
- Timeout sur les connexions inactives configuré ?

**Transactions :**
- Transactions maintenues ouvertes pendant des opérations longues (I/O, appels API) ?
- Verrous (locks) trop larges ou trop longs ?

---

### 4. CACHING (MISE EN CACHE)

> Le cache = stocker le résultat d'une opération coûteuse pour ne pas la refaire

**Ce qui devrait être mis en cache :**
- Résultats de requêtes DB coûteuses et peu changeantes ?
- Réponses d'APIs externes (météo, taux de change, etc.) ?
- Calculs lourds (agrégations, statistiques) ?
- Sessions utilisateur (Redis vs DB) ?
- Rendu de pages ou fragments HTML ?

**Stratégies de cache :**
- Cache-aside : l'app vérifie le cache → si absent, requête DB → stocke en cache
- Write-through : écriture simultanée en cache et en DB
- TTL (Time To Live) défini et adapté à la fraîcheur nécessaire des données ?
- Stratégie d'invalidation du cache lors d'une modification ?

**Implémentation :**
- Redis ou Memcached en place pour les données fréquemment lues ?
- Cache HTTP (headers `Cache-Control`, `ETag`, `Last-Modified`) sur les réponses API stables ?
- Memoization des fonctions de calcul intensif ?
- CDN (Cloudflare, CloudFront) pour les assets statiques ?

**Problèmes de cache :**
- Cache stampede : tous les caches expirent en même temps → pic de requêtes DB ?
  (Solution : jitter sur le TTL, ou lock sur la régénération)
- Cache poisoning : données corrompues en cache ?
- Mémoire cache illimitée → fuite mémoire éventuelle ?

---

### 5. ARCHITECTURE ET TRAITEMENT ASYNCHRONE

**Opérations synchrones qui devraient être asynchrones :**
- Envoi d'emails dans la requête HTTP (ralentit la réponse) ?
- Génération de PDF ou traitement d'images dans la requête HTTP ?
- Appels à des APIs externes lentes dans le chemin critique ?
- Notifications (Slack, webhooks) dans la requête HTTP ?
- Imports de fichiers CSV traités de façon synchrone ?

**Files de messages (Queue) :**
- Queue en place pour les tâches longues (BullMQ, RabbitMQ, SQS, Celery) ?
- Jobs qui échouent : retry avec backoff exponentiel configuré ?
- Dead letter queue pour les jobs qui échouent définitivement ?
- Monitoring des queues en place ?

**Concurrence :**
- Race conditions possibles lors d'accès simultanés aux mêmes ressources ?
- Opérations "lire puis écrire" atomiques (transactions ou locks) ?
  ```js
  // ❌ Race condition : deux users peuvent passer simultanément
  const stock = await getStock(itemId)  // stock = 1
  if (stock > 0) {
    await decrementStock(itemId)  // les deux decrementent → stock = -1
  }
  // ✅ Atomique avec UPDATE ... RETURNING
  UPDATE items SET stock = stock - 1 WHERE id = $1 AND stock > 0 RETURNING stock
  ```

---

### 6. RESSOURCES SERVEUR ET CODE

**Gestion de la mémoire :**
- Chargement de grands ensembles de données entiers en mémoire (ex. 100k lignes) ?
  (Utiliser des curseurs ou streams à la place)
- Objets volumineux gardés en mémoire sans être libérés ?
- Fuites mémoire : listeners d'événements jamais retirés ?
- Résultats de requêtes DB non limités (`findAll()` sans `limit`) ?

**CPU :**
- Calculs CPU-intensifs dans le thread principal Node.js (bloque tout) ?
  (Worker threads ou délégation à un service séparé)
- Expressions régulières catastrophiques sur du texte long ?
- Algorithmes O(n²) ou pire sur de grands datasets ?

**I/O (entrées/sorties) :**
- Opérations fichiers synchrones (`fs.readFileSync`) dans des routes HTTP ?
- Appels API séquentiels qui pourraient être parallèles ?
  ```js
  // ❌ Séquentiel : 3 × 200ms = 600ms
  const user = await getUser(id)
  const orders = await getOrders(id)
  const stats = await getStats(id)
  
  // ✅ Parallèle : max(200ms, 200ms, 200ms) = 200ms
  const [user, orders, stats] = await Promise.all([
    getUser(id), getOrders(id), getStats(id)
  ])
  ```

---

### 7. API ET RÉSEAU

**Pagination et limites :**
- Toutes les routes qui retournent des listes sont-elles paginées ?
- Limite maximale par page appliquée côté serveur (pas de `limit=999999`) ?
- Pagination cursor-based pour les très grandes collections ?

**Optimisation des payloads :**
- Compression gzip/brotli activée sur le serveur ?
- Seulement les champs nécessaires retournés par l'API ?
- Réponses API très larges qui pourraient être fragmentées ?

**GraphQL spécifique :**
- Protection contre les requêtes trop profondes (depth limiting) ?
- Requêtes GraphQL coûteuses limitées (query complexity) ?
- DataLoader ou équivalent pour éviter les N+1 en GraphQL ?

**Rate Limiting :**
- Rate limiting par utilisateur/IP sur toutes les routes (pas juste le login) ?
- Routes d'export ou de téléchargement particulièrement protégées ?

---

### 8. FRONTEND ET ASSETS

**Bundle JavaScript :**
- Taille du bundle JS analysée (objectif : < 200KB initial) ?
- Code splitting en place (lazy loading des routes) ?
- Tree shaking activé (supprime le code non utilisé des librairies) ?
- Librairies lourdes avec alternatives légères (ex. moment.js → date-fns) ?

**Images :**
- Images en format WebP ou AVIF (plus léger que PNG/JPEG) ?
- Images dimensionnées correctement (pas afficher une image 4000px pour 400px) ?
- Lazy loading des images (`loading="lazy"`) ?
- CDN pour les assets statiques ?

**Rendu :**
- Trop de re-rendus React inutiles (memo, useMemo, useCallback) ?
- Listes longues rendues sans virtualisation (react-window, react-virtual) ?
- Appels API refaits à chaque render au lieu d'être mis en cache (React Query, SWR) ?

---

### 9. SCALABILITÉ HORIZONTALE

> Scalabilité horizontale = ajouter des serveurs au lieu de rendre un serveur plus puissant

**Compatibilité multi-instance :**
- L'app peut-elle tourner sur plusieurs serveurs simultanément ?
- État de session stocké en DB ou Redis (pas en mémoire locale du serveur) ?
- Uploads de fichiers vers un stockage centralisé (S3) et non le disque local ?
- Tâches planifiées (cron) fonctionnant sur multi-instance sans duplication ?
- WebSockets ou SSE : adapter (Redis Pub/Sub) ?

**Stateless :**
- L'app est-elle "stateless" (sans état local entre requêtes) ?
- Aucune variable globale modifiée par les requêtes HTTP ?

---

### 10. MONITORING ET OBSERVABILITÉ

> Sans monitoring, on découvre les problèmes de performance en production. Trop tard.

- APM (Application Performance Monitoring) en place ? (Datadog, New Relic, Sentry Performance)
- Métriques DB exposées (slow query log, nombre de connexions actives) ?
- Alertes sur les temps de réponse > seuil ?
- Logs structurés (JSON) pour faciliter l'analyse ?
- Distributed tracing si microservices ?
- Health check endpoint avec informations de charge ?

---

## Format du rapport de scalabilité

```
## Rapport de Scalabilité — [Nom du projet] — [Date]

### Évaluation de capacité actuelle
[Estimation du volume gérable avec l'architecture actuelle]

### 🔴 Bottlenecks Critiques (bloqueront la croissance rapidement)
[Problème | Impact sur la performance | Correction]

### 🟡 Optimisations Importantes (nécessaires avant 10x croissance)
[Problème | Impact | Correction]

### 🟢 Améliorations pour la scalabilité long terme (avant 100x)
[Problème | Impact | Correction]

### ✅ Bonnes pratiques déjà en place

### Roadmap de scalabilité suggérée
[Phase 1 (maintenant) → Phase 2 (à 10x) → Phase 3 (à 100x)]
```

---

## Règles de comportement du skill

1. **Quantifier si possible** : "cette requête prend 3s à 10k lignes, 30s à 100k lignes"
2. **Prioriser par impact réel** : l'optimisation prématurée est l'ennemi — prioriser ce qui compte
3. **Expliquer les concepts** : N+1, index, cache — toujours vulgariser pour un non-développeur
4. **Proposer la correction concrète** : pas juste "ajouter un index", mais lequel, sur quelle colonne
5. **Distinguer optimisation nécessaire vs prématurée** : certaines choses ne sont pas urgentes à 100 utilisateurs
