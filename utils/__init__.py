"""
Module utilitaires pour la plateforme FinovRelance
Contient les outils communs de sécurité, logging, et gestion des erreurs
"""

# Import des modules principaux pour faciliter l'utilisation
from .secure_logging import (
    sanitize_email_for_logs,
    sanitize_user_id_for_logs,
    sanitize_company_id_for_logs,
    sanitize_stripe_id_for_logs,
    sanitize_sensitive_data_for_logs,
    create_secure_log_message,
    secure_log_function_call
)

from .http_client import (
    RobustHTTPSession,
    get_robust_session,
    retry_on_failure,
    create_stripe_session,
    create_microsoft_session,
    create_quickbooks_session
)

# Les fonctions currency sont dans le fichier utils.py racine, pas dans utils/currency.py

# Import des fonctions existantes depuis utils.py (fichier racine)
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# FONCTIONS UTILITAIRES - Copiées depuis utils.py (source de vérité)
# NOTE: Impossible d'importer directement depuis utils.py à cause d'imports circulaires
# Ces fonctions sont maintenues en sync avec utils.py

CURRENCY_CONFIG = {
    'CAD': {'symbol': '$', 'position': 'after', 'locale': 'fr-CA', 'name': 'Dollar canadien'},
    'USD': {'symbol': '$', 'position': 'before', 'locale': 'en-US', 'name': 'Dollar américain'},
    'EUR': {'symbol': '€', 'position': 'after', 'locale': 'fr-FR', 'name': 'Euro'},
    'GBP': {'symbol': '£', 'position': 'before', 'locale': 'en-GB', 'name': 'Livre sterling'},
    'CHF': {'symbol': 'CHF', 'position': 'after', 'locale': 'fr-CH', 'name': 'Franc suisse'},
}

def format_currency(amount, currency='CAD'):
    """Format currency according to the specified currency code (ISO 4217)

    Args:
        amount: The amount to format (can be None, float, Decimal, etc.)
        currency: ISO 4217 currency code (CAD, USD, EUR, GBP, CHF). Defaults to CAD.

    Returns:
        Formatted currency string
    """
    if amount is None:
        amount = 0

    try:
        # Handle Decimal types
        if hasattr(amount, '__float__'):
            amount = float(amount)

        # Get currency config (default to CAD if unknown)
        config = CURRENCY_CONFIG.get(currency, CURRENCY_CONFIG['CAD'])
        symbol = config['symbol']
        position = config['position']

        # Format with appropriate number formatting
        formatted = f"{abs(amount):,.2f}"

        # Split into integer and decimal parts
        if '.' in formatted:
            integer_part, decimal_part = formatted.split('.')
        else:
            integer_part, decimal_part = formatted, "00"

        # French-style formatting (space for thousands, comma for decimals)
        # Used for CAD, EUR, CHF
        if currency in ('CAD', 'EUR', 'CHF'):
            integer_part = integer_part.replace(',', ' ')
            number_str = f"{integer_part},{decimal_part}"
        else:
            # English-style formatting (comma for thousands, period for decimals)
            # Used for USD, GBP
            number_str = f"{integer_part}.{decimal_part}"

        # Handle negative amounts
        prefix = "-" if amount < 0 else ""

        # Position symbol
        if position == 'before':
            return f"{prefix}{symbol}{number_str}"
        else:
            return f"{prefix}{number_str} {symbol}"

    except Exception as e:
        from flask import current_app
        current_app.logger.error(f"Error formatting currency: {str(e)}")
        symbol = CURRENCY_CONFIG.get(currency, CURRENCY_CONFIG['CAD'])['symbol']
        return f"0,00 {symbol}"

def check_feature_access(feature_name):
    """
    Helper function for templates to check feature access
    Returns dict with access info for button styling
    """
    from flask_login import current_user
    if not current_user.is_authenticated:
        return {
            'allowed': False,
            'restriction': 'role',
            'message': "Vous n'avez pas accès à cette fonctionnalité",
            'css_class': 'btn-disabled',
            'icon': 'fas fa-lock'
        }

    company = current_user.get_selected_company()
    if not company:
        return {
            'allowed': False,
            'restriction': 'role',
            'message': "Aucune entreprise sélectionnée",
            'css_class': 'btn-disabled',
            'icon': 'fas fa-lock'
        }

    from permissions import PermissionService
    can_access, restriction_reason = PermissionService.can_access_feature(
        current_user, company, feature_name
    )

    if can_access:
        return {
            'allowed': True,
            'restriction': None,
            'message': None,
            'css_class': '',
            'icon': None
        }
    else:
        message = PermissionService.get_restriction_message(restriction_reason)
        return {
            'allowed': False,
            'restriction': restriction_reason,
            'message': message,
            'css_class': 'btn-disabled',
            'icon': 'fas fa-lock'
        }

