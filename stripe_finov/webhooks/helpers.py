"""
Helpers spécialisés pour les webhooks Stripe
Évite les imports circulaires avec utils
"""

def _get_stripe_items_safely(subscription):
    """Accès sécurisé aux items d'une subscription Stripe - Support dict webhook + objet Stripe"""
    try:
        # CAS 1: Dictionnaire JSON (depuis webhook)
        if isinstance(subscription, dict):
            return subscription.get('items', {}).get('data', [])

        # CAS 2: Objet Stripe (depuis API call)
        elif hasattr(subscription, 'items') and not callable(subscription.items):
            items = subscription.items
            if hasattr(items, 'data'):
                return items.data
            elif hasattr(items, '__iter__') and not isinstance(items, str):
                return items
            else:
                return []
        else:
            return []
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Erreur _get_stripe_items_safely: {str(e)}")
        return []

def get_item_quantity(item):
    """Helper pour extraire quantity de façon uniforme (dict webhook vs objet Stripe)"""
    try:
        # CAS 1: Dictionnaire JSON (webhook)
        if isinstance(item, dict):
            return item.get('quantity', 1)

        # CAS 2: Objet Stripe (API call)
        elif hasattr(item, 'quantity'):
            return item.quantity
        else:
            return 1
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Erreur get_item_quantity: {str(e)}")
        return 1

def get_item_price_id(item):
    """Helper pour extraire price_id de façon uniforme (dict webhook vs objet Stripe)"""
    try:
        # CAS 1: Dictionnaire JSON (webhook)
        if isinstance(item, dict):
            price = item.get('price', {})
            if isinstance(price, dict):
                return price.get('id')
            else:
                return str(price) if price else None

        # CAS 2: Objet Stripe (API call)
        elif hasattr(item, 'price'):
            price = item.price
            if hasattr(price, 'id'):
                return price.id
            else:
                return str(price) if price else None
        else:
            return None
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Erreur get_item_price_id: {str(e)}")
        return None