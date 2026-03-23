"""
Helpers pour la gestion des consentements RGPD/Loi 25
Enregistre les consentements utilisateur dans la base de données
"""

from flask import request
from app import db
from models import ConsentLog
from datetime import datetime


# Versions des documents légaux
CURRENT_TERMS_VERSION = "2025-12-29"  # Section 3.3 Abonnements, Section 7 Limitation renforcée, Section 8 Suspension
CURRENT_PRIVACY_VERSION = "2025-12-29"  # Stripe paiements, conservation spécifique, transferts internationaux, notification violations
CURRENT_COOKIES_VERSION = "2025-12-29"  # Cloudflare cookies, détails techniques, outils diagnostic


def get_client_ip():
    """Récupère l'adresse IP du client de manière sécurisée"""
    # Si derrière Cloudflare, utiliser CF-Connecting-IP
    if request.headers.get('CF-Connecting-IP'):
        return request.headers.get('CF-Connecting-IP')

    # Sinon utiliser X-Forwarded-For si disponible
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()

    # Sinon utiliser l'IP directe
    return request.remote_addr


def get_user_agent():
    """Récupère le User-Agent du client"""
    return request.headers.get('User-Agent', '')


def log_consent(user_id, consent_type, accepted, version=None, ip_address=None, user_agent=None):
    """
    Enregistre un consentement dans la base de données

    Args:
        user_id (int): ID de l'utilisateur (None pour anonyme)
        consent_type (str): Type de consentement ('terms', 'privacy', 'cookies')
        accepted (bool): True si accepté, False si refusé
        version (str): Version du document (optionnel, utilise la version actuelle par défaut)
        ip_address (str): Adresse IP (optionnel, détecté automatiquement)
        user_agent (str): User-Agent (optionnel, détecté automatiquement)

    Returns:
        ConsentLog: L'enregistrement de consentement créé
    """
    # Déterminer la version si non fournie
    if not version:
        if consent_type == 'terms':
            version = CURRENT_TERMS_VERSION
        elif consent_type == 'privacy':
            version = CURRENT_PRIVACY_VERSION
        elif consent_type == 'cookies':
            version = CURRENT_COOKIES_VERSION
        else:
            version = datetime.utcnow().strftime('%Y-%m-%d')

    # Déterminer l'IP et le User-Agent si non fournis
    if ip_address is None:
        ip_address = get_client_ip()

    if user_agent is None:
        user_agent = get_user_agent()

    # Créer l'enregistrement
    consent = ConsentLog(
        user_id=user_id,
        consent_type=consent_type,
        consent_version=version,
        accepted=accepted,
        ip_address=ip_address,
        user_agent=user_agent,
        created_at=datetime.utcnow()
    )

    # Ne pas commit ici - laisser l'appelant gerer la transaction
    # pour eviter les commits partiels en cas d'erreur dans le flux appelant
    db.session.add(consent)

    return consent


def log_terms_consent(user_id, accepted=True):
    """Enregistre le consentement aux CGU"""
    return log_consent(user_id, 'terms', accepted, CURRENT_TERMS_VERSION)


def log_privacy_consent(user_id, accepted=True):
    """Enregistre le consentement à la politique de confidentialité"""
    return log_consent(user_id, 'privacy', accepted, CURRENT_PRIVACY_VERSION)


def log_cookies_consent(user_id, accepted):
    """Enregistre le consentement aux cookies (peut être accepté ou refusé)"""
    return log_consent(user_id, 'cookies', accepted, CURRENT_COOKIES_VERSION)


def check_user_needs_new_consent(user_id, consent_type):
    """
    Vérifie si l'utilisateur doit renouveler son consentement
    (si une nouvelle version des documents est disponible)

    Args:
        user_id (int): ID de l'utilisateur
        consent_type (str): Type de consentement à vérifier

    Returns:
        bool: True si un nouveau consentement est nécessaire
    """
    # Récupérer le dernier consentement
    latest = ConsentLog.get_user_latest_consent(user_id, consent_type)

    if not latest or not latest.accepted:
        return True  # Pas de consentement ou refusé = besoin de consentir

    # Vérifier la version
    if consent_type == 'terms':
        return latest.consent_version < CURRENT_TERMS_VERSION
    elif consent_type == 'privacy':
        return latest.consent_version < CURRENT_PRIVACY_VERSION
    elif consent_type == 'cookies':
        return latest.consent_version < CURRENT_COOKIES_VERSION

    return False


def get_user_consent_status(user_id):
    """
    Récupère le statut de tous les consentements d'un utilisateur

    Args:
        user_id (int): ID de l'utilisateur

    Returns:
        dict: Statut des consentements
    """
    return {
        'terms': {
            'consented': ConsentLog.has_user_consented(user_id, 'terms', CURRENT_TERMS_VERSION),
            'needs_update': check_user_needs_new_consent(user_id, 'terms'),
            'current_version': CURRENT_TERMS_VERSION
        },
        'privacy': {
            'consented': ConsentLog.has_user_consented(user_id, 'privacy', CURRENT_PRIVACY_VERSION),
            'needs_update': check_user_needs_new_consent(user_id, 'privacy'),
            'current_version': CURRENT_PRIVACY_VERSION
        },
        'cookies': {
            'consented': ConsentLog.has_user_consented(user_id, 'cookies', CURRENT_COOKIES_VERSION),
            'needs_update': check_user_needs_new_consent(user_id, 'cookies'),
            'current_version': CURRENT_COOKIES_VERSION
        }
    }
