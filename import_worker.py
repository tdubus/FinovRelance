"""
Background worker for processing import jobs asynchronously
Uses threading to avoid blocking the main Flask request
"""

import threading
import logging
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
import os

logger = logging.getLogger(__name__)

# Redis key prefix for import file storage
REDIS_IMPORT_FILE_PREFIX = 'import_file:'
REDIS_IMPORT_FILE_TTL = 3600  # 1 hour


def _get_redis_for_import():
    """Get Redis client for import file storage. Returns None if unavailable."""
    try:
        redis_url = os.environ.get('REDIS_URL')
        if not redis_url:
            return None
        import redis
        client = redis.from_url(redis_url)
        client.ping()
        return client
    except Exception:
        return None


def store_import_file(session_id, file_content):
    """Store import file content in Redis with /tmp fallback.

    Args:
        session_id: Unique identifier for the import session (or job_id)
        file_content: bytes content of the file

    Returns:
        str: Storage location identifier ('redis' or file path)
    """
    import tempfile

    redis_client = _get_redis_for_import()
    if redis_client:
        try:
            redis_key = f"{REDIS_IMPORT_FILE_PREFIX}{session_id}"
            redis_client.setex(redis_key, REDIS_IMPORT_FILE_TTL, file_content)
            logger.info(f"Import file stored in Redis: {redis_key} ({len(file_content)} bytes)")
            return 'redis'
        except Exception as e:
            logger.warning(f"Redis store failed for import file, falling back to /tmp: {e}")

    # Fallback: write to /tmp
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, f"import_job_{session_id}")
    with open(file_path, 'wb') as f:
        f.write(file_content)
    logger.info(f"Import file stored on disk: {file_path} ({len(file_content)} bytes)")
    return file_path


def retrieve_import_file(session_id):
    """Retrieve import file content from Redis with /tmp fallback.

    Args:
        session_id: Unique identifier for the import session (or job_id)

    Returns:
        bytes or None: File content, or None if not found
    """
    import tempfile

    # Try Redis first
    redis_client = _get_redis_for_import()
    if redis_client:
        try:
            redis_key = f"{REDIS_IMPORT_FILE_PREFIX}{session_id}"
            content = redis_client.get(redis_key)
            if content:
                logger.info(f"Import file retrieved from Redis: {redis_key}")
                return content
        except Exception as e:
            logger.warning(f"Redis retrieve failed: {e}")

    # Fallback: read from /tmp
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, f"import_job_{session_id}")
    if os.path.exists(file_path):
        with open(file_path, 'rb') as f:
            content = f.read()
        logger.info(f"Import file retrieved from disk: {file_path}")
        return content

    return None


def cleanup_import_file(session_id):
    """Remove import file from both Redis and /tmp."""
    import tempfile

    redis_client = _get_redis_for_import()
    if redis_client:
        try:
            redis_key = f"{REDIS_IMPORT_FILE_PREFIX}{session_id}"
            redis_client.delete(redis_key)
        except Exception:
            pass

    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, f"import_job_{session_id}")
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass


