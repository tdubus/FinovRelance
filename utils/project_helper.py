"""
Project Helper Functions
=========================
Centralized utilities for managing the optional project field feature.
Available only for Excel/CSV imports. Accounting connectors ignore this field.
"""

def get_project_label(company):
    """
    Get the customizable project field label for a company.

    Args:
        company: Company model instance

    Returns:
        str: The custom project label (e.g., "Projet", "Contrat", "Chantier")
             Falls back to "Projet" if not set.
    """
    if not company:
        return "Projet"

    if hasattr(company, 'project_field_name') and company.project_field_name:
        return company.project_field_name

    return "Projet"


def is_project_feature_enabled(company):
    """
    Check if the project feature is enabled for a company.

    Args:
        company: Company model instance

    Returns:
        bool: True if project feature is enabled, False otherwise
    """
    if not company:
        return False

    if hasattr(company, 'project_field_enabled'):
        return company.project_field_enabled == True

    return False
