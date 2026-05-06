#!/usr/bin/env python3
"""
Migration : index ajoutés pour optimiser le tableau de bord.

Idempotent : utilise CREATE INDEX IF NOT EXISTS et CREATE INDEX CONCURRENTLY
pour ne pas verrouiller les tables en production.

Usage :
  python scripts/migrate_dashboard_indexes.py

L'environnement DATABASE_URL doit pointer sur la base cible.

Indexes créés :

1. idx_cn_user_reminder_active (CommunicationNote)
   Index partiel sur les rappels actifs uniquement.
   Sert : SELECT ... FROM communication_notes
          WHERE user_id = X AND reminder_date IS NOT NULL AND is_reminder_completed = false
   Avant : table scan ou idx_cn_reminder_date suivi d'un filtrage user_id.
   Après : lecture quasi-instantanée même à 100k+ notes.

2. idx_snapshot_company_year (ReceivablesSnapshot)
   Index expression sur EXTRACT(YEAR FROM snapshot_date).
   Sert : /api/receivables-years (sélecteur d'années sur le graphique).
   Avant : extract() ne peut pas utiliser idx_snapshot_company_date → seq scan.
   Après : index utilisé directement.
"""

import os
import sys
import psycopg2


INDEXES = [
    {
        'name': 'idx_cn_user_reminder_active',
        # CONCURRENTLY : ne bloque pas les écritures pendant la création.
        # IF NOT EXISTS : permet de relancer le script sans erreur.
        'sql': """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cn_user_reminder_active
            ON communication_notes (user_id, reminder_date)
            WHERE reminder_date IS NOT NULL AND is_reminder_completed = false
        """,
    },
    {
        'name': 'idx_snapshot_company_year',
        'sql': """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snapshot_company_year
            ON receivables_snapshots (company_id, (EXTRACT(YEAR FROM snapshot_date)))
        """,
    },
]


def main():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        print("ERREUR : DATABASE_URL n'est pas définie.")
        sys.exit(1)

    # CREATE INDEX CONCURRENTLY ne tolère pas une transaction → autocommit.
    conn = psycopg2.connect(db_url)
    conn.set_session(autocommit=True)
    cur = conn.cursor()

    for idx in INDEXES:
        print(f"-- Création index {idx['name']}...")
        try:
            cur.execute(idx['sql'])
            print(f"   OK ({idx['name']})")
        except psycopg2.Error as e:
            # Si l'index existe déjà ou est en cours, on log et continue.
            print(f"   AVERTISSEMENT ({idx['name']}) : {e}")

    cur.close()
    conn.close()
    print("\nTerminé.")


if __name__ == '__main__':
    main()
