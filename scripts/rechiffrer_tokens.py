"""
Script de re-chiffrement Fernet (gAAAAA) -> AES-256-GCM (v2:)

Pourquoi ce script existe :
    Le code Flask actuel chiffre les tokens via les properties des modeles.
    Chaque property utilise un field_name specifique pour le sel PBKDF2 :
        - EmailConfiguration.outlook_oauth_access_token  -> 'outlook_access'
        - EmailConfiguration.outlook_oauth_refresh_token -> 'outlook_refresh'
        - EmailConfiguration.gmail_oauth_access_token    -> 'gmail_access'
        - EmailConfiguration.gmail_oauth_refresh_token   -> 'gmail_refresh'
        - EmailConfiguration.gmail_smtp_app_password     -> 'gmail_smtp_password'
        - SystemEmailConfiguration.outlook_oauth_access  -> 'system_outlook_access'
        - SystemEmailConfiguration.outlook_oauth_refresh -> 'system_outlook_refresh'
        - AccountingConnection.access_token              -> 'bc_access'
        - AccountingConnection.refresh_token             -> 'bc_refresh'
        - UserTOTP.secret                                -> 'totp_secret'

    L'ancien script scripts/migrate_encryption.py utilise des field_name
    hardcodes ('token_oauth_access') qui ne correspondent pas. Il ne peut
    pas decrypter les donnees prod.

Strategie de ce script :
    Au lieu de re-implementer la logique de chiffrement, on utilise les
    properties des modeles directement :
        - LIRE via getter -> decrypte automatiquement (Fernet legacy si gAAAAA)
        - ECRIRE via setter -> re-chiffre automatiquement (en v2: AES-256-GCM)

    Le code Flask des modeles est garanti correct (utilise en prod).

Usage :
    # Dry-run (simulation, ne modifie rien)
    python scripts/rechiffrer_tokens.py --dry-run

    # Execution reelle
    python scripts/rechiffrer_tokens.py

ATTENTION :
    - Faire un backup de la DB avant l'execution reelle.
    - Le script est idempotent : les valeurs deja en v2: sont ignorees.
    - L'execution reelle MODIFIE les colonnes chiffrees de la DB.
    - Apres execution, verifier avec psql qu'il n'y a plus de gAAAAA :
        SELECT count(*) FROM email_configurations
        WHERE outlook_oauth_access_token LIKE 'gAAAAA%%';
      Doit retourner 0.
"""

import sys
import os
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


def is_v2(value):
    """Verifie si une valeur est deja en format v2: AES-256-GCM."""
    return isinstance(value, str) and value.startswith('v2:')


def is_legacy(value):
    """Verifie si une valeur est en format Fernet legacy gAAAAA."""
    return isinstance(value, str) and value.startswith('gAAAAA')


def rechiffrer_email_configurations(dry_run, stats):
    """Re-chiffre les tokens dans EmailConfiguration via les properties."""
    from models import EmailConfiguration

    fields = [
        'outlook_oauth_access_token',
        'outlook_oauth_refresh_token',
        'gmail_oauth_access_token',
        'gmail_oauth_refresh_token',
        'gmail_smtp_app_password',
    ]

    column_map = {
        'outlook_oauth_access_token': '_outlook_oauth_access_token',
        'outlook_oauth_refresh_token': '_outlook_oauth_refresh_token',
        'gmail_oauth_access_token': '_gmail_oauth_access_token',
        'gmail_oauth_refresh_token': '_gmail_oauth_refresh_token',
        'gmail_smtp_app_password': '_gmail_smtp_app_password',
    }

    records = EmailConfiguration.query.all()
    logger.info(f'EmailConfiguration : {len(records)} lignes a inspecter')

    for record in records:
        for field in fields:
            raw_attr = column_map[field]
            raw_value = getattr(record, raw_attr, None)

            if not raw_value:
                continue

            if is_v2(raw_value):
                stats['skipped_v2'] += 1
                continue

            if not is_legacy(raw_value):
                logger.warning(
                    f'  EmailConfiguration#{record.id}.{field} : format inconnu '
                    f'(prefix={raw_value[:10]!r}), ignore'
                )
                stats['skipped_unknown'] += 1
                continue

            try:
                # LIRE via property = decrypte Fernet automatiquement
                plaintext = getattr(record, field)

                if plaintext is None:
                    logger.error(
                        f'  EmailConfiguration#{record.id}.{field} : '
                        f'decryption a retourne None (donnee corrompue ?)'
                    )
                    stats['errors'] += 1
                    continue

                if dry_run:
                    logger.info(
                        f'  [DRY-RUN] Re-chiffrerait EmailConfiguration#{record.id}.{field}'
                    )
                else:
                    # ECRIRE via setter = re-chiffre en v2:
                    setattr(record, field, plaintext)
                    logger.info(
                        f'  Re-chiffre EmailConfiguration#{record.id}.{field}'
                    )

                stats['migrated'] += 1

            except Exception as e:
                logger.error(
                    f'  Erreur EmailConfiguration#{record.id}.{field} : {e}'
                )
                stats['errors'] += 1


