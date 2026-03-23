# Gunicorn configuration file
# Timeout étendu pour permettre les backups de base de données
from constants import GUNICORN_TIMEOUT

# Worker timeout de 2 minutes (120 secondes) — overridable via GUNICORN_TIMEOUT env var
# Pour les operations longues (backups), overrider via Coolify: GUNICORN_TIMEOUT=600
timeout = GUNICORN_TIMEOUT

# Bind address
bind = "0.0.0.0:5000"

# Worker options
reload = False
reuse_port = True

# Logging
loglevel = "info"
