"""
SÉCURITÉ ÉTAPE 8 - Session HTTP Robuste pour Appels API Externes
Implémente les timeouts, retry avec backoff et gestion d'erreurs améliorée
pour tous les appels vers services externes (Stripe, Microsoft, QuickBooks)
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
from typing import Optional
from functools import wraps
import time
from constants import HTTP_TIMEOUT_DEFAULT, MAX_RETRY_ATTEMPTS, HTTP_RETRY_STATUS_CODES

logger = logging.getLogger(__name__)

class RobustHTTPSession:
    """
    Session HTTP robuste avec retry stratégique, timeouts et gestion d'erreurs
    Conforme aux bonnes pratiques de sécurité pour appels API externes
    """

    def __init__(self,
                 timeout: int = HTTP_TIMEOUT_DEFAULT,
                 max_retries: int = MAX_RETRY_ATTEMPTS,
                 backoff_factor: float = 1,
                 status_forcelist: Optional[list] = None):
        """
        Initialise la session robuste

        Args:
            timeout: Timeout en secondes (défaut: 30s)
            max_retries: Nombre maximum de retry (défaut: 3)
            backoff_factor: Facteur de backoff exponentiel (défaut: 1)
            status_forcelist: Codes HTTP à retry (défaut: erreurs serveur + rate limit)
        """
        if status_forcelist is None:
            status_forcelist = HTTP_RETRY_STATUS_CODES

        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.status_forcelist = status_forcelist

        self._session = self._create_session()

        # Session créée silencieusement (performance)

    def _create_session(self) -> requests.Session:
        """Crée une session requests avec retry stratégique"""
        session = requests.Session()

        # Configuration retry avec backoff exponentiel
        retry_strategy = Retry(
            total=self.max_retries,
            read=self.max_retries,
            connect=self.max_retries,
            backoff_factor=self.backoff_factor,
            status_forcelist=self.status_forcelist,
            # Ne pas retry sur les méthodes qui peuvent causer des effets de bord
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            raise_on_status=False  # Gérer les statuts dans notre code
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Effectue une requête HTTP robuste avec gestion d'erreurs complète

        Args:
            method: Méthode HTTP (GET, POST, etc.)
            url: URL de destination
            **kwargs: Paramètres additionnels pour requests

        Returns:
            requests.Response: Réponse HTTP

        Raises:
            requests.Timeout: Timeout dépassé
            requests.ConnectionError: Erreur de connexion
            requests.HTTPError: Erreur HTTP (4xx, 5xx)
            Exception: Autres erreurs réseau
        """
        # Appliquer le timeout par défaut si non spécifié
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.timeout

        # Sanitiser l'URL pour les logs (masquer les tokens/secrets)
        safe_url = self._sanitize_url_for_logs(url)

        try:
            logger.debug(f"Requête HTTP {method} vers {safe_url} (timeout: {kwargs['timeout']}s)")

            start_time = time.time()
            response = self._session.request(method, url, **kwargs)
            duration = time.time() - start_time

            # Log uniquement les erreurs (performance)
            if response.status_code >= 400:
                logger.warning(f"HTTP {method} {safe_url} -> {response.status_code} ({duration:.2f}s)")

            # Déclencher l'exception pour les codes d'erreur HTTP
            response.raise_for_status()

            return response

        except requests.Timeout as e:
            logger.error(f"Timeout {method} {safe_url} après {self.timeout}s")
            raise requests.Timeout(f"Timeout après {self.timeout}s pour {safe_url}")

        except requests.ConnectionError as e:
            logger.error(f"Erreur connexion {method} {safe_url}")
            raise requests.ConnectionError(f"Impossible de se connecter à {safe_url}")

        except requests.HTTPError as e:
            logger.error(f"Erreur HTTP {method} {safe_url}: {e.response.status_code}")
            raise requests.HTTPError(f"Erreur HTTP {e.response.status_code} pour {safe_url}")

        except Exception as e:
            logger.error(f"Erreur inattendue {method} {safe_url}: {str(e)}")
            raise RuntimeError(f"Erreur réseau inattendue pour {safe_url}: {str(e)}")

    def get(self, url: str, **kwargs) -> requests.Response:
        """Requête GET robuste"""
        return self.request('GET', url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """Requête POST robuste"""
        return self.request('POST', url, **kwargs)

    def put(self, url: str, **kwargs) -> requests.Response:
        """Requête PUT robuste"""
        return self.request('PUT', url, **kwargs)

    def delete(self, url: str, **kwargs) -> requests.Response:
        """Requête DELETE robuste"""
        return self.request('DELETE', url, **kwargs)

    def _sanitize_url_for_logs(self, url: str) -> str:
        """
        Sanitise l'URL pour les logs en masquant les informations sensibles
        """
        # Masquer les tokens, API keys, secrets dans l'URL
        import re

        # Masquer les paramètres sensibles courants
        sensitive_patterns = [
            (r'(token|key|secret|password|auth)=([^&\s]+)', r'\1=***'),
            (r'(access_token|refresh_token)=([^&\s]+)', r'\1=***'),
            (r'/([a-zA-Z0-9]{20,})', r'/***')  # Masquer les IDs/tokens longs
        ]

        safe_url = url
        for pattern, replacement in sensitive_patterns:
            safe_url = re.sub(pattern, replacement, safe_url, flags=re.IGNORECASE)

        return safe_url

    def close(self):
        """Ferme la session proprement"""
        if self._session:
            self._session.close()
            logger.debug("Session HTTP robuste fermée")


# Instance globale pour utilisation dans toute l'application
_default_session = None

def get_robust_session(**kwargs) -> RobustHTTPSession:
    """
    Retourne une session HTTP robuste (singleton par défaut)

    Args:
        **kwargs: Paramètres de configuration (timeout, max_retries, etc.)

    Returns:
        RobustHTTPSession: Instance de session robuste
    """
    global _default_session

    # Si des paramètres spécifiques sont demandés, créer une nouvelle instance
    if kwargs:
        return RobustHTTPSession(**kwargs)

    # Sinon, retourner l'instance par défaut (singleton)
    if _default_session is None:
        _default_session = RobustHTTPSession()

    return _default_session

# Décorateur pour retry automatique sur les fonctions
def retry_on_failure(max_retries: int = 3, delay: float = 1, backoff_factor: float = 2):
    """
    Décorateur pour retry automatique sur les fonctions qui peuvent échouer

    Args:
        max_retries: Nombre maximum de retry
        delay: Délai initial en secondes
        backoff_factor: Facteur multiplicateur du délai
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    if attempt < max_retries:
                        logger.warning(f"Tentative {attempt + 1}/{max_retries + 1} échouée pour {func.__name__}: {str(e)}")
                        logger.info(f"Retry dans {current_delay:.1f}s...")
                        time.sleep(current_delay)
                        current_delay *= backoff_factor
                    else:
                        logger.error(f"Fonction {func.__name__} échoue après {max_retries + 1} tentatives")

            if last_exception:
                raise last_exception
            else:
                raise RuntimeError(f"Fonction {func.__name__} a échoué sans exception spécifique")
        return wrapper
    return decorator

# Helper pour créer des sessions spécialisées par service
def create_stripe_session() -> RobustHTTPSession:
    """Session optimisée pour l'API Stripe"""
    return RobustHTTPSession(
        timeout=45,  # Stripe peut être lent
        max_retries=2,  # Pas trop de retry pour éviter la double facturation
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]
    )

