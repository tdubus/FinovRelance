"""
Cache temporaire partage pour les PDF de factures a joindre aux courriels.

Utilise Flask-Caching avec backend Redis en production pour partager le cache
entre les workers Gunicorn. Sans Redis (dev local), fallback sur SimpleCache
qui reste per-process — acceptable car le dev tourne en general avec 1 worker.

Cle Redis : pdf_temp:{user_id}:{invoice_id}
Valeur : dict {bytes, filename, company_id}
TTL : 30 minutes (gere automatiquement par Redis)

IMPORTANT : ne jamais utiliser un dict en memoire pour ce cache. Avec plusieurs
workers Gunicorn, chaque worker a sa propre memoire et le PDF est perdu si
le prefetch et le send tombent sur des workers differents.
"""
from typing import Optional, Dict, Any

PDF_CACHE_TTL = 1800  # 30 minutes en secondes


def _key(user_id: int, invoice_id: int) -> str:
    return f"pdf_temp:{user_id}:{invoice_id}"


def set_pdf(user_id: int, invoice_id: int, pdf_bytes: bytes, filename: str, company_id: int) -> None:
    """Stocke un PDF de facture dans le cache partage entre workers."""
    from app import cache
    cache.set(
        _key(user_id, invoice_id),
        {
            'bytes': pdf_bytes,
            'filename': filename,
            'company_id': company_id,
        },
        timeout=PDF_CACHE_TTL
    )


def get_pdf(user_id: int, invoice_id: int) -> Optional[Dict[str, Any]]:
    """Recupere un PDF du cache. Retourne None si absent ou expire."""
    from app import cache
    return cache.get(_key(user_id, invoice_id))


def delete_pdf(user_id: int, invoice_id: int) -> None:
    """Supprime un PDF du cache (a appeler apres envoi du courriel)."""
    from app import cache
    cache.delete(_key(user_id, invoice_id))
