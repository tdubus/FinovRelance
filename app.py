"FICHIER NETTOYÉ LE 2025-12-31"
import sys
import os
import json
import logging

# Augmenter la limite de récursion pour éviter les crashs SQLAlchemy avec Gunicorn --preload
sys.setrecursionlimit(3000)
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_compress import Compress
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix

# Configure logging - adapté selon l'environnement
is_production = os.environ.get('FLASK_ENV', 'production') == 'production'
is_dev_mode = os.environ.get('DEV_MODE', 'false').lower() == 'true'
log_level = logging.INFO if is_production else logging.DEBUG


class JSONLogFormatter(logging.Formatter):
    """Formatter JSON pour logs structurés en production.
    Inclut timestamp, level, message, module et tout champ extra."""

    def format(self, record):
        log_entry = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'message': record.getMessage(),
            'module': record.module,
            'name': record.name,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry['exception'] = self.formatException(record.exc_info)
        # Ajouter les extra fields passes au logger (ex: logger.info("msg", extra={...}))
        standard_attrs = {
            'name', 'msg', 'args', 'created', 'relativeCreated', 'exc_info',
            'exc_text', 'stack_info', 'lineno', 'funcName', 'pathname',
            'filename', 'module', 'thread', 'threadName', 'process',
            'processName', 'levelname', 'levelno', 'message', 'msecs',
            'taskName',
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith('_'):
                log_entry[key] = value
        return json.dumps(log_entry, default=str, ensure_ascii=False)


# En production (pas DEV_MODE), utiliser le formatter JSON
if not is_dev_mode:
    _json_handler = logging.StreamHandler()
    _json_handler.setFormatter(JSONLogFormatter())
    logging.root.handlers = []
    logging.root.addHandler(_json_handler)
    logging.root.setLevel(log_level)
else:
    # En dev, garder le format texte lisible
    logging.basicConfig(level=log_level)

# Réduire le bruit des librairies externes (PIL, urllib3, etc.)
logging.getLogger('PIL').setLevel(logging.WARNING)
logging.getLogger('PIL.PngImagePlugin').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.INFO)


class Base(DeclarativeBase):
    pass


# Initialize extensions
db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
csrf = CSRFProtect()

# Rate Limiting - Protection contre les attaques par force brute
# BONNES PRATIQUES:
# - Limites par défaut très élevées pour ne pas bloquer les utilisateurs normaux
# - Les endpoints sensibles (login, reset password) ont leurs propres limites strictes définies explicitement
# - Les endpoints API internes sont exemptés car protégés par @login_required
# NOTE: "memory://" stocke les compteurs en mémoire du processus. Avec plusieurs workers
# gunicorn, chaque worker a ses propres compteurs, ce qui rend les limites moins strictes
# (multipliées par le nombre de workers). Pour des limites exactes en multi-worker,
# utiliser Redis comme backend de stockage.
_rate_limit_storage = os.environ.get("REDIS_URL", "memory://")
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["5000 per day", "500 per hour"],
    storage_uri=_rate_limit_storage
)

# Flask-Caching - Redis si disponible, SimpleCache sinon (dev sans Redis)
_redis_url = os.environ.get("REDIS_URL")
if _redis_url:
    _cache_config = {
        'CACHE_TYPE': 'RedisCache',
        'CACHE_REDIS_URL': _redis_url,
        'CACHE_DEFAULT_TIMEOUT': 300,  # 5 minutes TTL par defaut
    }
else:
    _cache_config = {
        'CACHE_TYPE': 'SimpleCache',
        'CACHE_DEFAULT_TIMEOUT': 300,
    }
cache = Cache()

# create the app
app = Flask(__name__)

# Enable gzip compression for responses
Compress(app)

# Configuration sécurisée - Clé secrète obligatoire
secret_key = os.environ.get("SESSION_SECRET")
if not secret_key:
    raise RuntimeError("SESSION_SECRET environment variable must be set")
app.secret_key = secret_key
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Cache temporaire en mémoire pour les PDF de factures à joindre aux courriels.
# Clé: (user_id, invoice_id) → {bytes, filename, expires: datetime}
# Vidé après envoi ; jamais persisté en base de données.
app.pdf_temp_cache = {}

# Configuration de sécurité - HTTPS en production
app.config['SESSION_COOKIE_SECURE'] = is_production
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Protection XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Protection CSRF
app.config[
    'PERMANENT_SESSION_LIFETIME'] = 7200  # Session de 2 heures pour inscription