def create_microsoft_session() -> RobustHTTPSession:
    """Session optimisée pour Microsoft Graph API"""
    return RobustHTTPSession(
        timeout=60,  # Microsoft Graph peut être lent pour gros emails
        max_retries=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]
    )

def create_quickbooks_session() -> RobustHTTPSession:
    """Session optimisée pour QuickBooks API"""
    return RobustHTTPSession(
        timeout=30,
        max_retries=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504]
    )

def create_secure_session() -> RobustHTTPSession:
    """Session sécurisée générale pour APIs externes avec sécurité renforcée"""
    return RobustHTTPSession(
        timeout=45,  # Timeout adapté pour APIs externes
        max_retries=3,  # Retry standard pour robustesse
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]
    )

class ReadOnlyBusinessCentralSession(RobustHTTPSession):
    """
    Session HTTP READ-ONLY pour Business Central

    🔒 SÉCURITÉ: Bloque explicitement toute opération d'écriture (POST/PUT/PATCH/DELETE)
    pour garantir que le connecteur Business Central ne peut QUE lire les données.

    Cette protection empêche toute modification accidentelle ou non autorisée des données
    dans Business Central, même si du code tente d'effectuer des écritures.

    Méthodes autorisées : GET, HEAD, OPTIONS (lecture seule)
    Méthodes bloquées : POST, PUT, PATCH, DELETE (écriture)
    """

    # Liste des méthodes HTTP autorisées (lecture seule)
    ALLOWED_METHODS = {'GET', 'HEAD', 'OPTIONS'}

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Override de request() pour bloquer les méthodes d'écriture

        Cette protection au niveau de request() empêche tout bypass via
        session.request('POST', ...) et garantit la sécurité read-only
        """
        method_upper = method.upper()

        if method_upper not in self.ALLOWED_METHODS:
            raise PermissionError(
                f"❌ OPÉRATION INTERDITE: {method_upper} non autorisé sur Business Central. "
                f"Ce connecteur est configuré en LECTURE SEULE (read-only). "
                f"Méthodes autorisées : {', '.join(sorted(self.ALLOWED_METHODS))}. "
                f"Les opérations d'écriture sont explicitement bloquées pour des raisons de sécurité."
            )

        # Si méthode autorisée, déléguer à la classe parent
        return super().request(method, url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """Bloque POST - opération interdite pour Business Central"""
        raise PermissionError(
            "❌ OPÉRATION INTERDITE: POST non autorisé sur Business Central. "
            "Ce connecteur est configuré en LECTURE SEULE (read-only)."
        )

    def put(self, url: str, **kwargs) -> requests.Response:
        """Bloque PUT - opération interdite pour Business Central"""
        raise PermissionError(
            "❌ OPÉRATION INTERDITE: PUT non autorisé sur Business Central. "
            "Ce connecteur est configuré en LECTURE SEULE (read-only)."
        )

    def patch(self, url: str, **kwargs) -> requests.Response:
        """Bloque PATCH - opération interdite pour Business Central"""
        raise PermissionError(
            "❌ OPÉRATION INTERDITE: PATCH non autorisé sur Business Central. "
            "Ce connecteur est configuré en LECTURE SEULE (read-only)."
        )

    def delete(self, url: str, **kwargs) -> requests.Response:
        """Bloque DELETE - opération interdite pour Business Central"""
        raise PermissionError(
            "❌ OPÉRATION INTERDITE: DELETE non autorisé sur Business Central. "
            "Ce connecteur est configuré en LECTURE SEULE (read-only)."
        )

def create_business_central_session() -> ReadOnlyBusinessCentralSession:
    """
    Session optimisée et sécurisée pour Microsoft Business Central API

    Retourne une session READ-ONLY qui:
    - ✅ Autorise GET et HEAD (lecture)
    - ❌ Bloque POST, PUT, PATCH, DELETE (écriture)
    - 🔒 Garantit que le connecteur ne peut pas modifier les données BC
    """
    return ReadOnlyBusinessCentralSession(
        timeout=60,  # Business Central peut être lent pour gros datasets
        max_retries=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]
    )

def create_xero_session() -> RobustHTTPSession:
    """Session optimisée pour Xero Accounting API"""
    return RobustHTTPSession(
        timeout=30,  # Xero API standard timeout
        max_retries=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504]  # Rate limiting + server errors
    )

def create_pennylane_session() -> RobustHTTPSession:
    """Session optimisée pour Pennylane API v2

    Rate limit: 25 requêtes par fenêtre de 5 secondes.
    Backoff factor plus agressif pour respecter le rate limit strict.
    """
    return RobustHTTPSession(
        timeout=30,
        max_retries=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]
    )

def create_odoo_session() -> RobustHTTPSession:
    """Session optimisée pour Odoo XML-RPC API

    Note: Odoo utilise XML-RPC qui peut avoir des temps de réponse plus longs
    pour les opérations complexes, d'où un timeout plus élevé.
    """
    return RobustHTTPSession(
        timeout=60,  # Timeout élevé pour les requêtes XML-RPC complexes
        max_retries=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504]  # Rate limiting + server errors
    )