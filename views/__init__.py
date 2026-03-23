# Views module for Flask application
# Import only the specialized modules to avoid circular imports

from .auth_views import auth_bp
from .client_views import client_bp
from .company_views import company_bp
from .admin_views import admin_bp
from .note_views import note_bp