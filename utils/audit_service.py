"""
Service d'audit centralisé pour logger toutes les actions utilisateurs.
Fournit des méthodes helpers pour faciliter l'enregistrement des logs.
IMPORTANT: Les données clients sensibles sont masquées pour protéger la vie privée.
"""

from functools import wraps


def mask_name(name):
    """
    Masque un nom pour protéger la vie privée.
    Ex: "Jean Dupont" -> "J*** D***"
    """
    if not name:
        return None
    parts = str(name).split()
    masked_parts = []
    for part in parts:
        if len(part) > 1:
            masked_parts.append(part[0] + '***')
        else:
            masked_parts.append(part + '***')
    return ' '.join(masked_parts)


def mask_email(email):
    """
    Masque un email pour protéger la vie privée.
    Ex: "jean.dupont@example.com" -> "j***@e***.com"
    Retourne toujours une valeur masquée (jamais None) pour préserver la traçabilité.
    """
    if not email:
        return None
    email_str = str(email)
    if '@' not in email_str:
        if len(email_str) > 2:
            return email_str[0] + '***' + email_str[-1]
        return '***'
    try:
        local, domain = email_str.split('@', 1)
        masked_local = local[0] + '***' if len(local) > 1 else local + '***'
        domain_parts = domain.rsplit('.', 1)
        if len(domain_parts) == 2:
            masked_domain = domain_parts[0][0] + '***.' + domain_parts[1]
        else:
            masked_domain = domain[0] + '***'
        return masked_local + '@' + masked_domain
    except Exception:
        return '***@***.***'


def mask_phone(phone):
    """
    Masque un numéro de téléphone.
    Ex: "514-555-1234" -> "***-***-1234"
    """
    if not phone:
        return None
    phone_str = str(phone)
    if len(phone_str) > 4:
        return '***' + phone_str[-4:]
    return '***'


def mask_client_data(data, mask_sensitive=True):
    """
    Masque les données clients sensibles dans un dictionnaire.
    Préserve les IDs et codes pour la traçabilité.
    """
    if not data or not mask_sensitive:
        return data

    if not isinstance(data, dict):
        return data

    masked = {}
    sensitive_name_keys = ['name', 'client_name', 'customer_name', 'contact_name', 'nom', 'nom_client']
    sensitive_email_keys = ['email', 'client_email', 'contact_email', 'courriel']
    sensitive_phone_keys = ['phone', 'telephone', 'tel', 'mobile']
    sensitive_address_keys = ['address', 'adresse', 'street', 'rue']

    for key, value in data.items():
        lower_key = key.lower()
        if lower_key in sensitive_name_keys:
            masked[key] = mask_name(value)
        elif lower_key in sensitive_email_keys:
            masked[key] = mask_email(value)
        elif lower_key in sensitive_phone_keys:
            masked[key] = mask_phone(value)
        elif lower_key in sensitive_address_keys:
            masked[key] = '***' if value else None
        elif isinstance(value, dict):
            masked[key] = mask_client_data(value, mask_sensitive)
        else:
            masked[key] = value

    return masked


class AuditActions:
    """Constants for audit action types"""

    LOGIN_SUCCESS = 'login_success'
    LOGIN_FAILED = 'login_failed'
    LOGOUT = 'logout'
    TWO_FA_SENT = '2fa_sent'
    TWO_FA_SUCCESS = '2fa_success'
    TWO_FA_FAILED = '2fa_failed'
    PASSWORD_RESET_REQUESTED = 'password_reset_requested'
    PASSWORD_RESET_COMPLETED = 'password_reset_completed'
    PASSWORD_CHANGED = 'password_changed'

    CLIENT_CREATED = 'client_created'
    CLIENT_UPDATED = 'client_updated'
    CLIENT_DELETED = 'client_deleted'
    CLIENT_IMPORTED = 'client_imported'

    INVOICE_CREATED = 'invoice_created'
    INVOICE_UPDATED = 'invoice_updated'
    INVOICE_DELETED = 'invoice_deleted'

    RECEIVABLE_CREATED = 'receivable_created'
    RECEIVABLE_UPDATED = 'receivable_updated'
    RECEIVABLE_DELETED = 'receivable_deleted'
    RECEIVABLE_MARKED_PAID = 'receivable_marked_paid'

    EMAIL_SENT = 'email_sent'
    EMAIL_TEMPLATE_CREATED = 'email_template_created'
    EMAIL_TEMPLATE_UPDATED = 'email_template_updated'
    EMAIL_TEMPLATE_DELETED = 'email_template_deleted'
    CAMPAIGN_CREATED = 'campaign_created'
    CAMPAIGN_STARTED = 'campaign_started'
    CAMPAIGN_STOPPED = 'campaign_stopped'
    CAMPAIGN_DELETED = 'campaign_deleted'
    CAMPAIGN_SENT = 'campaign_sent'

    COMPANY_CREATED = 'company_created'
    COMPANY_UPDATED = 'company_updated'
    USER_INVITED = 'user_invited'
    USER_ROLE_CHANGED = 'user_role_changed'
    USER_REMOVED = 'user_removed'
    USER_PERMISSIONS_CHANGED = 'user_permissions_changed'

    OAUTH_CONNECTED = 'oauth_connected'
    OAUTH_DISCONNECTED = 'oauth_disconnected'
    SYNC_STARTED = 'sync_started'
    SYNC_COMPLETED = 'sync_completed'
    SYNC_FAILED = 'sync_failed'

    ADMIN_USER_CREATED = 'admin_user_created'
    ADMIN_USER_MODIFIED = 'admin_user_modified'
    ADMIN_USER_DELETED = 'admin_user_deleted'
    ADMIN_COMPANY_MODIFIED = 'admin_company_modified'
    ADMIN_COMPANY_DELETED = 'admin_company_deleted'
    ADMIN_PLAN_CHANGED = 'admin_plan_changed'

    NOTE_CREATED = 'note_created'
    NOTE_UPDATED = 'note_updated'
    NOTE_DELETED = 'note_deleted'

    REMINDER_COMPLETED = 'reminder_completed'
    REMINDER_DELETED = 'reminder_deleted'
    REMINDER_REACTIVATED = 'reminder_reactivated'

    USER_CREATED = 'user_created'
    USER_ACTIVATED = 'user_activated'
    USER_DEACTIVATED = 'user_deactivated'

    LOGO_UPLOADED = 'logo_uploaded'
    LOGO_DELETED = 'logo_deleted'

    LICENSE_UPDATED = 'license_updated'

    IMPORT_CLIENTS = 'import_clients'
    IMPORT_INVOICES = 'import_invoices'


