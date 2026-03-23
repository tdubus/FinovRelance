"""
Permission service for managing access control based on plans and roles
"""
from functools import wraps
from flask import abort, jsonify, request
from flask_login import current_user

class PermissionService:
    """Service for handling permission checks based on plans and roles"""

    @staticmethod
    def can_access_feature(user, company, feature_name):
        """
        Check if user can access a specific feature based on their role and company plan

        Returns:
            (bool, str): (can_access, restriction_reason)
            restriction_reason: 'role' or 'plan' or None
        """
        if not user or not company:
            return False, 'role'

        # Get user's role in the company
        user_role = user.get_role_in_company(company.id)
        if not user_role:
            return False, 'role'

        # Get company's plan features
        plan_features = company.get_plan_features() or {}

        # Feature permission matrix
        feature_permissions = {
            'dashboard': {
                'roles': ['super_admin', 'admin', 'employe', 'lecteur'],
                'plan_required': None
            },
            'clients_view': {
                'roles': ['super_admin', 'admin', 'employe', 'lecteur'],
                'plan_required': None
            },
            'clients_create': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': None
            },
            'clients_edit': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': None
            },
            'clients_delete': {
                'roles': ['super_admin', 'admin'],
                'plan_required': None
            },
            'receivables_view': {
                'roles': ['super_admin', 'admin', 'employe', 'lecteur'],
                'plan_required': None
            },
            'notes_create': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': None
            },
            'notes_edit': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': None
            },
            'csv_import': {
                'roles': ['super_admin', 'admin'],
                'plan_required': None  # All plans allow CSV import
            },
            'accounting_connection': {
                'roles': ['super_admin', 'admin'],
                'plan_required': 'allows_accounting_connection'
            },
            'email_sending': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': 'allows_email_sending'
            },
            'email_connection': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': 'allows_email_connection'
            },
            'email_templates_shared': {
                'roles': ['super_admin', 'admin'],
                'plan_required': 'allows_email_templates'
            },
            'email_templates_personal': {
                'roles': ['super_admin', 'admin', 'employe'],
                'plan_required': 'allows_email_templates'
            },
            'team_management': {
                'roles': ['super_admin', 'admin'],
                'plan_required': 'allows_team_management'
            },
            'company_settings': {
                'roles': ['super_admin'],
                'plan_required': None
            },
            'plan_management': {
                'roles': ['super_admin'],
                'plan_required': None
            },
            'reports_export': {
                'roles': ['super_admin', 'admin', 'employe', 'lecteur'],
                'plan_required': None
            },
            'campaigns': {
                'roles': ['super_admin'],  # SÉCURITÉ: Seul super_admin par défaut, délégation explicite via can_create_campaigns
                'plan_required': 'allows_email_sending',
                'check_delegation': 'can_create_campaigns'  # Permission déléguée par super_admin
            }
        }

        # Check if feature exists
        if feature_name not in feature_permissions:
            return False, 'role'

        feature_config = feature_permissions[feature_name]

        # Check role permission
        role_allowed = user_role in feature_config['roles']

        # Check for delegated permission (e.g., can_create_campaigns)
        delegation_allowed = False
        if not role_allowed and 'check_delegation' in feature_config:
            delegation_field = feature_config['check_delegation']
            from models import UserCompany
            user_company = UserCompany.query.filter_by(
                user_id=user.id,
                company_id=company.id,
                is_active=True
            ).first()
            if user_company and getattr(user_company, delegation_field, False):
                delegation_allowed = True

        if not role_allowed and not delegation_allowed:
            return False, 'role'

        # Check plan permission
        plan_required = feature_config['plan_required']
        if plan_required:
            if not plan_features.get(plan_required, False):
                return False, 'plan'

        return True, None

    @staticmethod
    def check_client_limit(company):
        """
        Check if company can add more clients

        Returns:
            (bool, str): (can_add, reason)
        """
        if company.is_free_account:
            return True, None

        if not company.can_add_client():
            return False, 'plan'

        return True, None

    @staticmethod
    def get_restriction_message(restriction_type, feature_name=None):
        """Get user-friendly restriction message"""
        if restriction_type == 'role':
            return "Vous n'avez pas accès à cette fonctionnalité"
        elif restriction_type == 'plan':
            return "Votre forfait actuel ne vous donne pas accès à cette fonctionnalité, veuillez upgrader votre plan"
        else:
            return "Accès refusé"

def require_permission(feature_name):
    """Decorator to require specific permission for a route"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            company = current_user.get_selected_company()
            if not company:
                abort(403)

            # Même les Super Admin globaux respectent les limites de forfait dans une entreprise
            can_access, restriction_reason = PermissionService.can_access_feature(
                current_user, company, feature_name
            )

            if not can_access:
                if request.is_json:
                    return jsonify({
                        'error': PermissionService.get_restriction_message(restriction_reason),
                        'restriction_type': restriction_reason
                    }), 403
                else:
                    abort(403)

            return f(*args, **kwargs)
        return decorated_function
    return decorator

def require_superuser():
    """Decorator to require superuser access (for admin panel)"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.is_superuser:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def require_role(allowed_roles):
    """Decorator to require specific role in current company"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            company = current_user.get_selected_company()
            if not company:
                abort(403)

            user_role = current_user.get_role_in_company(company.id)
            if user_role not in allowed_roles:
                abort(403)

            return f(*args, **kwargs)
        return decorated_function
    return decorator

def require_plan_feature(feature_name):
    """Decorator to require specific plan feature"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            company = current_user.get_selected_company()
            if not company:
                abort(403)

            # Même les Super Admin globaux respectent les limites de forfait
            plan_features = company.get_plan_features()
            if not plan_features.get(feature_name, False):
                if request.is_json:
                    return jsonify({
                        'error': PermissionService.get_restriction_message('plan'),
                        'restriction_type': 'plan'
                    }), 403
                else:
                    abort(403)

            return f(*args, **kwargs)
        return decorated_function
    return decorator