def rechiffrer_system_email_configurations(dry_run, stats):
    """Re-chiffre les tokens dans SystemEmailConfiguration."""
    from models import SystemEmailConfiguration

    fields = ['outlook_oauth_access_token', 'outlook_oauth_refresh_token']
    column_map = {
        'outlook_oauth_access_token': '_outlook_oauth_access_token',
        'outlook_oauth_refresh_token': '_outlook_oauth_refresh_token',
    }

    records = SystemEmailConfiguration.query.all()
    logger.info(f'SystemEmailConfiguration : {len(records)} lignes a inspecter')

    for record in records:
        for field in fields:
            raw_attr = column_map[field]
            raw_value = getattr(record, raw_attr, None)

            if not raw_value:
                continue

            if is_v2(raw_value):
                stats['skipped_v2'] += 1
                continue

            if not is_legacy(raw_value):
                logger.warning(
                    f'  SystemEmailConfiguration#{record.id}.{field} : format inconnu, ignore'
                )
                stats['skipped_unknown'] += 1
                continue

            try:
                plaintext = getattr(record, field)
                if plaintext is None:
                    logger.error(
                        f'  SystemEmailConfiguration#{record.id}.{field} : decryption None'
                    )
                    stats['errors'] += 1
                    continue

                if dry_run:
                    logger.info(
                        f'  [DRY-RUN] Re-chiffrerait SystemEmailConfiguration#{record.id}.{field}'
                    )
                else:
                    setattr(record, field, plaintext)
                    logger.info(
                        f'  Re-chiffre SystemEmailConfiguration#{record.id}.{field}'
                    )
                stats['migrated'] += 1

            except Exception as e:
                logger.error(
                    f'  Erreur SystemEmailConfiguration#{record.id}.{field} : {e}'
                )
                stats['errors'] += 1


def rechiffrer_accounting_connections(dry_run, stats):
    """Re-chiffre les tokens dans AccountingConnection."""
    from models import AccountingConnection

    fields = ['access_token', 'refresh_token']
    column_map = {
        'access_token': '_access_token',
        'refresh_token': '_refresh_token',
    }

    records = AccountingConnection.query.all()
    logger.info(f'AccountingConnection : {len(records)} lignes a inspecter')

    for record in records:
        for field in fields:
            raw_attr = column_map[field]
            raw_value = getattr(record, raw_attr, None)

            if not raw_value:
                continue

            if is_v2(raw_value):
                stats['skipped_v2'] += 1
                continue

            if not is_legacy(raw_value):
                logger.warning(
                    f'  AccountingConnection#{record.id}.{field} : format inconnu, ignore'
                )
                stats['skipped_unknown'] += 1
                continue

            try:
                plaintext = getattr(record, field)
                if plaintext is None:
                    logger.error(
                        f'  AccountingConnection#{record.id}.{field} : decryption None'
                    )
                    stats['errors'] += 1
                    continue

                if dry_run:
                    logger.info(
                        f'  [DRY-RUN] Re-chiffrerait AccountingConnection#{record.id}.{field} '
                        f'(system={record.system_type})'
                    )
                else:
                    setattr(record, field, plaintext)
                    logger.info(
                        f'  Re-chiffre AccountingConnection#{record.id}.{field}'
                    )
                stats['migrated'] += 1

            except Exception as e:
                logger.error(
                    f'  Erreur AccountingConnection#{record.id}.{field} : {e}'
                )
                stats['errors'] += 1


