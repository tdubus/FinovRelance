"""
FAÇADE UNIFIED - Réexporte le blueprint du nouveau gestionnaire
Pour maintenir la compatibilité avec app.py
"""

from stripe_finov.webhooks.handler import webhook_blueprint as unified_webhook_bp

# Export direct du blueprint pour compatibilité
__all__ = ['unified_webhook_bp']