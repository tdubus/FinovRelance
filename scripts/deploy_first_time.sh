#!/bin/bash
set -e

# =============================================================
#  FinovRelance - Deploiement initial (migration depuis Neon)
#  A executer UNE SEULE FOIS sur le VPS apres le premier deploy
# =============================================================

echo ""
echo "============================================"
echo "  FinovRelance - Migration initiale"
echo "============================================"
echo ""

# Verifications
if [ -z "$DATABASE_URL_SOURCE" ]; then
    echo "ERREUR : Variable DATABASE_URL_SOURCE non definie."
    echo "C'est l'URL de ta base Neon de production."
    echo ""
    echo "Usage :"
    echo "  export DATABASE_URL_SOURCE='postgresql://user:pass@neon-host:5432/finovrelance'"
    echo "  export DATABASE_URL_TARGET='postgresql://user:pass@localhost:5432/finovrelance'"
    echo "  bash scripts/deploy_first_time.sh"
    exit 1
fi

if [ -z "$DATABASE_URL_TARGET" ]; then
    echo "ERREUR : Variable DATABASE_URL_TARGET non definie."
    echo "C'est l'URL de ta nouvelle base PostgreSQL sur le VPS."
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="/tmp/finovrelance_migration_${TIMESTAMP}.sql"

echo "1/4 - Dump de la base de production Neon..."
pg_dump "$DATABASE_URL_SOURCE" \
    --no-owner \
    --no-acl \
    --clean \
    --if-exists \
    -f "$DUMP_FILE"
echo "     Dump OK : $DUMP_FILE"

echo ""
echo "2/4 - Restauration dans la base cible..."
psql "$DATABASE_URL_TARGET" -f "$DUMP_FILE"
echo "     Restauration OK"

echo ""
echo "3/4 - Verification..."
TABLE_COUNT=$(psql "$DATABASE_URL_TARGET" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';")
echo "     Tables dans la cible : $TABLE_COUNT"

USER_COUNT=$(psql "$DATABASE_URL_TARGET" -t -c "SELECT COUNT(*) FROM \"user\";" 2>/dev/null || echo "N/A")
echo "     Utilisateurs migres : $USER_COUNT"

echo ""
echo "4/4 - Nettoyage..."
rm -f "$DUMP_FILE"
echo "     Dump supprime."

echo ""
echo "============================================"
echo "  MIGRATION TERMINEE"
echo "============================================"
echo ""
echo "Prochaines etapes :"
echo "  1. Teste l'application sur https://test.finov-relance.com"
echo "  2. Verifie que les users peuvent se connecter"
echo "  3. Verifie les connecteurs OAuth (Microsoft, Xero, BC)"
echo "  4. Quand tout est valide, bascule le DNS de app.finov-relance.com"
echo ""
