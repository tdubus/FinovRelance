# INVENTAIRE DES CONFLITS ET CODE MORT

## 🔴 HANDLER CRITIQUE MANQUANT

### invoice.payment_failed
**État actuel** : Seulement un log, pas d'implémentation complète
```python
# Ligne 202-207 dans main_handler.py
elif event_type == 'invoice.payment_failed':
    current_app.logger.warning(f"⚠️ Échec de paiement: {event.get('data', {}).get('object', {}).get('id')}")
    # URGENT : Implémenter handler complet ici
    webhook_action.status = WebhookActionStatus.IGNORED
    webhook_action.response_message = "Payment failed - handler not implemented"
```

**Implémentation requise** :
1. Identifier l'entreprise via subscription_id ou customer_id
2. Marquer company.subscription_status = 'past_due'
3. Calculer et appliquer période de grâce
4. Envoyer notifications urgentes (email + in-app)
5. Logger dans audit trail avec détails complets
6. Déclencher relances automatiques si configurées

## 🟡 DUPLICATIONS IDENTIFIÉES

### 1. Routes webhook multiples
- `/stripe/webhook` (stripe_routes.py - ancien)
- `/stripe/v2/webhook` (stripe_webhook_v2.py - actuel)
- `/admin/webhook/test` (admin_webhook_routes.py - test)

### 2. Handlers dupliqués
- `handle_subscription_updated()` : 2 versions
- `handle_payment_succeeded()` : 2 versions  
- `process_credits_purchase_webhook()` : 2 locations

### 3. Imports redondants
- stripe importé 5 fois dans différents fichiers
- db importé dans chaque handler séparément
- models importé multiple fois

## 🟢 CODE MORT À SUPPRIMER

### Fichiers obsolètes
1. webhook_handlers/subscription_handlers.py (remplacé par v2)
2. admin_webhook_views.py.backup
3. test_deletion_script.py (test temporaire)

### Code non utilisé
1. Fonctions de test dans stripe_routes.py
2. Handlers legacy commentés
3. Imports non utilisés (environ 25 imports)

## 📊 MÉTRIQUES DE QUALITÉ

| Métrique | Valeur Actuelle | Cible Phase 2 |
|----------|----------------|---------------|
| Fichiers webhook | 8 | 4 |
| Lignes de code | ~2500 | ~1500 |
| Duplication | 30% | 0% |
| Couverture événements | 11/12 (91.6%) | 12/12 (100%) |
| Handlers manquants | 1 | 0 |
| Tests unitaires | 0% | 90% |

## ✅ ACTIONS CORRECTIVES PHASE 2

1. **Unification** : Créer stripe/webhooks/unified.py comme point d'entrée unique
2. **Élimination duplications** : Un seul handler par événement
3. **Handler manquant** : Implémenter invoice.payment_failed complètement
4. **Nettoyage** : Supprimer tout code mort identifié
5. **Tests** : Ajouter tests unitaires pour chaque handler