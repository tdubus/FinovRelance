"""
Module de logging sécurisé pour les webhooks Stripe
Assure que les informations sensibles ne sont pas exposées dans les logs
"""

def create_secure_log_message(message, **kwargs):
    """
    Crée un message de log sécurisé sans exposer d'informations sensibles

    Args:
        message: Message principal du log
        **kwargs: Informations additionnelles à inclure (signature_present, signature_length, etc.)

    Returns:
        str: Message de log formaté de manière sécurisée
    """
    safe_parts = [message]

    if 'signature_present' in kwargs:
        safe_parts.append(f"signature_present={kwargs['signature_present']}")

    if 'signature_length' in kwargs:
        safe_parts.append(f"signature_length={kwargs['signature_length']}")

    if 'event_type' in kwargs:
        safe_parts.append(f"event_type={kwargs['event_type']}")

    if 'event_id' in kwargs:
        safe_parts.append(f"event_id={kwargs['event_id']}")

    return " | ".join(safe_parts)