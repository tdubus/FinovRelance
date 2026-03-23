"""
Utilitaire pour créer des snapshots des comptes à recevoir.
Appelé après chaque synchronisation ou import de factures réussi.
"""

import logging
from datetime import datetime, date
from decimal import Decimal
from constants import AGING_DAYS_30, AGING_DAYS_60, AGING_DAYS_90

logger = logging.getLogger(__name__)


def create_receivables_snapshot(company_id: int, trigger_type: str = 'sync', session=None):
    """
    Crée un snapshot des comptes à recevoir pour une entreprise.

    Args:
        company_id: ID de l'entreprise
        trigger_type: 'sync' ou 'import'
        session: Session SQLAlchemy optionnelle (pour import_worker qui utilise sa propre session)

    Returns:
        ReceivablesSnapshot créé ou None en cas d'erreur
    """
    try:
        if session is None:
            from app import db
            session = db.session

        from models import ReceivablesSnapshot, Invoice, Client, Company
        from sqlalchemy import func, case, and_, cast, Date

        company = session.query(Company).get(company_id)
        if not company:
            logger.warning(f"Snapshot CAR: Entreprise {company_id} non trouvée")
            return None

        today = date.today()

        calc_date_field = Invoice.invoice_date if company.aging_calculation_method == 'invoice_date' else Invoice.due_date

        days_old = cast(today, Date) - calc_date_field
        is_overdue = Invoice.due_date < today

        current_bucket = case(
            (is_overdue == False, Invoice.amount),
            else_=0
        )

        days_30_bucket = case(
            (and_(is_overdue == True, days_old <= AGING_DAYS_30), Invoice.amount),
            else_=0
        )

        days_60_bucket = case(
            (and_(is_overdue == True, days_old > AGING_DAYS_30, days_old <= AGING_DAYS_60), Invoice.amount),
            else_=0
        )

        days_90_bucket = case(
            (and_(is_overdue == True, days_old > AGING_DAYS_60, days_old <= AGING_DAYS_90), Invoice.amount),
            else_=0
        )

        over_90_bucket = case(
            (and_(is_overdue == True, days_old > AGING_DAYS_90), Invoice.amount),
            else_=0
        )

        result = session.query(
            func.coalesce(func.sum(Invoice.amount), 0).label('total'),
            func.coalesce(func.sum(current_bucket), 0).label('current'),
            func.coalesce(func.sum(days_30_bucket), 0).label('days_0_30'),
            func.coalesce(func.sum(days_60_bucket), 0).label('days_31_60'),
            func.coalesce(func.sum(days_90_bucket), 0).label('days_61_90'),
            func.coalesce(func.sum(over_90_bucket), 0).label('days_90_plus')
        ).join(
            Client, Invoice.client_id == Client.id
        ).filter(
            Client.company_id == company_id,
            Invoice.is_paid == False
        ).first()

        snapshot = ReceivablesSnapshot(
            company_id=company_id,
            snapshot_date=datetime.utcnow(),
            total_amount=Decimal(str(result.total or 0)),
            current_amount=Decimal(str(result.current or 0)),
            days_0_30_amount=Decimal(str(result.days_0_30 or 0)),
            days_31_60_amount=Decimal(str(result.days_31_60 or 0)),
            days_61_90_amount=Decimal(str(result.days_61_90 or 0)),
            days_90_plus_amount=Decimal(str(result.days_90_plus or 0)),
            trigger_type=trigger_type
        )

        session.add(snapshot)
        session.commit()

        logger.info(f"Snapshot CAR créé pour entreprise {company_id}: Total={result.total}")
        return snapshot

    except Exception as e:
        logger.error(f"Erreur création snapshot CAR pour entreprise {company_id}: {e}")
        return None
