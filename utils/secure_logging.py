"""
SÉCURITÉ ÉTAPE 9 - Logs Sécurisés
Sanitisation des informations sensibles dans les logs pour éviter les fuites de données
Conforme aux bonnes pratiques de sécurité et de confidentialité
"""
import hashlib
from typing import Optional, Union, Any

def sanitize_email_for_logs(email: Optional[str]) -> str:
    """
    Masque partiellement l'email pour les logs tout en gardant l'information utile

    Args:
        email: Email à sanitiser

    Returns:
        str: Email partiellement masqué (ex: jo***@example.com)

    Examples:
        >>> sanitize_email_for_logs("john.doe@example.com")
        "jo***@example.com"
        >>> sanitize_email_for_logs("a@test.com")
        "a@test.com"
        >>> sanitize_email_for_logs("invalid")
        "email_invalid"
    """
    if not email or not isinstance(email, str):
        return 'email_empty'

    if '@' not in email:
        return 'email_invalid'

    try:
        local, domain = email.split('@', 1)

        # Si l'email est très court, ne pas masquer
        if len(local) <= 2:
            return f"{local}@{domain}"

        # Masquer la partie locale en gardant les 2 premiers caractères
        return f"{local[:2]}***@{domain}"

    except Exception:
        return 'email_error'

def sanitize_user_id_for_logs(user_id: Union[int, str, None]) -> str:
    """
    Masque les IDs utilisateurs sensibles en gardant une référence unique

    Args:
        user_id: ID utilisateur à sanitiser

    Returns:
        str: ID masqué avec hash pour traçabilité (ex: user_a1b2c3d4)
    """
    if not user_id:
        return 'user_unknown'

    # Créer un hash court mais unique pour la traçabilité
    user_str = str(user_id)
    short_hash = hashlib.md5(user_str.encode()).hexdigest()[:8]
    return f"user_{short_hash}"

def sanitize_company_id_for_logs(company_id: Union[int, str, None]) -> str:
    """
    Masque les IDs d'entreprises sensibles

    Args:
        company_id: ID entreprise à sanitiser

    Returns:
        str: ID masqué avec hash pour traçabilité (ex: company_x7y8z9a0)
    """
    if not company_id:
        return 'company_unknown'

    # Créer un hash court mais unique pour la traçabilité
    company_str = str(company_id)
    short_hash = hashlib.md5(company_str.encode()).hexdigest()[:8]
    return f"company_{short_hash}"

def sanitize_stripe_id_for_logs(stripe_id: Optional[str]) -> str:
    """
    Masque les IDs Stripe tout en gardant le préfixe pour identification

    Args:
        stripe_id: ID Stripe à masquer (ex: cus_1234567890)

    Returns:
        str: ID masqué (ex: cus_***4567890)
    """
    if not stripe_id or not isinstance(stripe_id, str):
        return 'stripe_invalid'

    # Les IDs Stripe ont un format préfixe_identifiant
    if '_' not in stripe_id or len(stripe_id) < 10:
        return 'stripe_short'

    try:
        prefix, identifier = stripe_id.split('_', 1)

        if len(identifier) <= 6:
            # ID court, masquer partiellement
            return f"{prefix}_***{identifier[-3:]}"
        else:
            # ID long, masquer le milieu
            return f"{prefix}_***{identifier[-6:]}"

    except Exception:
        return 'stripe_error'

def sanitize_sensitive_data_for_logs(data: Any, field_name: str = "") -> str:
    """
    Sanitise automatiquement différents types de données sensibles

    Args:
        data: Donnée à sanitiser
        field_name: Nom du champ pour détection automatique

    Returns:
        str: Donnée sanitisée selon le type détecté
    """
    if data is None:
        return f"{field_name}_none" if field_name else "none"

    data_str = str(data)
    field_lower = field_name.lower()

    # Détection automatique du type de donnée sensible
    if 'email' in field_lower or '@' in data_str:
        return sanitize_email_for_logs(data_str)

    elif 'user' in field_lower and 'id' in field_lower:
        return sanitize_user_id_for_logs(data)

    elif 'company' in field_lower and 'id' in field_lower:
        return sanitize_company_id_for_logs(data)

    elif 'stripe' in field_lower or data_str.startswith(('cus_', 'sub_', 'pi_', 'pm_')):
        return sanitize_stripe_id_for_logs(data_str)

    elif 'password' in field_lower or 'secret' in field_lower or 'token' in field_lower:
        return '***REDACTED***'

    elif 'phone' in field_lower and len(data_str) > 6:
        # Masquer les numéros de téléphone
        return f"***-{data_str[-4:]}" if len(data_str) >= 4 else "phone_short"

    else:
        # Par défaut, limiter la longueur des données pour éviter les gros logs
        from constants import LOG_MAX_LENGTH, LOG_TRUNCATE_LENGTH
        if len(data_str) > LOG_MAX_LENGTH:
            return f"{data_str[:LOG_TRUNCATE_LENGTH]}...[truncated]"
        return data_str

def create_secure_log_message(message: str, **sensitive_data) -> str:
    """
    Crée un message de log sécurisé en sanitisant automatiquement les données sensibles

    Args:
        message: Message de base
        **sensitive_data: Données sensibles à inclure dans le log

    Returns:
        str: Message de log sécurisé

    Examples:
        >>> create_secure_log_message(
        ...     "User login attempt",
        ...     user_email="john@example.com",
        ...     user_id=123,
        ...     company_id=456
        ... )
        "User login attempt - user_email: jo***@example.com, user_id: user_a665a45e, company_id: company_c9f0f895"
    """
    if not sensitive_data:
        return message

    sanitized_parts = []
    for key, value in sensitive_data.items():
        sanitized_value = sanitize_sensitive_data_for_logs(value, key)
        sanitized_parts.append(f"{key}: {sanitized_value}")

    return f"{message} - {', '.join(sanitized_parts)}"

# Décorateur pour logging automatique sécurisé
def secure_log_function_call(logger):
    """
    Décorateur pour logger automatiquement les appels de fonction avec données sanitisées

    Args:
        logger: Logger à utiliser
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Sanitiser les arguments pour le log
            sanitized_kwargs = {
                k: sanitize_sensitive_data_for_logs(v, k)
                for k, v in kwargs.items()
                if not k.startswith('_')  # Ignorer les paramètres privés
            }

            logger.info(f"Calling {func.__name__} with args: {sanitized_kwargs}")

            try:
                result = func(*args, **kwargs)
                logger.info(f"{func.__name__} completed successfully")
                return result
            except Exception as e:
                logger.error(f"{func.__name__} failed: {str(e)}")
                raise

        return wrapper
    return decorator

# Constantes pour les types de logs sécurisés
LOG_LEVEL_SECURE_INFO = "SECURE_INFO"
LOG_LEVEL_SECURE_WARNING = "SECURE_WARNING"
LOG_LEVEL_SECURE_ERROR = "SECURE_ERROR"