def rechiffrer_user_totp(dry_run, stats):
    """Re-chiffre le secret TOTP dans UserTOTP."""
    from models import UserTOTP

    records = UserTOTP.query.all()
    logger.info(f'UserTOTP : {len(records)} lignes a inspecter')

    for record in records:
        raw_value = record.secret_encrypted

        if not raw_value:
            continue

        if is_v2(raw_value):
            stats['skipped_v2'] += 1
            continue

        if not is_legacy(raw_value):
            logger.warning(
                f'  UserTOTP#{record.id}.secret : format inconnu, ignore'
            )
            stats['skipped_unknown'] += 1
            continue

        try:
            plaintext = record.secret
            if plaintext is None:
                logger.error(f'  UserTOTP#{record.id}.secret : decryption None')
                stats['errors'] += 1
                continue

            if dry_run:
                logger.info(
                    f'  [DRY-RUN] Re-chiffrerait UserTOTP#{record.id}.secret '
                    f'(user_id={record.user_id})'
                )
            else:
                record.secret = plaintext
                logger.info(f'  Re-chiffre UserTOTP#{record.id}.secret')
            stats['migrated'] += 1

        except Exception as e:
            logger.error(f'  Erreur UserTOTP#{record.id}.secret : {e}')
            stats['errors'] += 1


def main(dry_run):
    """Point d'entree principal."""
    from app import app, db

    with app.app_context():
        stats = {
            'migrated': 0,
            'skipped_v2': 0,
            'skipped_unknown': 0,
            'errors': 0,
        }

        logger.info('=' * 60)
        logger.info(
            f'Mode : {"DRY-RUN (simulation)" if dry_run else "EXECUTION REELLE"}'
        )
        logger.info('=' * 60)

        rechiffrer_email_configurations(dry_run, stats)
        rechiffrer_system_email_configurations(dry_run, stats)
        rechiffrer_accounting_connections(dry_run, stats)
        rechiffrer_user_totp(dry_run, stats)

        if not dry_run:
            try:
                db.session.commit()
                logger.info('DB commitee avec succes.')
            except Exception as e:
                db.session.rollback()
                logger.error(f'Commit echoue, rollback : {e}')
                return 1

        logger.info('=' * 60)
        logger.info('RESUME')
        logger.info('=' * 60)
        logger.info(f'  Re-chiffres   : {stats["migrated"]}')
        logger.info(f'  Deja en v2:   : {stats["skipped_v2"]}')
        logger.info(f'  Format inconnu: {stats["skipped_unknown"]}')
        logger.info(f'  Erreurs       : {stats["errors"]}')

        if dry_run:
            logger.info('')
            logger.info('Mode DRY-RUN : aucune modification en DB.')
            logger.info(
                'Pour executer reellement : python scripts/rechiffrer_tokens.py'
            )
        else:
            logger.info('')
            logger.info('Verification post-migration :')
            logger.info('  Lance dans psql :')
            logger.info(
                "    SELECT count(*) FROM email_configurations "
                "WHERE outlook_oauth_access_token LIKE 'gAAAAA%%';"
            )
            logger.info('  Doit retourner 0.')

        if stats['errors'] > 0:
            return 1
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Re-chiffrement Fernet (gAAAAA) -> AES-256-GCM (v2:)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Simuler sans modifier la DB',
    )
    args = parser.parse_args()

    sys.exit(main(dry_run=args.dry_run))