class ImportWorker:
    """Worker for processing import jobs in background"""

    def __init__(self, app=None):
        self.app = app
        self._thread = None

    def process_import_job(self, job_id, app=None):
        """
        Process an import job in a background thread
        Creates its own database session to avoid conflicts with main thread

        Args:
            job_id: ID of the ImportJob to process
            app: Flask app instance (required for app context in background thread)
        """
        def run_import():
            try:
                _run_import_inner()
            except Exception as e:
                logger.error(f"Import job {job_id} thread crashed: {e}", exc_info=True)

        def _run_import_inner():
            # Create a new database session for this thread
            database_url = os.environ.get('DATABASE_URL')
            if not database_url:
                logger.error(f"Import job {job_id} failed: DATABASE_URL environment variable is not set")
                return
            engine = create_engine(database_url, pool_pre_ping=True)
            Session = scoped_session(sessionmaker(bind=engine))
            session = Session()

            try:
                from models import ImportJob, Client, Invoice, Company
                from file_import_connector import transform_file_to_standard_format

                # Load job
                job = session.query(ImportJob).get(job_id)
                if not job:
                    logger.error(f"Import job {job_id} not found")
                    return

                # Mark as processing
                job.mark_as_processing()
                session.commit()

                logger.info(f"Starting import job {job_id}: {job.import_type} from {job.filename}")

                # Load file mapping configuration
                from models import FileImportMapping
                mapping_config = session.query(FileImportMapping).filter_by(company_id=job.company_id).first()

                if not mapping_config or not mapping_config.is_configured:
                    job.mark_as_failed("Configuration du mapping non trouvée")
                    session.commit()
                    return

                # Get appropriate mapping based on import type
                if job.import_type == 'clients':
                    column_mapping = mapping_config.get_client_mapping()
                elif job.import_type == 'invoices':
                    column_mapping = mapping_config.get_invoice_mapping()
                else:
                    job.mark_as_failed(f"Type d'import invalide: {job.import_type}")
                    session.commit()
                    return

                # Read file from Redis or temporary storage
                file_content = retrieve_import_file(job_id)

                if not file_content:
                    job.mark_as_failed("Fichier d'import introuvable")
                    session.commit()
                    return

                # Determine file type
                file_extension = job.filename.lower().split('.')[-1]
                file_type = 'excel' if file_extension in ['xlsx', 'xls'] else 'csv'

                # Transform file using intelligent connector
                language_mappings = mapping_config.get_language_mappings()
                company = session.query(Company).get(job.company_id)
                include_project_field = company.project_field_enabled if company else False

                rows, total_rows, transform_errors = transform_file_to_standard_format(
                    file_content,
                    file_type,
                    column_mapping,
                    job.import_type,
                    language_mappings,
                    include_project_field
                )

                job.total_rows = total_rows
                session.commit()

                if transform_errors:
                    logger.warning(f"Transform errors: {transform_errors}")

                # Process rows based on import type and mode
                if job.import_type == 'clients':
                    result = self._process_clients(session, rows, job.company_id, job)
                elif job.import_type == 'invoices':
                    result = self._process_invoices(session, rows, job.company_id, job)
                else:
                    job.mark_as_failed(f"Type d'import non supporté: {job.import_type}")
                    session.commit()
                    return

                # Unpack results (supports both old and new format)
                success_count = result[0]
                error_count = result[1]
                errors = result[2]
                created = result[3] if len(result) > 3 else 0
                updated = result[4] if len(result) > 4 else 0
                deleted = result[5] if len(result) > 5 else 0

                # Mark as completed with detailed counts
                all_errors = transform_errors + errors if errors else transform_errors
                job.mark_as_completed(success_count, error_count, all_errors, created, updated, deleted)

                # Build result message based on mode
                if job.import_mode == 'sync':
                    job.result_message = f"Synchronisation: {created} créées, {updated} mises à jour, {deleted} supprimées"
                    if error_count > 0:
                        job.result_message += f", {error_count} erreurs"
                else:
                    job.result_message = f"{success_count} enregistrements importés avec succès"
                    if error_count > 0:
                        job.result_message += f", {error_count} erreurs"

                session.commit()
                logger.info(f"Import job {job_id} completed: {success_count} success, {error_count} errors")

                # Send success notification
                try:
                    from models import Notification, User
                    user = session.query(User).get(job.user_id)
                    if user:
                        if job.import_mode == 'sync':
                            msg = f'Import réussi : {created} factures créées, {updated} mises à jour, {deleted} supprimées'
                        else:
                            msg = f'Import réussi : {success_count} enregistrements importés'
                        if error_count > 0:
                            msg += f', {error_count} erreurs'
                        notif = Notification(
                            user_id=user.id,
                            company_id=job.company_id,
                            type='file_import_success',
                            title='Synchronisation terminée',
                            message=msg,
                            is_read=False
                        )
                        session.add(notif)
                        session.commit()
                except Exception as notif_error:
                    logger.warning(f"Notification non envoyée: {notif_error}")

                try:
                    from models import AuditLog, User, Company
                    from utils.audit_service import AuditActions, EntityTypes
                    company_obj = session.query(Company).get(job.company_id)
                    user_obj = session.query(User).get(job.user_id) if job.user_id else None
                    AuditLog.log_with_session(
                        session=session,
                        action=AuditActions.SYNC_COMPLETED,
                        entity_type=EntityTypes.SYNC,
                        entity_name=f"Import {job.import_type}",
                        details={
                            'sync_type': f'import_{job.import_type}',
                            'mode': job.import_mode,
                            'stats': {
                                'created': created,
                                'updated': updated,
                                'deleted': deleted,
                                'errors': error_count
                            }
                        },
                        user=user_obj,
                        company=company_obj
                    )
                    session.commit()
                except Exception as audit_error:
                    logger.warning(f"Audit log non créé pour import: {audit_error}")

                # Enregistrer snapshot des CAR (non bloquant) - uniquement pour import de factures
                if job.import_type == 'invoices':
                    try:
                        from utils.receivables_snapshot import create_receivables_snapshot
                        create_receivables_snapshot(job.company_id, trigger_type='import', session=session)
                    except Exception as snapshot_error:
                        logger.warning(f"Snapshot CAR non créé: {snapshot_error}")

                # Clean up temporary file from Redis and /tmp
                cleanup_import_file(job_id)

            except Exception as e:
                logger.error(f"Error processing import job {job_id}: {e}", exc_info=True)
                try:
                    job = session.query(ImportJob).get(job_id)
                    if job:
                        job.mark_as_failed(str(e))
                        session.commit()

                        # Send error notification
                        from models import Notification, User
                        user = session.query(User).get(job.user_id)
                        if user:
                            notif = Notification(
                                user_id=user.id,
                                company_id=job.company_id,
                                type='file_import_error',
                                title='Erreur synchronisation',
                                message=f'Erreur lors de l\'import : {str(e)[:200]}',
                                is_read=False
                            )
                            session.add(notif)
                            session.commit()
                except Exception:
                    pass
            finally:
                session.close()
                Session.remove()
                engine.dispose()

        # Start thread (with app context if available)
        def run_with_context():
            if app:
                with app.app_context():
                    run_import()
            else:
                run_import()

        thread = threading.Thread(target=run_with_context, daemon=True)
        thread.start()
        logger.info(f"Import job {job_id} started in background thread")

    # Batch size for commits during import processing
    BATCH_SIZE = 500

    def _process_clients(self, session, rows, company_id, job):
        """Process client import rows"""
        from models import Client
        from werkzeug.security import generate_password_hash

        success_count = 0
        error_count = 0
        errors = []

        # Cache existing clients
        existing_clients = {}
        clients_query = session.query(Client).filter_by(company_id=company_id).all()
        for client in clients_query:
            existing_clients[client.code_client] = client

        clients_to_create = []
        clients_to_update = []
        batch_success = 0  # Track successes within the current batch

        for processed_index, row in enumerate(rows, start=1):
            # Row number for error messages (line 2 = first data row after header)
            row_num = processed_index + 1

            try:
                # Expected format: [code_client, name, email, phone, address, representative_name, payment_terms, parent_code, language]
                if len(row) < 2:
                    errors.append(f'Ligne {row_num}: Données insuffisantes')
                    error_count += 1
                    continue

                code_client = row[0].strip()
                name = row[1].strip()

                if not code_client or not name:
                    errors.append(f'Ligne {row_num}: Code client et nom requis')
                    error_count += 1
                    continue

                # Extract other fields
                email = row[2].strip() if len(row) > 2 else None
                phone = row[3].strip() if len(row) > 3 else None
                address = row[4].strip() if len(row) > 4 else None
                representative_name = row[5].strip() if len(row) > 5 else None
                payment_terms = int(row[6]) if len(row) > 6 and row[6].strip() else None
                parent_code = row[7].strip() if len(row) > 7 else None
                language = row[8].strip() if len(row) > 8 else 'fr'

                if code_client in existing_clients:
                    # Update existing
                    client = existing_clients[code_client]
                    client.name = name
                    if email:
                        client.email = email
                    if phone:
                        client.phone = phone
                    if address:
                        client.address = address
                    if representative_name:
                        client.representative_name = representative_name
                    if payment_terms is not None:
                        client.payment_terms = payment_terms
                    if language:
                        client.language = language
                    clients_to_update.append(client)
                else:
                    # Create new
                    new_client = Client(
                        company_id=company_id,
                        code_client=code_client,
                        name=name,
                        email=email,
                        phone=phone,
                        address=address,
                        representative_name=representative_name,
                        payment_terms=payment_terms,
                        language=language
                    )
                    clients_to_create.append(new_client)

                success_count += 1
                batch_success += 1

                # Commit in batches of BATCH_SIZE
                if batch_success >= self.BATCH_SIZE:
                    try:
                        if clients_to_create:
                            session.bulk_save_objects(clients_to_create)
                            clients_to_create = []
                        session.commit()
                        job.update_progress(processed_index, len(rows))
                        session.commit()
                        logger.info(f"Import job clients batch committed: {processed_index}/{len(rows)} rows processed")
                        batch_success = 0
                    except Exception as batch_err:
                        session.rollback()
                        logger.error(f"Batch commit failed at row {processed_index}: {batch_err}")
                        errors.append(f'Erreur batch lignes {processed_index - self.BATCH_SIZE + 1}-{processed_index}: {str(batch_err)}')
                        error_count += batch_success
                        success_count -= batch_success
                        batch_success = 0
                        clients_to_create = []

            except Exception as e:
                logger.error(f"Error processing client row {row_num}: {e}")
                errors.append(f'Ligne {row_num}: {str(e)}')
                error_count += 1

        # Final commit for remaining rows
        if clients_to_create or batch_success > 0:
            try:
                if clients_to_create:
                    session.bulk_save_objects(clients_to_create)
                session.commit()
                job.update_progress(len(rows), len(rows))
                session.commit()
            except Exception as batch_err:
                session.rollback()
                logger.error(f"Final batch commit failed: {batch_err}")
                errors.append(f'Erreur batch final: {str(batch_err)}')
                error_count += batch_success
                success_count -= batch_success

        # For clients, always append mode (no sync yet)
        return success_count, error_count, errors, 0, 0, 0

    def _process_invoices(self, session, rows, company_id, job):
        """Process invoice import rows with optional sync mode"""
        from models import Client, Invoice, CommunicationNote
        from datetime import datetime

        success_count = 0
        error_count = 0
        errors = []
        created_count = 0
        updated_count = 0
        deleted_count = 0

        # Cache clients (code_client -> client_id)
        clients_cache = {}
        clients = session.query(Client).filter_by(company_id=company_id).all()
        for client in clients:
            clients_cache[client.code_client] = client.id

        # Load all existing invoices for this company (invoice_number is unique per company)
        existing_invoices = {}
        all_invoices = session.query(Invoice).filter_by(company_id=company_id).all()
        for inv in all_invoices:
            existing_invoices[inv.invoice_number] = inv

        # For SYNC mode: compute delta and delete missing invoices
        if job.import_mode == 'sync':
            # Build set of invoice numbers from import file
            incoming_invoice_numbers = set()
            for row in rows:
                if len(row) >= 2:
                    invoice_number = row[1].strip()
                    if invoice_number:
                        incoming_invoice_numbers.add(invoice_number)

            # Compute invoices to delete (exist in DB but not in file)
            invoices_to_delete_numbers = set(existing_invoices.keys()) - incoming_invoice_numbers

            # Delete invoices that are no longer in the file
            # SECURITY: Block deletion if invoice has communication notes
            for invoice_number in invoices_to_delete_numbers:
                invoice = existing_invoices[invoice_number]

                # Check if invoice has communication notes
                has_notes = session.query(CommunicationNote).filter_by(
                    invoice_id=invoice.id
                ).first() is not None

                if has_notes:
                    errors.append(f'Facture {invoice_number}: Suppression bloquée (notes de communication associées)')
                    error_count += 1
                else:
                    try:
                        session.delete(invoice)
                        deleted_count += 1

                        # Commit in batches
                        if deleted_count % 100 == 0:
                            session.commit()
                    except Exception as e:
                        logger.error(f"Error deleting invoice {invoice_number}: {e}")
                        errors.append(f'Facture {invoice_number}: Erreur de suppression - {str(e)}')
                        error_count += 1

            # Final commit for deletions
            if deleted_count % 100 != 0:
                session.commit()

            logger.info(f"Sync mode: Deleted {deleted_count} invoices (blocked: {len([e for e in errors if 'bloquée' in e])})")

        # Process rows (create or update)
        invoices_to_create = []
        invoices_to_update = []
        batch_success = 0  # Track successes within the current batch
        batch_created = 0
        batch_updated = 0

        for processed_index, row in enumerate(rows, start=1):
            # Row number for error messages (line 2 = first data row after header)
            row_num = processed_index + 1

            try:
                # Expected format: [code_client, invoice_number, amount, original_amount, issue_date, due_date, project_name]
                if len(row) < 5:
                    errors.append(f'Ligne {row_num}: Données insuffisantes')
                    error_count += 1
                    continue

                code_client = row[0].strip()
                invoice_number = row[1].strip()
                amount_str = row[2].strip()
                original_amount_str = row[3].strip() if len(row) > 3 and row[3].strip() else None
                issue_date_str = row[4].strip()
                due_date_str = row[5].strip()
                project_name = row[6].strip() if len(row) > 6 and row[6].strip() else None

                # Validate
                if not all([code_client, invoice_number, amount_str, issue_date_str, due_date_str]):
                    errors.append(f'Ligne {row_num}: Données manquantes')
                    error_count += 1
                    continue

                # Get client ID
                client_id = clients_cache.get(code_client)
                if not client_id:
                    errors.append(f'Ligne {row_num}: Client "{code_client}" non trouvé')
                    error_count += 1
                    continue

                # Parse dates
                try:
                    invoice_date = datetime.strptime(issue_date_str, '%Y-%m-%d').date()
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                except ValueError:
                    errors.append(f'Ligne {row_num}: Date invalide (format YYYY-MM-DD)')
                    error_count += 1
                    continue

                # Parse amounts
                try:
                    amount = float(amount_str.replace(',', '.'))
                    original_amount = float(original_amount_str.replace(',', '.')) if original_amount_str else None
                except (ValueError, AttributeError):
                    errors.append(f'Ligne {row_num}: Montant invalide')
                    error_count += 1
                    continue

                # Check if invoice exists (always check - loaded at start of method)
                existing_invoice = existing_invoices.get(invoice_number)

                if existing_invoice:
                    # UPDATE mode: Only update the amount (solde)
                    existing_invoice.amount = amount
                    existing_invoice.updated_at = datetime.utcnow()
                    updated_count += 1
                    batch_updated += 1
                    success_count += 1
                else:
                    # CREATE mode: Create new invoice
                    invoice_data = {
                        'invoice_number': invoice_number,
                        'client_id': client_id,
                        'company_id': company_id,
                        'invoice_date': invoice_date,
                        'due_date': due_date,
                        'amount': amount,
                        'original_amount': original_amount,
                        'is_paid': False,
                        'created_at': datetime.utcnow(),
                        'updated_at': datetime.utcnow()
                    }

                    if project_name:
                        invoice_data['project_name'] = project_name

                    new_invoice = Invoice(**invoice_data)
                    invoices_to_create.append(new_invoice)
                    created_count += 1
                    batch_created += 1
                    success_count += 1

                    # IMPORTANT: Add to existing_invoices to prevent duplicates in same file
                    existing_invoices[invoice_number] = new_invoice

                batch_success += 1

                # Commit in batches of BATCH_SIZE
                if batch_success >= self.BATCH_SIZE:
                    try:
                        if invoices_to_create:
                            session.bulk_save_objects(invoices_to_create)
                            invoices_to_create = []
                        session.commit()
                        job.update_progress(processed_index, len(rows))
                        session.commit()
                        logger.info(f"Import job invoices batch committed: {processed_index}/{len(rows)} rows processed")
                        batch_success = 0
                        batch_created = 0
                        batch_updated = 0
                    except Exception as batch_err:
                        session.rollback()
                        logger.error(f"Batch commit failed at row {processed_index}: {batch_err}")
                        errors.append(f'Erreur batch lignes {processed_index - self.BATCH_SIZE + 1}-{processed_index}: {str(batch_err)}')
                        error_count += batch_success
                        success_count -= batch_success
                        created_count -= batch_created
                        updated_count -= batch_updated
                        batch_success = 0
                        batch_created = 0
                        batch_updated = 0
                        invoices_to_create = []

            except Exception as e:
                logger.error(f"Error processing invoice row {row_num}: {e}")
                errors.append(f'Ligne {row_num}: {str(e)}')
                error_count += 1

        # Final commit for remaining rows
        if invoices_to_create or batch_success > 0:
            try:
                if invoices_to_create:
                    session.bulk_save_objects(invoices_to_create)
                session.commit()
                job.update_progress(len(rows), len(rows))
                session.commit()
            except Exception as batch_err:
                session.rollback()
                logger.error(f"Final batch commit failed: {batch_err}")
                errors.append(f'Erreur batch final: {str(batch_err)}')
                error_count += batch_success
                success_count -= batch_success
                created_count -= batch_created
                updated_count -= batch_updated

        logger.info(f"Invoice processing completed: {created_count} created, {updated_count} updated, {deleted_count} deleted")

        return success_count, error_count, errors, created_count, updated_count, deleted_count


# Global worker instance
_worker = None

def get_worker():
    """Get or create the global worker instance"""
    global _worker
    if _worker is None:
        _worker = ImportWorker()
    return _worker
