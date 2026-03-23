# -*- coding: utf-8 -*-
"""
Utilitaires pour la gestion centralisée des rôles utilisateur
Normalisation français/anglais et constantes
"""

# Constantes centralisées des rôles
ROLE_SUPER_ADMIN = 'super_admin'
ROLE_ADMIN = 'admin'
ROLE_EMPLOYE = 'employe'
ROLE_LECTEUR = 'lecteur'

# Rôles payants (consomment des licences)
PAID_ROLES = [ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_EMPLOYE]

# Rôles gratuits (ne consomment pas de licences)
FREE_ROLES = [ROLE_LECTEUR]

# Tous les rôles valides
ALL_ROLES = PAID_ROLES + FREE_ROLES


def normalize_role(role):
    """
    Normalise un rôle en gérant les variantes français/anglais

    Args:
        role (str): Rôle à normaliser

    Returns:
        str: Rôle normalisé selon les constantes

    Raises:
        ValueError: Si le rôle n'est pas reconnu
    """
    if not role or not isinstance(role, str):
        raise ValueError(f"Rôle invalide: {role}")

    role_lower = role.lower().strip()

    # Mapping des variantes vers les constantes normalisées
    role_mapping = {
        # Rôles français (officiels)
        'super_admin': ROLE_SUPER_ADMIN,
        'admin': ROLE_ADMIN,
        'employe': ROLE_EMPLOYE,
        'employé': ROLE_EMPLOYE,  # Accent
        'lecteur': ROLE_LECTEUR,

        # Variantes anglaises (legacy/compatibilité)
        'employee': ROLE_EMPLOYE,
        'reader': ROLE_LECTEUR,

        # Autres variantes
        'administrateur': ROLE_ADMIN,
        'superuser': ROLE_SUPER_ADMIN,
        'superadmin': ROLE_SUPER_ADMIN,
    }

    normalized = role_mapping.get(role_lower)
    if normalized is None:
        raise ValueError(f"Rôle non reconnu: {role}")

    return normalized


def is_paid_role(role):
    """
    Vérifie si un rôle est payant (consomme une licence)

    Args:
        role (str): Rôle à vérifier

    Returns:
        bool: True si le rôle est payant
    """
    try:
        normalized = normalize_role(role)
        return normalized in PAID_ROLES
    except ValueError:
        return False


def is_free_role(role):
    """
    Vérifie si un rôle est gratuit

    Args:
        role (str): Rôle à vérifier

    Returns:
        bool: True si le rôle est gratuit
    """
    try:
        normalized = normalize_role(role)
        return normalized in FREE_ROLES
    except ValueError:
        return False


def get_role_display_name(role):
    """
    Obtient le nom d'affichage français d'un rôle

    Args:
        role (str): Rôle à afficher

    Returns:
        str: Nom d'affichage en français
    """
    try:
        normalized = normalize_role(role)
        display_names = {
            ROLE_SUPER_ADMIN: 'Super Admin',
            ROLE_ADMIN: 'Administrateur',
            ROLE_EMPLOYE: 'Employé',
            ROLE_LECTEUR: 'Lecteur'
        }
        return display_names.get(normalized, normalized)
    except ValueError:
        return str(role)


def get_role_choices():
    """
    Retourne les choix de rôles pour les formulaires

    Returns:
        list: Liste de tuples (valeur, libellé) pour les formulaires
    """
    return [
        (ROLE_SUPER_ADMIN, get_role_display_name(ROLE_SUPER_ADMIN)),
        (ROLE_ADMIN, get_role_display_name(ROLE_ADMIN)),
        (ROLE_EMPLOYE, get_role_display_name(ROLE_EMPLOYE)),
        (ROLE_LECTEUR, get_role_display_name(ROLE_LECTEUR)),
    ]


def validate_role_change(old_role, new_role, company):
    """
    Valide un changement de rôle en tenant compte des licences

    Args:
        old_role (str): Ancien rôle
        new_role (str): Nouveau rôle
        company: Instance de Company

    Returns:
        tuple: (bool, str) - (valide, message d'erreur si invalide)
    """
    try:
        old_normalized = normalize_role(old_role)
        new_normalized = normalize_role(new_role)

        # Pas de changement
        if old_normalized == new_normalized:
            return True, "OK"

        # Passage d'un rôle gratuit à un rôle payant
        if is_free_role(old_normalized) and is_paid_role(new_normalized):
            return company.can_add_user(new_normalized)

        # Autres changements (payant vers gratuit, ou payant vers payant)
        return True, "OK"

    except ValueError as e:
        return False, str(e)