def get_user_timezone():
    """Get user's timezone from session or default to Toronto"""
    from flask import session
    return session.get('user_timezone', 'America/Toronto')

def convert_utc_to_local(utc_datetime, timezone_str=None):
    """Convert UTC datetime to local timezone"""
    if not utc_datetime:
        return None

    try:
        import pytz

        if timezone_str is None:
            timezone_str = get_user_timezone()

        local_tz = pytz.timezone(timezone_str)

        # If datetime is naive (no timezone), assume it's UTC
        if utc_datetime.tzinfo is None:
            utc_datetime = pytz.utc.localize(utc_datetime)

        # Convert to local timezone
        local_datetime = utc_datetime.astimezone(local_tz)
        return local_datetime

    except Exception as e:
        from flask import current_app
        current_app.logger.error(f"Error converting UTC to local: {str(e)}")
        return None

def convert_local_to_utc(local_datetime, timezone_str=None):
    """Convert local datetime to UTC"""
    if not local_datetime:
        return None

    if timezone_str is None:
        timezone_str = get_user_timezone()

    import pytz
    local_tz = pytz.timezone(timezone_str)

    # If datetime is naive, assume it's in local timezone
    if local_datetime.tzinfo is None:
        local_datetime = local_tz.localize(local_datetime)

    # Convert to UTC
    utc_datetime = local_datetime.astimezone(pytz.UTC)
    return utc_datetime

def get_local_now(timezone_str=None):
    """Get current datetime in local timezone"""
    if timezone_str is None:
        timezone_str = get_user_timezone()

    import pytz
    from datetime import datetime
    local_tz = pytz.timezone(timezone_str)
    return datetime.now(local_tz)

def format_local_datetime(utc_datetime, format_str='%d/%m/%Y %H:%M', timezone_str=None):
    """Format UTC datetime as local timezone string"""
    local_dt = convert_utc_to_local(utc_datetime, timezone_str)
    if local_dt:
        return local_dt.strftime(format_str)
    return ''

def format_local_date(date_obj, format_str='%d/%m/%Y', timezone_str=None):
    """Format date/datetime object as local date string"""
    if not date_obj:
        return ''

    # If it's a date object (not datetime), format directly
    if hasattr(date_obj, 'strftime') and not hasattr(date_obj, 'tzinfo'):
        return date_obj.strftime(format_str)

    # If it's a datetime object, convert timezone first
    if hasattr(date_obj, 'tzinfo'):
        local_dt = convert_utc_to_local(date_obj, timezone_str)
        if local_dt:
            return local_dt.strftime(format_str)

    return ''

def split_client_emails(raw):
    """Split a stored multi-email string (separated by ; or ,) into individual addresses."""
    if not raw:
        return []
    import re
    return [a.strip() for a in re.split(r'[;,]', raw.strip().rstrip(';,')) if a.strip()]

def clean_note_text(text):
    """Clean note text by removing @ mentions and extra whitespace"""
    if not text:
        return ""

    import re
    lines = text.split('\n')
    cleaned_lines = []

    for line in lines:
        # Remove @ mentions completely
        line = re.sub(r'@\w+', '', line)
        # Clean up extra spaces
        line = ' '.join(line.split())
        if line:  # Only keep non-empty lines
            cleaned_lines.append(line)

    return '\n'.join(cleaned_lines)

def replace_email_variables(content, client, company, user=None, include_children=False):
    """Replace email template variables with actual values"""
    if not content:
        return content

    # Replace client variables (support both {var} and {{var}} formats)
    content = content.replace('{{client_name}}', client.name or '')
    content = content.replace('{client_name}', client.name or '')
    content = content.replace('{{client_code}}', client.code_client or '')
    content = content.replace('{client_code}', client.code_client or '')

    # Calculate total outstanding based on include_children filter
    if include_children and hasattr(client, 'is_parent') and client.is_parent:
        # Calculate consolidated total for parent + children
        total_outstanding = client.get_total_outstanding()
        if hasattr(client, 'child_clients'):
            for child in client.child_clients:
                total_outstanding += child.get_total_outstanding()
    else:
        # Only this client's outstanding
        total_outstanding = client.get_total_outstanding()

    # Format the amount with currency
    formatted_amount = format_currency(total_outstanding)
    content = content.replace('{{client_total_outstanding}}', formatted_amount)
    content = content.replace('{client_total_outstanding}', formatted_amount)

    # Replace company variables
    content = content.replace('{{company_name}}', company.name or '')
    content = content.replace('{company_name}', company.name or '')

    return content