class EntityTypes:
    """Constants for entity types"""

    USER = 'user'
    COMPANY = 'company'
    CLIENT = 'client'
    INVOICE = 'invoice'
    RECEIVABLE = 'receivable'
    EMAIL = 'email'
    EMAIL_TEMPLATE = 'email_template'
    CAMPAIGN = 'campaign'
    OAUTH = 'oauth'
    SYNC = 'sync'
    PLAN = 'plan'


def audit_log(action, entity_type=None, get_entity_id=None, get_entity_name=None,
              get_old_value=None, get_new_value=None, get_details=None):
    """
    Decorator to automatically log actions on route functions.

    Args:
        action: The action type (from AuditActions)
        entity_type: The entity type (from EntityTypes)
        get_entity_id: Function to extract entity ID from function result
        get_entity_name: Function to extract entity name from function result
        get_old_value: Function to extract old value before action
        get_new_value: Function to extract new value after action
        get_details: Function to extract additional details
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            from models import AuditLog

            old_value = None
            if get_old_value:
                try:
                    old_value = get_old_value(*args, **kwargs)
                except Exception:
                    pass

            result = f(*args, **kwargs)

            try:
                entity_id = None
                entity_name = None
                new_value = None
                details = None

                if get_entity_id:
                    try:
                        entity_id = get_entity_id(result, *args, **kwargs)
                    except Exception:
                        pass

                if get_entity_name:
                    try:
                        entity_name = get_entity_name(result, *args, **kwargs)
                    except Exception:
                        pass

                if get_new_value:
                    try:
                        new_value = get_new_value(result, *args, **kwargs)
                    except Exception:
                        pass

                if get_details:
                    try:
                        details = get_details(result, *args, **kwargs)
                    except Exception:
                        pass

                AuditLog.log(
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    entity_name=entity_name,
                    old_value=old_value,
                    new_value=new_value,
                    details=details
                )
            except Exception:
                pass

            return result
        return decorated_function
    return decorator


def log_action(action, entity_type=None, entity_id=None, entity_name=None,
               old_value=None, new_value=None, details=None, user=None, company=None):
    """
    Helper function to manually log an action.
    IMPORTANT: Applique automatiquement le masquage pour les entités CLIENT.

    Args:
        action: The action type (from AuditActions)
        entity_type: The entity type (from EntityTypes)
        entity_id: ID of the affected entity
        entity_name: Human-readable name of the entity
        old_value: Previous value (for updates)
        new_value: New value (for creates/updates)
        details: Additional details as dict
        user: User object (optional, auto-detected)
        company: Company object (optional, auto-detected)
    """
    from models import AuditLog

    masked_entity_name = entity_name
    masked_old_value = old_value
    masked_new_value = new_value
    masked_details = details

    if entity_type == EntityTypes.CLIENT:
        masked_entity_name = mask_name(entity_name) if entity_name else None
        masked_old_value = mask_client_data(old_value) if old_value else None
        masked_new_value = mask_client_data(new_value) if new_value else None
        masked_details = mask_client_data(details) if details else None

    return AuditLog.log(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=masked_entity_name,
        old_value=masked_old_value,
        new_value=masked_new_value,
        details=masked_details,
        user=user,
        company=company
    )


def log_login(success, email, reason=None, user=None):
    """Log a login attempt with masked email for privacy."""
    from models import AuditLog

    action = AuditActions.LOGIN_SUCCESS if success else AuditActions.LOGIN_FAILED
    details = {'email': mask_email(email)}
    if reason:
        details['reason'] = reason

    return AuditLog.log(
        action=action,
        entity_type=EntityTypes.USER,
        details=details,
        user=user
    )


def log_2fa(action_type, email, success=True, user=None):
    """Log a 2FA action with masked email for privacy."""
    from models import AuditLog

    return AuditLog.log(
        action=action_type,
        entity_type=EntityTypes.USER,
        details={'email': mask_email(email), 'success': success},
        user=user
    )


def log_user_action(action, target_user, role=None, details=None, user=None, company=None):
    """Log a user management action (invite, role change, etc.)."""
    from models import AuditLog

    log_details = details.copy() if details else {}
    if role:
        log_details['role'] = role
    if target_user:
        log_details['target_email'] = mask_email(target_user.email)
        log_details['target_name'] = mask_name(target_user.full_name) if hasattr(target_user, 'full_name') else None

    return AuditLog.log(
        action=action,
        entity_type=EntityTypes.USER,
        entity_id=target_user.id if target_user else None,
        entity_name=mask_name(target_user.full_name) if target_user and hasattr(target_user, 'full_name') else None,
        details=log_details,
        user=user,
        company=company
    )


def log_oauth_action(action, provider, details=None, user=None, company=None):
    """Log an OAuth connection/disconnection action."""
    from models import AuditLog

    log_details = details.copy() if details else {}
    log_details['provider'] = provider

    return AuditLog.log(
        action=action,
        entity_type=EntityTypes.OAUTH,
        entity_name=provider,
        details=log_details,
        user=user,
        company=company
    )


def log_sync_action(action, sync_type, stats=None, details=None, user=None, company=None):
    """Log a synchronization action with grouped stats (for imports/syncs)."""
    from models import AuditLog

    log_details = details.copy() if details else {}
    log_details['sync_type'] = sync_type
    if stats:
        log_details['stats'] = stats

    return AuditLog.log(
        action=action,
        entity_type=EntityTypes.SYNC,
        entity_name=sync_type,
        details=log_details,
        user=user,
        company=company
    )


def log_client_action(action, client, old_data=None, new_data=None, user=None, company=None):
    """Log a client-related action with masked sensitive data."""
    from models import AuditLog

    return AuditLog.log(
        action=action,
        entity_type=EntityTypes.CLIENT,
        entity_id=client.id if client else None,
        entity_name=mask_name(client.name) if client else None,
        old_value=mask_client_data(old_data) if old_data else None,
        new_value=mask_client_data(new_data) if new_data else None,
        user=user,
        company=company
    )


def log_email_action(action, recipient=None, subject=None, template_id=None,
                     template_name=None, details=None, user=None, company=None):
    """Log an email-related action with masked recipient."""
    from models import AuditLog

    log_details = details.copy() if details else {}
    if recipient:
        log_details['recipient'] = mask_email(recipient)
    if subject:
        log_details['subject'] = subject[:100] if len(subject) > 100 else subject
    if template_id:
        log_details['template_id'] = template_id

    return AuditLog.log(
        action=action,
        entity_type=EntityTypes.EMAIL_TEMPLATE if 'template' in action else EntityTypes.EMAIL,
        entity_id=template_id,
        entity_name=template_name,
        details=log_details,
        user=user,
        company=company
    )


def log_admin_action(action, entity_type, entity_id=None, entity_name=None,
                     old_value=None, new_value=None, details=None):
    """Log an admin action."""
    from models import AuditLog

    return AuditLog.log(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        old_value=old_value,
        new_value=new_value,
        details=details
    )


class CronJobLogger:
    """Context manager for logging cron job executions."""

    def __init__(self, job_name, details=None):
        self.job_name = job_name
        self.details = details or {}
        self.log_entry = None
        self.items_processed = 0
        self.items_failed = 0
        self.items_skipped = 0

    def __enter__(self):
        from models import CronJobLog
        self.log_entry = CronJobLog.start_job(self.job_name, self.details)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.log_entry:
            if exc_type:
                self.log_entry.complete_job(
                    status='failed',
                    error_message=str(exc_val) if exc_val else 'Unknown error',
                    items_processed=self.items_processed,
                    items_failed=self.items_failed,
                    items_skipped=self.items_skipped
                )
            else:
                status = 'warning' if self.items_failed > 0 else 'success'
                self.log_entry.complete_job(
                    status=status,
                    items_processed=self.items_processed,
                    items_failed=self.items_failed,
                    items_skipped=self.items_skipped
                )
        return False

    def set_counts(self, processed=None, failed=None, skipped=None):
        """Update item counts."""
        if processed is not None:
            self.items_processed = processed
        if failed is not None:
            self.items_failed = failed
        if skipped is not None:
            self.items_skipped = skipped

    def add_processed(self, count=1):
        """Increment processed count."""
        self.items_processed += count

    def add_failed(self, count=1):
        """Increment failed count."""
        self.items_failed += count

    def add_skipped(self, count=1):
        """Increment skipped count."""
        self.items_skipped += count
