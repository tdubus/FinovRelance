"""
Module de vérification des permissions pour les fonctionnalités de connexion comptable.
Vérifie à la fois le rôle utilisateur et les fonctionnalités du plan d'abonnement.
"""

def check_accounting_access(user, company):
    """
    Vérifie si un utilisateur peut accéder aux fonctionnalités de connexion comptable.

    Args:
        user: L'utilisateur actuel
        company: L'entreprise sélectionnée

    Returns:
        dict: {
            'allowed': bool,           # True si accès autorisé
            'user_role': str,          # Rôle de l'utilisateur
            'has_role': bool,          # True si rôle suffisant
            'has_plan_feature': bool,  # True si plan inclut la fonctionnalité
            'restriction_reason': str  # Message explicatif si accès refusé
        }
    """
    # Gestion défensive : vérifier que user et company existent
    if not user or not company:
        return {
            'allowed': False,
            'user_role': None,
            'has_role': False,
            'has_plan_feature': False,
            'restriction_reason': "Utilisateur ou entreprise non défini"
        }

    try:
        # 1. Vérifier le rôle utilisateur dans l'entreprise
        user_role = user.get_role_in_company(company.id)
        has_role = user_role in ['super_admin', 'admin'] if user_role else False

        # 2. Vérifier les fonctionnalités du plan d'abonnement (gestion défensive)
        try:
            plan_features = company.get_plan_features()
            has_plan_feature = plan_features.get('allows_accounting_connection', False) if plan_features else False
        except Exception:
            # Si get_plan_features() échoue, considérer que la fonctionnalité n'est pas disponible
            has_plan_feature = False

        # 3. Déterminer si l'accès est autorisé
        allowed = has_role and has_plan_feature

        # 4. Générer le message d'erreur si nécessaire
        restriction_reason = _get_restriction_reason(has_role, has_plan_feature)

        return {
            'allowed': allowed,
            'user_role': user_role,
            'has_role': has_role,
            'has_plan_feature': has_plan_feature,
            'restriction_reason': restriction_reason
        }

    except Exception as e:
        # En cas d'erreur inattendue, refuser l'accès par sécurité
        return {
            'allowed': False,
            'user_role': None,
            'has_role': False,
            'has_plan_feature': False,
            'restriction_reason': "Erreur lors de la vérification des permissions"
        }


def _get_restriction_reason(has_role, has_plan_feature):
    """
    Génère un message d'erreur contextuel selon les restrictions.

    Args:
        has_role: True si l'utilisateur a le rôle requis
        has_plan_feature: True si le plan inclut la fonctionnalité

    Returns:
        str: Message explicatif ou None si accès autorisé
    """
    if has_role and has_plan_feature:
        return None  # Accès autorisé

    if not has_role and not has_plan_feature:
        return "Accès réservé aux administrateurs avec plan Relance"

    if not has_role:
        return "Accès réservé aux administrateurs"

    if not has_plan_feature:
        return "Fonctionnalité disponible avec le plan Relance"

    return "Accès non autorisé"  # Cas imprévu