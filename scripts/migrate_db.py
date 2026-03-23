#!/usr/bin/env python3
"""
Script de migration de la base de donnees FinovRelance.
Copie integrale de la base de production Neon vers la cible.

Usage :
  python scripts/migrate_db.py --source DATABASE_URL_SOURCE --target DATABASE_URL_TARGET

Prerequis :
  - pg_dump et psql installes localement
  - Acces reseau aux deux bases de donnees
"""

import subprocess
import sys
import argparse
import os
from datetime import datetime


def run_command(cmd, description, env=None):
    """Execute une commande shell et affiche le resultat."""
    print(f"\n{'='*60}")
    print(f"  {description}")
    print(f"{'='*60}")

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    result = subprocess.run(
        cmd, shell=False, capture_output=True, text=True, env=merged_env
    )

    if result.returncode != 0:
        print(f"ERREUR : {result.stderr}")
        sys.exit(1)

    if result.stdout:
        print(result.stdout[:500])  # Tronquer pour lisibilite

    print("OK")
    return result


def main():
    parser = argparse.ArgumentParser(description="Migration DB FinovRelance")
    parser.add_argument("--source", required=True, help="URL de la base source (Neon prod)")
    parser.add_argument("--target", required=True, help="URL de la base cible (VPS ou local)")
    parser.add_argument("--dump-only", action="store_true", help="Seulement faire le dump, pas la restauration")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_file = f"finovrelance_dump_{timestamp}.sql"

    # Etape 1 : Dump de la base source
    run_command(
        ['pg_dump', args.source, '--no-owner', '--no-acl', '--clean', '--if-exists', '-f', dump_file],
        "Dump de la base de production (Neon)..."
    )

    print(f"\nDump sauvegarde dans : {dump_file}")

    if args.dump_only:
        print("Mode dump-only : arret ici.")
        return

    # Etape 2 : Restauration dans la base cible
    run_command(
        ['psql', args.target, '-f', dump_file],
        "Restauration dans la base cible..."
    )

    # Etape 3 : Verification
    run_command(
        ['psql', args.target, '-c', "SELECT COUNT(*) as nb_tables FROM information_schema.tables WHERE table_schema = 'public';"],
        "Verification : nombre de tables dans la cible..."
    )

    run_command(
        ['psql', args.target, '-c', "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;"],
        "Liste des tables migrees..."
    )

    print(f"\n{'='*60}")
    print("  MIGRATION TERMINEE AVEC SUCCES")
    print(f"{'='*60}")
    print(f"Dump conserve dans : {dump_file}")
    print("Supprime-le apres validation pour des raisons de securite.")


if __name__ == "__main__":
    main()
