"""
DMP Calculator — Délai Moyen de Paiement

Formule : DMP = AVG(payment_date - date_référence)
  - date_référence = invoice_date  si company.aging_calculation_method = 'invoice_date'
  - date_référence = due_date      si company.aging_calculation_method = 'due_date'

Calculé depuis la table ReceivedPayment (paiements historiques réels).
Conforme à la pratique LME / recouvrement B2B français.
"""

import logging

logger = logging.getLogger(__name__)

MIN_RECORDS = 1


def _get_calc_mode(company_id):
    """Retourne la méthode de calcul configurée pour l'entreprise."""
    from models import Company
    company = Company.query.get(company_id)
    if not company:
        return 'invoice_date'
    return company.aging_calculation_method or 'invoice_date'


def _calculate_dmp_from_records(records, mode):
    """
    Calcule le DMP depuis une liste de ReceivedPayment.

    Args:
        records: liste d'objets ReceivedPayment
        mode: 'invoice_date' ou 'due_date'

    Returns:
        float (jours, peut être négatif si paiement en avance) ou None
    """
    if not records or len(records) < MIN_RECORDS:
        return None

    total_days = 0
    count = 0

    for record in records:
        if not record.payment_date:
            continue

        if mode == 'due_date':
            if not record.invoice_due_date:
                continue
            if record.invoice_date and record.invoice_due_date < record.invoice_date:
                continue  # Données BC corrompues : échéance avant la facture
            ref_date = record.invoice_due_date
        else:
            if not record.invoice_date:
                continue
            ref_date = record.invoice_date

        total_days += (record.payment_date - ref_date).days
        count += 1

    if count < MIN_RECORDS:
        return None

    return round(total_days / count, 1)


def _calculate_dmp_both_from_records(records):
    """
    Calcule les deux DMP (date facture et date échéance) en un seul passage.

    Returns:
        dict avec clés 'invoice_date' et 'due_date', valeurs float ou None
    """
    if not records or len(records) < MIN_RECORDS:
        return {'invoice_date': None, 'due_date': None}

    total_invoice = 0
    count_invoice = 0
    total_due = 0
    count_due = 0

    for record in records:
        if not record.payment_date:
            continue
        if record.invoice_date:
            total_invoice += (record.payment_date - record.invoice_date).days
            count_invoice += 1
        if record.invoice_due_date:
            if record.invoice_date and record.invoice_due_date < record.invoice_date:
                pass  # Données BC corrompues : échéance avant la facture — ignorée
            else:
                total_due += (record.payment_date - record.invoice_due_date).days
                count_due += 1

    return {
        'invoice_date': round(total_invoice / count_invoice, 1) if count_invoice >= MIN_RECORDS else None,
        'due_date': round(total_due / count_due, 1) if count_due >= MIN_RECORDS else None,
    }


def calculate_global_dmp(company_id):
    """
    DMP global pour toute l'entreprise (mode configuré dans les paramètres).

    Args:
        company_id: ID de l'entreprise

    Returns:
        float (jours) ou None si pas assez de données
    """
    try:
        from models import ReceivedPayment
        mode = _get_calc_mode(company_id)
        records = ReceivedPayment.query.filter_by(company_id=company_id).all()
        return _calculate_dmp_from_records(records, mode)
    except Exception as e:
        logger.error(f"Erreur calcul DMP global (company {company_id}): {e}")
        return None


def calculate_global_dmp_both(company_id):
    """
    DMP global pour toute l'entreprise — retourne les deux modes simultanément.

    Args:
        company_id: ID de l'entreprise

    Returns:
        dict {'invoice_date': float|None, 'due_date': float|None}
    """
    try:
        from models import ReceivedPayment
        records = ReceivedPayment.query.filter_by(company_id=company_id).all()
        return _calculate_dmp_both_from_records(records)
    except Exception as e:
        logger.error(f"Erreur calcul DMP global both (company {company_id}): {e}")
        return {'invoice_date': None, 'due_date': None}


def calculate_client_dmp(client_id, company_id):
    """
    DMP pour un client spécifique (mode configuré dans les paramètres).

    Args:
        client_id: ID du client
        company_id: ID de l'entreprise (pour récupérer le mode de calcul)

    Returns:
        float (jours) ou None si pas assez de données
    """
    try:
        from models import ReceivedPayment
        mode = _get_calc_mode(company_id)
        records = ReceivedPayment.query.filter_by(
            company_id=company_id,
            client_id=client_id
        ).all()
        return _calculate_dmp_from_records(records, mode)
    except Exception as e:
        logger.error(f"Erreur calcul DMP client (client {client_id}): {e}")
        return None


def calculate_client_dmp_both(client_id, company_id):
    """
    DMP pour un client spécifique — retourne les deux modes simultanément.

    Args:
        client_id: ID du client
        company_id: ID de l'entreprise

    Returns:
        dict {'invoice_date': float|None, 'due_date': float|None}
    """
    try:
        from models import ReceivedPayment
        records = ReceivedPayment.query.filter_by(
            company_id=company_id,
            client_id=client_id
        ).all()
        return _calculate_dmp_both_from_records(records)
    except Exception as e:
        logger.error(f"Erreur calcul DMP client both (client {client_id}): {e}")
        return {'invoice_date': None, 'due_date': None}


def calculate_collector_dmp(collector_id, company_id):
    """
    DMP pour l'ensemble des clients d'un collecteur.

    Args:
        collector_id: ID du collecteur (User)
        company_id: ID de l'entreprise

    Returns:
        float (jours) ou None si pas assez de données
    """
    try:
        from models import ReceivedPayment, Client
        mode = _get_calc_mode(company_id)

        client_ids = [
            c.id for c in Client.query.filter_by(
                company_id=company_id,
                collector_id=collector_id
            ).all()
        ]

        if not client_ids:
            return None

        records = ReceivedPayment.query.filter(
            ReceivedPayment.company_id == company_id,
            ReceivedPayment.client_id.in_(client_ids)
        ).all()

        return _calculate_dmp_from_records(records, mode)
    except Exception as e:
        logger.error(f"Erreur calcul DMP collecteur (collector {collector_id}): {e}")
        return None
