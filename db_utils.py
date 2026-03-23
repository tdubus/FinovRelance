import time
import logging
from functools import wraps
from sqlalchemy.exc import OperationalError, DisconnectionError
from psycopg2 import OperationalError as Psycopg2OperationalError

logger = logging.getLogger(__name__)


def retry_on_db_error(max_retries=2, delay=0.5):
    """
    Décorateur pour réessayer automatiquement les opérations DB en cas d'erreur de connexion.

    Gère les erreurs courantes de Neon/PostgreSQL:
    - SSL connection has been closed unexpectedly
    - Connection refused
    - Connection timed out

    Args:
        max_retries: Nombre maximum de tentatives (défaut: 2)
        delay: Délai entre les tentatives en secondes (défaut: 0.5)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, DisconnectionError, Psycopg2OperationalError) as e:
                    last_exception = e
                    error_msg = str(e).lower()

                    is_connection_error = any(msg in error_msg for msg in [
                        'ssl connection has been closed',
                        'connection refused',
                        'connection timed out',
                        'server closed the connection',
                        'connection reset',
                        'could not connect',
                        'connection terminated'
                    ])

                    if is_connection_error and attempt < max_retries:
                        logger.warning(
                            f"Erreur connexion DB dans {func.__name__}, "
                            f"tentative {attempt + 1}/{max_retries + 1}: {e}"
                        )
                        time.sleep(delay * (attempt + 1))
                        continue
                    else:
                        raise

            if last_exception:
                raise last_exception
            raise RuntimeError("Retry exhausted without exception")

        return wrapper
    return decorator


def safe_db_query(query_func, default=None, max_retries=2):
    """
    Exécute une requête DB de manière sécurisée avec retry et valeur par défaut.

    Usage:
        result = safe_db_query(lambda: User.query.get(1), default=None)

    Args:
        query_func: Fonction lambda contenant la requête
        default: Valeur à retourner en cas d'échec après tous les retries
        max_retries: Nombre maximum de tentatives

    Returns:
        Résultat de la requête ou la valeur par défaut
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return query_func()
        except (OperationalError, DisconnectionError, Psycopg2OperationalError) as e:
            last_exception = e
            error_msg = str(e).lower()

            is_connection_error = any(msg in error_msg for msg in [
                'ssl connection has been closed',
                'connection refused',
                'connection timed out',
                'server closed the connection',
                'connection reset',
                'could not connect',
                'connection terminated'
            ])

            if is_connection_error and attempt < max_retries:
                logger.warning(
                    f"Erreur connexion DB, tentative {attempt + 1}/{max_retries + 1}: {e}"
                )
                time.sleep(0.5 * (attempt + 1))
                continue
            else:
                logger.error(f"Échec requête DB après {max_retries + 1} tentatives: {e}")
                return default
        except Exception as e:
            logger.error(f"Erreur inattendue dans requête DB: {e}")
            return default

    return default
