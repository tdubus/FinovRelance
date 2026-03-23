#!/usr/bin/env python3
"""
Utility pour le renouvellement des tokens OAuth utilisateurs
Utilisé par les jobs cron HTTP (jobs/refresh_email_tokens.py)
"""

import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TokenRefreshScheduler:
    def __init__(self, app=None):
        self.app = app

    def init_app(self, app):
        """Initialize with Flask app"""
        self.app = app

    def _refresh_user_oauth_token(self, email_config, max_retries=3):
        """
        Refresh Microsoft OAuth token for user email configuration
        ROBUSTESSE: Délègue à email_fallback.refresh_user_oauth_token() pour éviter la duplication
        Utilisé par jobs/refresh_email_tokens.py
        """
        from email_fallback import refresh_user_oauth_token
        return refresh_user_oauth_token(email_config, max_retries=max_retries)

# Global scheduler instance
scheduler = TokenRefreshScheduler()