# Import des fonctions Stripe depuis utils.py (fichier racine)
# Celles-ci ne causent pas de problèmes circulaires
try:
    import sys
    import os
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root_dir)

    from utils import _get_stripe_items_safely, get_item_price_id, get_item_quantity

except ImportError:
    # Fallback si import échoue
    def _get_stripe_items_safely(subscription):
        return []
    def get_item_price_id(item):
        return None
    def get_item_quantity(item):
        return 1

# Variables et constantes importantes
TRANSLATIONS = {
    'fr': {
        'statement_title': 'Liste des factures impayées',
        'aging_summary': 'Résumé',
        'invoice_date': 'Date facture',
        'due_date': 'Date échéance',
    }
}

# Les fonctions moins critiques peuvent rester dans le try/except
try:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Import de fonctions spécifiques depuis utils.py si possible
    import importlib.util
    utils_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utils.py")
    spec = importlib.util.spec_from_file_location("utils_orig", utils_file)

    if spec and spec.loader:
        utils_orig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(utils_orig)

        # Import des fonctions moins critiques avec signatures correctes
        def default_func(*args, **kwargs):
            return False
        def default_none_func(*args, **kwargs):
            return None
        def default_empty_list_func(*args, **kwargs):
            return []
        def default_password_func(*args, **kwargs):
            import secrets
            return secrets.token_urlsafe(16)
        def default_today_func(*args, **kwargs):
            from datetime import date
            return date.today()

        generate_statement_pdf_reportlab = getattr(utils_orig, 'generate_statement_pdf_reportlab', default_none_func)
        prepare_logo_cache = getattr(utils_orig, 'prepare_logo_cache', default_none_func)
        cleanup_logo_cache = getattr(utils_orig, 'cleanup_logo_cache', lambda x: None)
        get_local_today = getattr(utils_orig, 'get_local_today', default_today_func)
        company_has_original_amount = getattr(utils_orig, 'company_has_original_amount', default_func)
        send_password_reset_email = getattr(utils_orig, 'send_password_reset_email', default_func)
        _get_stripe_items_safely = getattr(utils_orig, '_get_stripe_items_safely', default_empty_list_func)
        convert_signature_images_to_base64 = getattr(utils_orig, 'convert_signature_images_to_base64', lambda x: x)
    else:
        # Si le spec échoue, définir directement les fonctions
        def generate_statement_pdf_reportlab(*args, **kwargs):
            return None
        def prepare_logo_cache(*args, **kwargs):
            return None
        def cleanup_logo_cache(*args, **kwargs):
            pass
        def get_local_today(*args, **kwargs):
            from datetime import date
            return date.today()
        def company_has_original_amount(*args, **kwargs):
            return False
        def send_password_reset_email(*args, **kwargs):
            return False
        def _get_stripe_items_safely(*args, **kwargs):
            return []
        def convert_signature_images_to_base64(html_content):
            return html_content

except Exception as e:
    # Fallback complet si les imports échouent
    print(f"ERREUR IMPORT UTILS AVANCÉES: {e}")
    def generate_statement_pdf_reportlab(*args, **kwargs):
        return None
    def prepare_logo_cache(*args, **kwargs):
        return None
    def cleanup_logo_cache(*args, **kwargs):
        pass
    def get_local_today(*args, **kwargs):
        from datetime import date
        return date.today()
    def company_has_original_amount(*args, **kwargs):
        return False
    def send_password_reset_email(*args, **kwargs):
        return False
    def _get_stripe_items_safely(*args, **kwargs):
        return []
    def convert_signature_images_to_base64(html_content):
        return html_content

__all__ = [
    # Secure logging functions
    'sanitize_email_for_logs',
    'sanitize_user_id_for_logs',
    'sanitize_company_id_for_logs',
    'sanitize_stripe_id_for_logs',
    'sanitize_sensitive_data_for_logs',
    'create_secure_log_message',
    'secure_log_function_call',
    # HTTP session functions
    'RobustHTTPSession',
    'get_robust_session',
    'retry_on_failure',
    'create_stripe_session',
    'create_microsoft_session',
    'create_quickbooks_session',
    # Currency and date functions
    'format_currency',
    'format_local_datetime',
    'format_local_date',
    'convert_utc_to_local',
    'convert_local_to_utc',
    'get_user_timezone',
    'get_local_now',
    # Text processing functions
    'split_client_emails',
    'clean_note_text',
    'replace_email_variables',
    'convert_signature_images_to_base64',
    # Access control functions
    'check_feature_access',
    # File handling functions
    'generate_statement_pdf_reportlab',
    'prepare_logo_cache',
    'cleanup_logo_cache',
    'get_local_today',
    'company_has_original_amount',
    'send_password_reset_email',
    # Internal functions
    '_get_stripe_items_safely',
    # Constants
    'TRANSLATIONS'
]