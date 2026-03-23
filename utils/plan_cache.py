"""
Helpers de cache pour les Plan queries.
Les plans changent tres rarement (admin seulement) — cache 10 min evite des requetes repetitives.

IMPORTANT: On cache des dicts simples (pas des objets ORM SQLAlchemy)
pour eviter DetachedInstanceError avec RedisCache en production.
"""


def _plan_to_dict(plan):
    """Convertit un objet Plan ORM en dict serialisable (safe pour Redis)"""
    if not plan:
        return None
    return {
        'id': plan.id,
        'name': plan.name,
        'display_name': plan.display_name,
        'max_clients': plan.max_clients,
        'daily_sync_limit': plan.daily_sync_limit,
        'allows_email_sending': plan.allows_email_sending,
        'allows_email_connection': plan.allows_email_connection,
        'allows_accounting_connection': plan.allows_accounting_connection,
        'allows_team_management': plan.allows_team_management,
        'allows_email_templates': plan.allows_email_templates,
        'is_free': plan.is_free,
        'is_active': plan.is_active,
        'plan_level': plan.plan_level,
        'stripe_product_id': plan.stripe_product_id,
        'stripe_price_id': plan.stripe_price_id,
    }


class PlanDict(dict):
    """Dict avec acces par attribut (plan.max_clients au lieu de plan['max_clients'])"""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(f"PlanDict has no attribute '{key}'")


def get_active_plans():
    """Retourne les plans actifs sous forme de PlanDict, caches 10 min"""
    from app import cache
    from models import Plan

    cache_key = 'active_plans'
    plans = cache.get(cache_key)
    if plans is None:
        plan_objects = Plan.query.filter_by(is_active=True).order_by(Plan.plan_level.asc()).all()
        plans = [PlanDict(_plan_to_dict(p)) for p in plan_objects]
        cache.set(cache_key, plans, timeout=600)
    return plans


def get_plan_by_id(plan_id):
    """Retourne un plan par ID sous forme de PlanDict, cache 10 min"""
    if not plan_id:
        return None

    from app import cache, db
    from models import Plan

    cache_key = f'plan_{plan_id}'
    plan = cache.get(cache_key)
    if plan is None:
        plan_obj = db.session.get(Plan, plan_id)
        if plan_obj:
            plan = PlanDict(_plan_to_dict(plan_obj))
            cache.set(cache_key, plan, timeout=600)
    return plan


def invalidate_plan_cache(plan_id=None):
    """Invalide le cache des plans (appeler apres modification admin)"""
    from app import cache

    cache.delete('active_plans')
    if plan_id:
        cache.delete(f'plan_{plan_id}')
