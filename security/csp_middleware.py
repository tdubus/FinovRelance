"""
Content Security Policy (CSP) Middleware
Protection contre les attaques XSS, clickjacking et injection de code.

Note: Cette implémentation utilise 'unsafe-inline' pour les scripts car l'application
existante contient de nombreux scripts inline dans les templates. Une migration vers
des nonces nécessiterait une refactorisation majeure de tous les templates.

Protection fournie malgré 'unsafe-inline':
- Restriction des sources de scripts aux CDN de confiance uniquement
- Protection clickjacking via frame-ancestors et X-Frame-Options
- HSTS en production pour forcer HTTPS
- X-Content-Type-Options pour prévenir le MIME sniffing
- Restriction des connexions aux APIs autorisées uniquement
"""

import os
import secrets
from constants import is_production


# TODO: utiliser lors de la migration vers CSP nonces
def generate_nonce():
    """Génère un nonce aléatoire pour les scripts inline autorisés."""
    return secrets.token_urlsafe(16)


def get_csp_directives(nonce=None):
    """
    Retourne les directives CSP configurées pour l'application.

    Args:
        nonce: Nonce optionnel pour autoriser des scripts inline spécifiques

    Returns:
        dict: Dictionnaire des directives CSP

    Note: 'unsafe-inline' est requis car l'application utilise des scripts inline
    dans les templates (tooltips Bootstrap, notifications, etc.). Une migration
    vers des nonces est recommandée pour une sécurité maximale.
    """
    nonce_directive = f"'nonce-{nonce}'" if nonce else ""

    directives = {
        'default-src': ["'self'"],

        'script-src': [
            "'self'",
            "https://cdn.jsdelivr.net",
            "https://cdnjs.cloudflare.com",
            "https://cdn.quilljs.com",
            "https://js.stripe.com",
            "https://static.cloudflareinsights.com",
            "https://www.googletagmanager.com",
            "https://cdn.visitors.now",
            "'unsafe-inline'",
        ],

        'style-src': [
            "'self'",
            "https://cdn.jsdelivr.net",
            "https://cdnjs.cloudflare.com",
            "https://cdn.quilljs.com",
            "'unsafe-inline'",
        ],

        'font-src': [
            "'self'",
            "https://cdnjs.cloudflare.com",
            "data:",
        ],

        'img-src': [
            "'self'",
            "data:",
            "https:",
            "blob:",
        ],

        'connect-src': [
            "'self'",
            "https://cdn.jsdelivr.net",
            "https://cdn.quilljs.com",
            "https://api.stripe.com",
            "https://login.microsoftonline.com",
            "https://graph.microsoft.com",
            "https://www.googletagmanager.com",
            "https://www.google-analytics.com",
            "https://stats.g.doubleclick.net",
            "https://region1.google-analytics.com",
            "https://cdn.visitors.now",
            "https://*.visitors.now",
        ],

        'frame-src': [
            "'self'",
            "https://js.stripe.com",
            "https://hooks.stripe.com",
            "https://www.youtube.com",
            "https://www.youtube-nocookie.com",
            "https://player.vimeo.com",
        ],

        'frame-ancestors': ["'self'"],

        'form-action': ["'self'"],

        'base-uri': ["'self'"],

        'object-src': ["'none'"],
    }

    # Seulement en production : forcer HTTPS via CSP
    if is_production():
        directives['upgrade-insecure-requests'] = []

    return directives


def build_csp_header(directives):
    """
    Construit la chaîne d'en-tête CSP à partir des directives.

    Args:
        directives: Dictionnaire des directives CSP

    Returns:
        str: Chaîne CSP formatée pour l'en-tête HTTP
    """
    parts = []
    for directive, values in directives.items():
        if values:
            parts.append(f"{directive} {' '.join(values)}")
        else:
            parts.append(directive)

    return "; ".join(parts)


def get_security_headers(include_hsts=False):
    """
    Retourne tous les en-têtes de sécurité recommandés.

    Args:
        include_hsts: Si True, ajoute l'en-tête HSTS (pour production HTTPS uniquement)

    Returns:
        dict: Dictionnaire des en-têtes de sécurité
    """
    csp_directives = get_csp_directives()
    csp_header = build_csp_header(csp_directives)

    headers = {
        'Content-Security-Policy': csp_header,

        'X-Content-Type-Options': 'nosniff',

        'X-Frame-Options': 'SAMEORIGIN',

        'X-XSS-Protection': '1; mode=block',

        'Referrer-Policy': 'strict-origin-when-cross-origin',

        'Permissions-Policy': 'geolocation=(), microphone=(), camera=()',
    }

    if include_hsts:
        headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    return headers


def init_security_headers(app):
    """
    Initialise le middleware de sécurité pour l'application Flask.
    Ajoute automatiquement les en-têtes de sécurité à toutes les réponses.

    Args:
        app: Instance de l'application Flask

    En-têtes ajoutés:
    - Content-Security-Policy: Contrôle les sources autorisées
    - X-Content-Type-Options: Prévient le MIME sniffing
    - X-Frame-Options: Protection clickjacking
    - X-XSS-Protection: Protection XSS navigateur
    - Referrer-Policy: Contrôle les informations de référent
    - Permissions-Policy: Désactive les fonctionnalités sensibles
    - Strict-Transport-Security: Force HTTPS (production uniquement)
    """

    production_mode = is_production()

    @app.after_request
    def add_security_headers(response):
        """Ajoute les en-têtes de sécurité à chaque réponse."""

        if response.content_type and 'text/html' in response.content_type:
            headers = get_security_headers(include_hsts=production_mode)
            for header, value in headers.items():
                response.headers[header] = value
        else:
            response.headers['X-Content-Type-Options'] = 'nosniff'

        return response

    mode = "PRODUCTION (HSTS activé)" if production_mode else "DÉVELOPPEMENT"
    app.logger.info(f"✅ En-têtes de sécurité CSP activés - Mode: {mode}")

    return app
