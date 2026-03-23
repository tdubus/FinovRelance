"""
Script de migration Fernet -> AES-256-GCM

A executer une fois pour re-chiffrer les tokens existants.
Lit les donnees chiffrees en Fernet (legacy), les dechiffre, et les re-chiffre en AES-256-GCM.

ATTENTION:
- Faire un backup de la DB avant execution
- Tester sur un environnement de staging avant production
- Ce script est idempotent: les valeurs deja en v2: seront ignorees

Usage:
    python scripts/migrate_encryption.py --dry-run   # Simuler sans modifier
    python scripts/migrate_encryption.py              # Executer la migration
"""

import os
import sys
import argparse
import logging

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def migrate_encryption(dry_run=True):
    """Re-chiffre toutes les colonnes chiffrees de Fernet vers AES-256-GCM."""
    from app import app, db
    from security.encryption_service import EncryptionService

    with app.app_context():
        encryption = EncryptionService()

        # Colonnes a migrer: (Model, column_name, field_name_for_encryption, entity_id_column)
        # Adapter selon les modeles reels de l'application
        from models import EmailConfiguration, SystemEmailConfiguration, AccountingConnection

        migrations = [
            # EmailConfiguration tokens
            (EmailConfiguration, 'outlook_oauth_access_token', 'token_oauth_access', 'user_id'),
            (EmailConfiguration, 'outlook_oauth_refresh_token', 'token_oauth_refresh', 'user_id'),
            (EmailConfiguration, 'gmail_oauth_access_token', 'token_oauth_access', 'user_id'),
            (EmailConfiguration, 'gmail_oauth_refresh_token', 'token_oauth_refresh', 'user_id'),
            (EmailConfiguration, 'gmail_smtp_app_password', 'gmail_smtp_app_password', 'user_id'),
            # SystemEmailConfiguration tokens
            (SystemEmailConfiguration, 'outlook_oauth_access_token', 'token_oauth_access', 'id'),
            (SystemEmailConfiguration, 'outlook_oauth_refresh_token', 'token_oauth_refresh', 'id'),
            # AccountingConnection tokens
            (AccountingConnection, 'access_token', 'token_oauth_access', 'company_id'),
            (AccountingConnection, 'refresh_token', 'token_oauth_refresh', 'company_id'),
        ]

        total_migrated = 0
        total_skipped = 0
        total_errors = 0

        for Model, column_name, field_name, entity_id_col in migrations:
            logger.info(f"\n--- Migration {Model.__name__}.{column_name} ---")

            try:
                records = Model.query.all()
            except Exception as e:
                logger.error(f"Cannot query {Model.__name__}: {e}")
                continue

            for record in records:
                value = getattr(record, column_name, None)
                if not value:
                    continue

                # Skip already migrated values
                if value.startswith('v2:'):
                    total_skipped += 1
                    continue

                # Skip plaintext values (not encrypted)
                if not encryption.is_encrypted(value):
                    total_skipped += 1
                    continue

                entity_id = getattr(record, entity_id_col, None)

                try:
                    # Decrypt with legacy Fernet
                    decrypted = encryption.decrypt_field(value, field_name, entity_id)
                    if decrypted is None:
                        logger.warning(f"  Cannot decrypt {Model.__name__}#{record.id}.{column_name}")
                        total_errors += 1
                        continue

                    # Re-encrypt with AES-256-GCM
                    new_encrypted = encryption._encrypt_aes_gcm(decrypted, field_name, entity_id)

                    if dry_run:
                        logger.info(f"  [DRY-RUN] Would migrate {Model.__name__}#{record.id}.{column_name}")
                    else:
                        setattr(record, column_name, new_encrypted)
                        logger.info(f"  Migrated {Model.__name__}#{record.id}.{column_name}")

                    total_migrated += 1

                except Exception as e:
                    logger.error(f"  Error migrating {Model.__name__}#{record.id}.{column_name}: {e}")
                    total_errors += 1

        if not dry_run:
            try:
                db.session.commit()
                logger.info("Database committed successfully.")
            except Exception as e:
                db.session.rollback()
                logger.error(f"Commit failed: {e}")
                return

        logger.info(f"\n=== Migration Summary ===")
        logger.info(f"Migrated: {total_migrated}")
        logger.info(f"Skipped (already v2 or plaintext): {total_skipped}")
        logger.info(f"Errors: {total_errors}")
        if dry_run:
            logger.info("DRY-RUN mode - no changes written to database.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Migrate encryption from Fernet to AES-256-GCM')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Simulate without modifying data')
    args = parser.parse_args()

    migrate_encryption(dry_run=args.dry_run)