# Configuration pour améliorer la stabilité des connexions
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 2592000  # Cache statique 30 jours (cache bust par filtre asset_url)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# Mode développeur pour contourner les problèmes de connexion réseau
app.config['DEV_MODE'] = os.environ.get(
    'DEV_MODE', 'false').lower() == 'true'  # Désactivé par défaut en production

# Configuration pour signatures volumineuses (Fix Request Entity Too Large)
app.config[
    'MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB pour guides avec images/vidéos Base64 + signatures

# Database configuration - utilise DATABASE_URL dans tous les environnements
database_url = os.environ.get("DATABASE_URL", "sqlite:///finova_ar.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
if is_production:
    print("PRODUCTION: Utilisation de la base de donnees de production")
else:
    print("DEVELOPPEMENT: Utilisation de la base de donnees de developpement")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 180,  # Recycler les connexions après 3 min pour éviter les connexions expirées
    "pool_pre_ping": True,  # Vérifier les connexions avant utilisation
    "pool_size": 5,  # Réduire pour éviter les connexions inactives
    "max_overflow": 10,  # Connexions supplémentaires en cas de pic
    "pool_timeout": 30,  # Timeout pour obtenir une connexion du pool
    "pool_reset_on_return": "rollback",  # Rollback propre des transactions orphelines
    "connect_args": {
        "connect_timeout": 30,  # Timeout de connexion initial
        "application_name": "finov_ar_app",
        "options": "-c statement_timeout=120000",  # Timeout requêtes: 2 minutes
        "keepalives": 1,  # Activer les keepalives TCP
        "keepalives_idle": 15,  # Envoyer keepalive après 15s d'inactivité (plus agressif)
        "keepalives_interval": 5,  # Intervalle entre keepalives plus court
        "keepalives_count": 3,  # Nombre de keepalives avant abandon
    }
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# QuickBooks configuration sécurisée
app.config["QUICKBOOKS_CLIENT_ID"] = os.environ.get("QUICKBOOKS_CLIENT_ID")
app.config["QUICKBOOKS_CLIENT_SECRET"] = os.environ.get(
    "QUICKBOOKS_CLIENT_SECRET")

# Microsoft OAuth configuration
app.config["MICROSOFT_CLIENT_ID"] = os.environ.get("MICROSOFT_CLIENT_ID")
app.config["MICROSOFT_CLIENT_SECRET"] = os.environ.get(
    "MICROSOFT_CLIENT_SECRET")
app.config["MICROSOFT_TENANT"] = os.environ.get("MICROSOFT_TENANT", "common")
# Use environment variable for redirect URI to match current deployment
app.config["MICROSOFT_REDIRECT_URI"] = os.environ.get(
    "MICROSOFT_REDIRECT_URI",
    "http://localhost:5000/auth/microsoft/callback")

# Configuration de sécurité CSRF
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # CSRF token valide 1 heure
# CSRF SSL strict seulement en production
app.config['WTF_CSRF_SSL_STRICT'] = is_production

# Log de la configuration CSRF pour diagnostic
csrf_mode = "STRICT (production)" if is_production else "PERMISSIF (dev)"
print(f"Configuration CSRF: {csrf_mode}")


def bootstrap_app(app):
    """Bootstrap the Flask app with extensions and configuration"""

    # Centralized Stripe API key configuration - set once at startup
    # Individual files only need `import stripe` to use the API
    import stripe
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

    # Gestionnaire personnalisé pour les erreurs SSL lors du teardown
    @app.teardown_request
    def handle_db_errors_on_teardown(exception=None):
        """
        Gère proprement les erreurs de connexion DB lors du teardown.
        Évite les erreurs SSL non catchées lors de la fermeture de session.
        """
        try:
            if exception:
                db.session.rollback()
        except Exception as e:
            # Log silencieusement les erreurs de connexion SSL lors du cleanup
            error_msg = str(e).lower()
            if 'ssl' in error_msg or 'connection' in error_msg or 'closed' in error_msg:
                app.logger.warning(f"Connexion DB fermée lors du cleanup (normal pour Neon): {e}")
            else:
                app.logger.error(f"Erreur DB inattendue lors du cleanup: {e}")
        finally:
            try:
                db.session.remove()
            except Exception:
                pass  # Ignorer les erreurs lors du remove final

    # Middleware pour routage basé sur le domaine
    @app.before_request
    def route_by_domain():
        """Route les requêtes vers le site marketing ou l'application selon le domaine"""
        from flask import request, redirect

        host = request.host.lower()
        path = request.path

        # EXEMPTION ABSOLUE pour les routes de cron jobs - ne jamais rediriger
        if path.startswith('/jobs/'):
            return None

        # Redirection www → domaine nu (301 permanent, prioritaire sur tout le reste)
        if host.startswith('www.finov-relance.com'):
            return redirect(f"https://finov-relance.com{request.full_path}", code=301)

        # Liste des routes marketing (SANS la racine "/" qui doit aller à l'app)
        marketing_routes = [
            '/fonctionnalites', '/tarifs', '/cas-usage', '/contact', '/guide',
            '/essai'
        ]

        # Routes légales et SEO qui doivent toujours être sur finov-relance.com
        legal_routes = [
            '/legal/cgu', '/legal/confidentialite', '/legal/cookies',
            '/legal/mentions'
        ]
        seo_routes = ['/sitemap.xml', '/robots.txt']

        # Routes favicon et assets qui doivent être accessibles sur tous les domaines
        favicon_routes = [
            '/favicon.png', '/favicon.ico', '/favicon-16x16.png', '/favicon-32x32.png',
            '/apple-touch-icon.png', '/android-chrome-192x192.png',
            '/android-chrome-512x512.png', '/site.webmanifest'
        ]

        # Si on est sur finov-relance.com (domaine principal sans app. ni test.)
        if 'finov-relance.com' in host and 'app.finov-relance.com' not in host and 'test.finov-relance.com' not in host:
            # Si c'est la racine "/", laisser passer (affichera le site marketing)
            if path == '/':
                return None
            # Si c'est une route légale, SEO ou favicon, laisser passer
            elif any(path.startswith(route)
                     for route in legal_routes) or path in seo_routes or path in favicon_routes:
                return None
            # Si ce n'est PAS une route marketing ni un fichier statique
            # Rediriger vers app.finov-relance.com
            elif path not in marketing_routes and not path.startswith(
                    '/marketing-static/'):
                return redirect(
                    f"https://app.finov-relance.com{request.full_path}",
                    code=301)

        # Si on est sur app.finov-relance.com
        elif 'app.finov-relance.com' in host:
            # Si c'est une route légale ou SEO, rediriger vers finov-relance.com
            if any(path.startswith(route)
                   for route in legal_routes) or path in seo_routes:
                return redirect(
                    f"https://finov-relance.com{request.full_path}", code=301)
            # Si c'est une route marketing (sans la racine)
            # Rediriger vers finov-relance.com
            elif path in marketing_routes:
                return redirect(
                    f"https://finov-relance.com{request.full_path}", code=301)

        # Sinon, laisser passer normalement
        return None

    # Routes pour servir les favicons à la racine du domaine (requis par Google)
    from flask import send_from_directory, make_response as _make_response

    _favicon_routes = {
        '/favicon.ico': ('favicon.ico', 'image/x-icon'),
        '/favicon.png': ('favicon.png', 'image/png'),
        '/favicon-16x16.png': ('favicon-16x16.png', 'image/png'),
        '/favicon-32x32.png': ('favicon-32x32.png', 'image/png'),
        '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'),
        '/android-chrome-192x192.png': ('android-chrome-192x192.png', 'image/png'),
        '/android-chrome-512x512.png': ('android-chrome-512x512.png', 'image/png'),
        '/site.webmanifest': ('site.webmanifest', 'application/manifest+json'),
    }

    def _make_favicon_handler(filename, mimetype):
        def handler():
            response = _make_response(send_from_directory(app.static_folder, filename, mimetype=mimetype))
            response.headers['Cache-Control'] = 'public, max-age=86400'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
        return handler

    for route_path, (filename, mimetype) in _favicon_routes.items():
        endpoint_name = filename.replace('.', '_').replace('-', '_')
        app.add_url_rule(route_path, endpoint=endpoint_name, view_func=_make_favicon_handler(filename, mimetype))

    # CSRF MONKEY-PATCH EXPLANATION:
    # csrf.exempt() (used below for blueprints) only exempts entire blueprints.
    # This monkey-patch is needed for individual routes that are NOT in exempt blueprints
    # but still cannot provide CSRF tokens (e.g. Stripe customer portal routes in
    # stripe_checkout_v2_bp, and cookie consent API endpoints in auth_bp).
    # These routes receive POST requests from external services or JS fetch without CSRF tokens.
    # Removing this monkey-patch would break those specific routes.
    original_csrf_protect = csrf.protect

    def custom_csrf_protect():
        from flask import request
        # EXEMPTIONS CSRF pour toutes les routes API Stripe (pas de token CSRF possible)
        api_exempt_paths = [
            '/stripe/v2/webhook',  # Webhook Stripe
            '/stripe/v2/customer-portal',  # Portail client
            '/stripe/v2/create-portal-session',  # Alternative portail
            '/stripe/v2/sync-subscription-data',  # Synchronisation abonnement
            '/auth/api/log-cookie-consent',  # API de consentement cookies (RGPD/Loi 25)
            '/auth/api/accept-existing-user-consent',  # API de consentement utilisateurs existants (RGPD/Loi 25)
            '/api/dismiss-migration-notice'  # Avis de migration VPS (one-time, authentifié via @login_required)
        ]

        # Bypass CSRF pour les routes API exemptées (Stripe)
        if any(request.path.startswith(path) for path in api_exempt_paths):
            return  # Pas de protection CSRF pour APIs exemptées
        # Appliquer CSRF normal pour toutes les autres routes
        return original_csrf_protect()

    # Remplacer la méthode protect par notre version personnalisée
    csrf.protect = custom_csrf_protect

    # Initialize extensions with app
    db.init_app(app)
    login_manager.init_app(app)
    # CSRF protection avec exemption ciblée pour webhooks
    csrf.init_app(app)

    # Initialize rate limiter - Protection contre les attaques par force brute
    limiter.init_app(app)

    # Initialize Flask-Caching avec la config Redis/Simple
    app.config.update(_cache_config)
    cache.init_app(app)

    # Gestionnaire d'erreur pour Rate Limiting (429)
    @app.errorhandler(429)
    def ratelimit_handler(e):
        from flask import flash, redirect, url_for, request, jsonify

        # Pour les requêtes AJAX (API), retourner JSON
        if request.is_json or 'application/json' in request.headers.get(
                'Content-Type', ''):
            return jsonify({
                'success':
                False,
                'error':
                'Trop de tentatives. Veuillez patienter avant de réessayer.',
                'retry_after':
                e.retry_after if hasattr(e, 'retry_after') else 60
            }), 429

        # Pour les requêtes web normales, rediriger avec message flash
        flash('Trop de tentatives. Veuillez patienter avant de réessayer.',
              'error')

        # Redirection intelligente selon le contexte
        if 'login' in request.path:
            return redirect(url_for('auth.login'))
        elif 'register' in request.path:
            return redirect(url_for('auth.register'))
        else:
            return redirect(url_for('main.dashboard'))

    # Gestionnaire d'erreur 404
    @app.errorhandler(404)
    def page_not_found(e):
        from flask import render_template
        return render_template('errors/404.html'), 404

    # Gestionnaire d'erreur pour Request Entity Too Large
    @app.errorhandler(413)
    def request_entity_too_large(error):
        from flask import flash, redirect, url_for, request
        # Log l'erreur pour debugging
        app.logger.error(
            f"Request Entity Too Large: {request.url}, content_length: {request.content_length}"
        )

        # Déterminer le message d'erreur selon le contexte
        error_message = 'La taille du fichier dépasse la limite autorisée.'

        if '/guides' in request.url or '/wiki' in request.url:
            error_message = 'La taille du contenu ou des fichiers dépasse la limite autorisée. Veuillez réduire la taille de vos images ou vidéos.'
        elif 'template' in request.url or 'signature' in request.url or 'send' in request.url:
            error_message = 'La taille de votre signature ou du contenu dépasse la limite autorisée. Veuillez réduire la taille de votre signature.'

        flash(error_message, 'error')

        # Rediriger vers la page d'origine si disponible, sinon dashboard
        if request.referrer:
            return redirect(request.referrer)

        return redirect(url_for('main.dashboard'))

    # Login manager configuration
    login_manager.login_view = 'auth.login'  # type: ignore
    login_manager.login_message = 'Veuillez vous connecter pour accéder à cette page.'
    login_manager.login_message_category = 'info'

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    @app.before_request
    def check_password_change_required():
        """Forcer le changement de mot de passe si nécessaire"""
        from flask_login import current_user, logout_user
        from flask import request, redirect, url_for
        from sqlalchemy.exc import OperationalError

        # Routes autorisées même avec changement de mot de passe obligatoire
        allowed_endpoints = ['auth.change_password', 'auth.logout', 'static', 'auth.login']

        try:
            if (current_user.is_authenticated and current_user.must_change_password
                    and request.endpoint not in allowed_endpoints
                    and (request.endpoint is None
                         or not request.endpoint.startswith('static'))):
                return redirect(url_for('auth.change_password'))
        except OperationalError:
            # Connexion PostgreSQL expirée - forcer déconnexion pour sécurité
            db.session.rollback()
            try:
                logout_user()
            except Exception:
                pass
            return redirect(url_for('auth.login'))

    # Plus besoin de vérification pending car les comptes ne sont créés qu'après paiement confirmé

    # Import models to ensure tables are created (moved to avoid circular imports)
    def create_tables():
        try:
            with app.app_context():
                import models
                import onboarding_models  # noqa: F401
                db.create_all()

                # Migration: ajouter bc_company_guid à business_central_configs si absent
                try:
                    from sqlalchemy import text, inspect as sa_inspect
                    inspector = sa_inspect(db.engine)
                    existing_cols = [c['name'] for c in inspector.get_columns('business_central_configs')]
                    if 'bc_company_guid' not in existing_cols:
                        with db.engine.connect() as conn:
                            conn.execute(text(
                                "ALTER TABLE business_central_configs "
                                "ADD COLUMN bc_company_guid VARCHAR(100)"
                            ))
                            conn.commit()
                        app.logger.info("Migration: colonne bc_company_guid ajoutée à business_central_configs")
                except Exception as e:
                    app.logger.warning(f"Migration bc_company_guid ignorée (table absente ou déjà migrée): {e}")

                # Migration: colonnes ajoutées post-Neon (absentes du dump de production)
                _pending_columns = [
                    ('users', 'last_company_id', 'INTEGER REFERENCES companies(id)'),
                    ('users', 'migration_notice_dismissed', 'BOOLEAN DEFAULT false'),
                    ('campaigns', 'filter_unassigned_collector', 'BOOLEAN DEFAULT false'),
                    ('campaigns', 'filter_without_notes', 'BOOLEAN DEFAULT false'),
                ]
                try:
                    from sqlalchemy import text, inspect as sa_inspect
                    inspector = sa_inspect(db.engine)
                    for table, col, col_type in _pending_columns:
                        existing = [c['name'] for c in inspector.get_columns(table)]
                        if col not in existing:
                            with db.engine.connect() as conn:
                                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}'))
                                conn.commit()
                            app.logger.info(f"Migration: colonne {col} ajoutée à {table}")
                except Exception as e:
                    app.logger.warning(f"Migration colonnes post-Neon ignorée: {e}")

                # Monitoring des synchronisations désormais démarré à la demande
                # Le monitoring se lance automatiquement quand une sync est déclenchée
                # et s'arrête après 10 minutes d'inactivité pour économiser les ressources DB
                # Voir sync_monitor.py::ensure_monitoring_started() pour l'implémentation

                # Note: Token refresh is handled by external cron jobs (cron-job.org)
                # No internal scheduler needed - see jobs/refresh_email_tokens.py and jobs/refresh_accounting_tokens.py

                # Phase 1 Scalabilite: index sur FK manquantes (idempotent)
                # Note: Les modeles ont aussi index=True (pour les nouveaux deploiements via db.create_all()).
                # Ce bloc CREATE INDEX est necessaire pour les bases existantes ou db.create_all()
                # ne modifie pas les tables deja creees. Sur un fresh deploy, les deux coexistent
                # avec des noms differents (ix_* vs idx_*) — inoffensif, PostgreSQL les deduplique en lecture.
                try:
                    fk_indexes = [
                        "CREATE INDEX IF NOT EXISTS idx_sal_old_plan ON subscription_audit_log(old_plan_id)",
                        "CREATE INDEX IF NOT EXISTS idx_sal_new_plan ON subscription_audit_log(new_plan_id)",
                        "CREATE INDEX IF NOT EXISTS idx_sal_user ON subscription_audit_log(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_company_plan ON companies(plan_id)",
                        "CREATE INDEX IF NOT EXISTS idx_company_created_by ON companies(created_by_user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_user_last_company ON users(last_company_id)",
                        "CREATE INDEX IF NOT EXISTS idx_2fa_user ON two_factor_auth(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_cn_user ON communication_notes(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_cn_updated_by ON communication_notes(updated_by)",
                        "CREATE INDEX IF NOT EXISTS idx_cn_parent_note ON communication_notes(parent_note_id)",
                        "CREATE INDEX IF NOT EXISTS idx_et_company ON email_templates(company_id)",
                        "CREATE INDEX IF NOT EXISTS idx_et_created_by ON email_templates(created_by)",
                        "CREATE INDEX IF NOT EXISTS idx_et_original ON email_templates(original_template_id)",
                        "CREATE INDEX IF NOT EXISTS idx_ac_company ON accounting_connections(company_id)",
                        "CREATE INDEX IF NOT EXISTS idx_bcsl_company ON business_central_sync_logs(company_id)",
                        "CREATE INDEX IF NOT EXISTS idx_ih_user ON import_history(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_prt_user ON password_reset_tokens(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_ij_user ON import_jobs(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_campaign_collector ON campaigns(filter_collector_id)",
                        "CREATE INDEX IF NOT EXISTS idx_campaign_template ON campaigns(email_template_id)",
                        "CREATE INDEX IF NOT EXISTS idx_campaign_stop_by ON campaigns(stop_requested_by)",
                        "CREATE INDEX IF NOT EXISTS idx_pst_user ON password_setup_tokens(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_pst_company ON password_setup_tokens(company_id)",
                    ]
                    with db.engine.connect() as conn:
                        for idx_sql in fk_indexes:
                            conn.execute(text(idx_sql))
                        conn.commit()
                    app.logger.info(f"Phase 1: {len(fk_indexes)} FK indexes verified/created")
                except Exception as e:
                    app.logger.warning(f"Phase 1 FK indexes: {e}")
        except Exception as e:
            # Log error but don't crash - allows health checks to still work
            app.logger.error(f"Database initialization error: {e}")
            # Re-raise in production if DATABASE_URL is set (expected to have DB)
            if os.environ.get("DATABASE_URL"):
                raise

    # Defer table creation to avoid circular imports
    create_tables()

    # RÉSILIENCE: Reprendre les campagnes interrompues par un redémarrage
    try:
        from views.campaign_views import resume_interrupted_campaigns
        import threading
        # Lancer en thread séparé pour ne pas bloquer le démarrage
        threading.Thread(target=resume_interrupted_campaigns, daemon=True).start()
    except Exception as e:
        app.logger.warning(f"Impossible de reprendre les campagnes: {e}")

    # Make template filters and globals available
    from utils import format_currency as _format_currency, format_local_datetime, format_local_date, check_feature_access, convert_utc_to_local
    from flask_login import current_user

    def format_currency_with_company(amount, currency=None):
        """Wrapper that automatically uses the company's currency if not specified"""
        if currency is None:
            try:
                if current_user and current_user.is_authenticated:
                    company = current_user.get_selected_company()
                    if company and hasattr(company, 'currency') and company.currency:
                        currency = company.currency
            except Exception:
                pass
        return _format_currency(amount, currency or 'CAD')

    app.jinja_env.filters['format_currency'] = format_currency_with_company
    app.jinja_env.filters['format_local_datetime'] = format_local_datetime
    app.jinja_env.filters['format_local_date'] = format_local_date
    app.jinja_env.filters['to_local_timezone'] = convert_utc_to_local
    app.jinja_env.globals['format_currency'] = format_currency_with_company
    app.jinja_env.globals['format_local_datetime'] = format_local_datetime
    app.jinja_env.globals['format_local_date'] = format_local_date
    app.jinja_env.globals['check_feature_access'] = check_feature_access

    # HTML sanitization filter for XSS protection
    import bleach

    def sanitize_html(html):
        """
        Sanitize HTML content using bleach to prevent XSS attacks.
        Allows common formatting tags used in email HTML content.
        Returns a plain string - templates must use |safe after this filter.
        """
        if not html:
            return ''

        # Define allowed HTML tags for email content (including img for email images)
        allowed_tags = [
            'p', 'br', 'strong', 'em', 'u', 'b', 'i', 'ul', 'ol', 'li',
            'a', 'span', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
            'blockquote', 'pre', 'code', 'table', 'thead', 'tbody', 'tr',
            'th', 'td', 'img', 'hr'
        ]

        # Define allowed attributes per tag (restrict img src to http/https only)
        allowed_attributes = {
            'a': ['href', 'title', 'target'],
            'img': ['src', 'alt', 'width', 'height', 'title'],
            'span': ['style'],
            'div': ['style'],
            'p': ['style'],
            'td': ['style', 'colspan', 'rowspan'],
            'th': ['style', 'colspan', 'rowspan']
        }

        # Sanitize the HTML (styles parameter removed - deprecated in bleach 6.x)
        cleaned_html = bleach.clean(
            html,
            tags=allowed_tags,
            attributes=allowed_attributes,
            strip=True
        )

        # Return plain string - template must use |safe explicitly after sanitization
        return cleaned_html

    app.jinja_env.filters['sanitize_html'] = sanitize_html

    # Ajouter DEV_MODE aux globals du template
    app.jinja_env.globals['DEV_MODE'] = app.config.get('DEV_MODE', False)

    # Ajouter les fonctions Python built-in pour les templates
    app.jinja_env.globals['min'] = min
    app.jinja_env.globals['max'] = max
    app.jinja_env.globals['range'] = range
    from datetime import datetime as _dt
    app.jinja_env.globals['now'] = _dt.now

    # Fonction pour nettoyer les aperçus email et regrouper les notes par conversation
    from utils.note_grouping import clean_email_preview, group_notes_by_conversation
    app.jinja_env.globals['clean_email_preview'] = clean_email_preview
    app.jinja_env.globals['group_notes_by_conversation'] = group_notes_by_conversation
    # Enregistrer aussi comme filtre pour la syntaxe {{ value|clean_email_preview(120) }}
    app.jinja_env.filters['clean_email_preview'] = clean_email_preview

    # Filtre asset_url: retourne l'URL complete du fichier statique avec cache bust
    # Usage: {{ 'css/style.css'|asset_url }} (retourne l'URL complete, pas besoin de url_for)
    from flask import url_for as _url_for
    def asset_url_filter(filename):
        try:
            filepath = os.path.join(app.static_folder, filename)
            mtime = int(os.path.getmtime(filepath))
            return _url_for('static', filename=filename, v=mtime)
        except OSError:
            return _url_for('static', filename=filename)
    app.jinja_env.filters['asset_url'] = asset_url_filter

    # Import main blueprints directly from views.py file and specialized modules from views package
    import importlib.util
    spec = importlib.util.spec_from_file_location("views_main", os.path.join(os.path.dirname(__file__), "views.py"))
    if spec is not None and spec.loader is not None:
        views_main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(views_main)
    else:
        raise ImportError("Cannot load views.py module")

    from views import auth_bp, client_bp, note_bp
    from views.admin_views import admin_bp
    from views.company_views import company_bp
    from views.reminder_views import reminder_bp
    from views.email_views import email_bp
    from views.invoice_views import invoice_bp
    from views.campaign_views import campaign_bp
    # REFONTE STRIPE 2.0 - subscription_api supprimé

    # Register Marketing Site Blueprint FIRST to capture "/" route
    from views.marketing_views import marketing_bp
    app.register_blueprint(marketing_bp)

    app.register_blueprint(views_main.main_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(client_bp, url_prefix='/clients')
    app.register_blueprint(views_main.receivable_bp, url_prefix='/receivables')
    app.register_blueprint(company_bp)  # Prefix already defined in blueprint
    app.register_blueprint(views_main.import_bp, url_prefix='/import')
    app.register_blueprint(email_bp, url_prefix='/emails')
    app.register_blueprint(note_bp, url_prefix='/notes')
    app.register_blueprint(reminder_bp, url_prefix='/reminders')
    app.register_blueprint(invoice_bp)  # Invoice operations blueprint
    app.register_blueprint(campaign_bp)  # Campaign email operations blueprint
    app.register_blueprint(views_main.profile_bp, url_prefix='/profile')
    app.register_blueprint(views_main.users_bp, url_prefix='/users')
    app.register_blueprint(admin_bp)  # Admin panel blueprint
    # REFONTE STRIPE 2.0 - subscription_api blueprint supprimé
    # PHASE 4 - Notifications blueprint déjà enregistré dans views.py

    # PHASE 5 - Webhook admin blueprint
    from admin_webhook_routes import admin_webhooks_bp
    app.register_blueprint(admin_webhooks_bp)

    # REFONTE STRIPE 2.0 - Système V2 unifié - Migration complète
    # L'ancien système est désactivé, tout passe par unified maintenant

    # Register Stripe V2 Checkout Blueprint
    from stripe_checkout_v2 import stripe_checkout_v2_bp, stripe_portal_bp
    app.register_blueprint(stripe_checkout_v2_bp)
    app.register_blueprint(stripe_portal_bp)

    # MIGRATION PROGRESSIVE - Nouveau système unifié
    from stripe_finov.webhooks.unified import unified_webhook_bp
    app.register_blueprint(unified_webhook_bp)

    # Désactivation CSRF pour les webhooks Stripe
    csrf.exempt(unified_webhook_bp)

    # Register OAuth callback
    from oauth_callback import oauth_callback_bp
    app.register_blueprint(oauth_callback_bp)

    # Register notification routes
    from notification_routes import notification_bp
    app.register_blueprint(notification_bp)

    # Register Jobs Blueprint for cron tasks
    from jobs.apply_pending_changes import jobs_bp
    app.register_blueprint(jobs_bp)
    csrf.exempt(jobs_bp)

    # Register Backup Blueprint for database backup to dedicated PostgreSQL
    from jobs.database_backup import backup_bp
    app.register_blueprint(backup_bp)
    csrf.exempt(backup_bp)

    # Register Email Token Refresh Blueprint for automatic OAuth token renewal
    from jobs.refresh_email_tokens import refresh_tokens_bp
    app.register_blueprint(refresh_tokens_bp)
    csrf.exempt(refresh_tokens_bp)

    # Register Accounting Token Refresh Blueprint for QB/BC token renewal
    from jobs.refresh_accounting_tokens import refresh_accounting_bp
    app.register_blueprint(refresh_accounting_bp)
    csrf.exempt(refresh_accounting_bp)

    # Register Email Sync V3 Blueprint (optimized conversation sync)
    from jobs.sync_email_v3 import sync_emails_v3_bp
    app.register_blueprint(sync_emails_v3_bp)
    csrf.exempt(sync_emails_v3_bp)

    # Register Cleanup Old Logs Blueprint for automatic log purge
    from jobs.cleanup_old_logs import cleanup_bp
    app.register_blueprint(cleanup_bp)
    csrf.exempt(cleanup_bp)

    # Health check: existant dans views.py (main_bp.route('/health'))
    # Le endpoint /health avec pool stats est dans views/health_views.py
    # mais non enregistre car conflit avec la route existante.
    # TODO: fusionner les deux dans une future iteration.

    # Register Import Progress Blueprint for SSE progress tracking
    from import_progress import import_progress_bp
    app.register_blueprint(import_progress_bp)

    # Register Stripe Onboarding Blueprint (new registration flow)
    from views.stripe_onboarding import onboarding_bp
    app.register_blueprint(onboarding_bp)

    # Stripe handles downgrade scheduling natively via cancel_at_period_end=True
    # No need for custom scheduler

    # Initialize Security Headers (CSP, X-Frame-Options, etc.)
    from security.csp_middleware import init_security_headers
    init_security_headers(app)

    # --- CORRECTION 10: Error logging et slow request warning ---
    import traceback
    import time

    @app.before_request
    def _start_request_timer():
        """Enregistre le timestamp de debut de requete pour detecter les requetes lentes"""
        from flask import request, g
        g._request_start_time = time.time()

    @app.after_request
    def _log_slow_requests(response):
        """Logue un warning pour les requetes dont le temps de reponse depasse 5 secondes"""
        from flask import request, g
        start_time = getattr(g, '_request_start_time', None)
        if start_time is not None:
            elapsed = time.time() - start_time
            if elapsed > 5.0:
                app.logger.warning(
                    "SLOW REQUEST: %s %s took %.2fs (status %s)",
                    request.method, request.path, elapsed, response.status_code
                )
        return response

    @app.errorhandler(Exception)
    def _handle_unhandled_exception(e):
        """Logue les exceptions non capturees avec le traceback complet"""
        from flask import request
        tb = traceback.format_exc()
        app.logger.error(
            "UNHANDLED EXCEPTION on %s %s:\n%s",
            request.method, request.path, tb
        )
        # Re-raise les HTTPException pour que Flask les gere normalement (404, 429, etc.)
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        return "Internal Server Error", 500


# Bootstrap the app with all configurations and extensions
bootstrap_app(app)

# Force SQLAlchemy mapper configuration to prevent recursion with Gunicorn --preload
from sqlalchemy.orm import configure_mappers
with app.app_context():
    configure_mappers()
