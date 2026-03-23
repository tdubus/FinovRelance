#!/bin/bash

# Script de configuration du cron job pour les changements différés Stripe
# À exécuter une fois pour configurer le cron quotidien

echo "Configuration du cron job pour les changements différés Stripe..."

# Créer le secret si nécessaire
if [ -z "$REPL_CRON_SECRET" ]; then
    REPL_CRON_SECRET=$(openssl rand -hex 32)
    echo "REPL_CRON_SECRET=$REPL_CRON_SECRET" >> .env
    echo "✅ Secret créé : REPL_CRON_SECRET"
else
    echo "✅ Secret existant utilisé"
fi

# URL du webhook (à ajuster selon le déploiement)
WEBHOOK_URL="https://workspace--tdubus.repl.co/jobs/apply_pending_changes"

# Créer le script cron
cat > /tmp/apply_pending_changes_cron.sh << 'EOF'
#!/bin/bash
# Script exécuté par le cron pour appliquer les changements différés

WEBHOOK_URL="${WEBHOOK_URL:-https://workspace--tdubus.repl.co/jobs/apply_pending_changes}"
REPL_CRON_SECRET="${REPL_CRON_SECRET}"

# Appeler le webhook avec le token de sécurité
curl -X POST "$WEBHOOK_URL" \
    -H "X-Job-Token: $REPL_CRON_SECRET" \
    -H "Content-Type: application/json" \
    --silent \
    --show-error \
    --max-time 30
EOF

chmod +x /tmp/apply_pending_changes_cron.sh

# Configurer le cron (3h00 du matin, heure locale)
CRON_TIME="0 3 * * *"  # Tous les jours à 3h00

# Ajouter au crontab (si pas déjà présent)
(crontab -l 2>/dev/null | grep -v "apply_pending_changes_cron.sh"; echo "$CRON_TIME /tmp/apply_pending_changes_cron.sh >> /tmp/cron_apply_pending_changes.log 2>&1") | crontab -

echo "✅ Cron job configuré pour s'exécuter tous les jours à 3h00"
echo ""
echo "Pour tester manuellement le cron job :"
echo "curl -X POST $WEBHOOK_URL -H \"X-Job-Token: $REPL_CRON_SECRET\""
echo ""
echo "Pour voir les logs du cron :"
echo "tail -f /tmp/cron_apply_pending_changes.log"
echo ""
echo "Configuration terminée !"