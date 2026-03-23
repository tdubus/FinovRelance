"""
CRON JOB - Application des changements différés
Exécuté quotidiennement pour appliquer les annulations et downgrades différés
"""

from flask import Blueprint, request, jsonify
from app import db
from models import Company, SubscriptionAuditLog, UserCompany
from utils.audit_service import CronJobLogger
from utils.advisory_lock import advisory_lock, LOCK_APPLY_PENDING_CHANGES
import os
import datetime as dt
import logging

# Blueprint pour les jobs
jobs_bp = Blueprint("jobs", __name__)

# Secret pour sécuriser le cron
CRON_SECRET = os.getenv("CRON_SECRET")

# Logger
logger = logging.getLogger(__name__)

def apply_cancel_to_free(company):
    """Appliquer l'annulation - passage au plan Découverte"""
    # Conservation du super admin uniquement
    company.plan = 'free'
    company.quantity_licenses = 1
    company.status = 'canceled'
    company.cancel_at = None
    company.pending_plan = None
    company.pending_quantity = None
    company.pending_expires_at = None

    super_admin = company.get_super_admin()

    # Désactiver tous les utilisateurs sauf le super admin
    for uc in company.user_companies:
        if super_admin and uc.user_id != super_admin.id:
            uc.is_active = False
            logger.info(f"Deactivated user {uc.user_id} for company {company.id}")

def apply_downgrade(company):
    """Appliquer un downgrade différé"""
    target_plan = company.pending_plan or company.plan
    target_qty = company.pending_quantity or company.quantity_licenses

    # Conversion des excédentaires en lecteur
    users = company.active_users_excluding_super_admin()
    over = max(0, len(users) - target_qty)

    if over > 0:
        # Tri du plus récent au plus ancien
        users_sorted = sorted(users, key=lambda u: u.created_at, reverse=True)

        for u in users_sorted[:over]:
            # Trouver la relation UserCompany
            uc = UserCompany.query.filter_by(user_id=u.id, company_id=company.id).first()
            if uc and uc.role != 'super_admin':
                uc.role = 'lecteur'
                logger.info(f"Converted user {u.id} to reader for company {company.id}")

    # Mise à jour des champs
    company.plan = target_plan
    company.quantity_licenses = target_qty
    company.status = 'active'
    company.pending_plan = None
    company.pending_quantity = None
    company.pending_expires_at = None

    logger.info(f"Applied downgrade for company {company.id}: plan={target_plan}, quantity={target_qty}")

@jobs_bp.route("/jobs/apply_pending_changes", methods=["POST"])
@advisory_lock(LOCK_APPLY_PENDING_CHANGES)
def apply_pending_changes():
    """Endpoint du cron pour appliquer les changements différés"""

    # Vérification du token de sécurité
    token = request.headers.get("X-Job-Token")
    if not CRON_SECRET or token != CRON_SECRET:
        logger.warning("Unauthorized cron job attempt")
        return "forbidden", 403

    now = dt.datetime.utcnow()
    applied_cancel = 0
    applied_downgrade = 0
    errors = []

    with CronJobLogger('apply_pending_changes') as job_log:
        try:
            # 1. Traiter les annulations différées
            pending_cancellations = Company.query.filter(
                Company.status == "pending_cancellation",
                Company.cancel_at <= now
            ).all()

            for company in pending_cancellations:
                try:
                    before = company.to_dict()
                    apply_cancel_to_free(company)

                    # Log audit
                    db.session.add(SubscriptionAuditLog(
                        company_id=company.id,
                        event_type="cron.apply_pending_cancellation",
                        stripe_event_id=f"cron-{now.isoformat()}-cancel-{company.id}",
                        before_json=before,
                        after_json=company.to_dict()
                    ))

                    applied_cancel += 1
                    logger.info(f"Applied pending cancellation for company {company.id}")

                except Exception as e:
                    logger.error(f"Error applying cancellation for company {company.id}: {e}")
                    errors.append(f"Cancel company {company.id}: {str(e)}")

            # 2. Traiter les downgrades différés
            pending_downgrades = Company.query.filter(
                Company.status == "pending_downgrade",
                Company.pending_expires_at <= now
            ).all()

            for company in pending_downgrades:
                try:
                    before = company.to_dict()
                    apply_downgrade(company)

                    # Log audit
                    db.session.add(SubscriptionAuditLog(
                        company_id=company.id,
                        event_type="cron.apply_pending_downgrade",
                        stripe_event_id=f"cron-{now.isoformat()}-down-{company.id}",
                        before_json=before,
                        after_json=company.to_dict()
                    ))

                    applied_downgrade += 1
                    logger.info(f"Applied pending downgrade for company {company.id}")

                except Exception as e:
                    logger.error(f"Error applying downgrade for company {company.id}: {e}")
                    errors.append(f"Downgrade company {company.id}: {str(e)}")

            # Commit des changements
            db.session.commit()

            # Mettre à jour les compteurs du job log
            job_log.set_counts(
                processed=applied_cancel + applied_downgrade,
                failed=len(errors),
                skipped=0
            )

            result = {
                "applied_cancel": applied_cancel,
                "applied_downgrade": applied_downgrade,
                "timestamp": now.isoformat(),
                "success": len(errors) == 0
            }

            if errors:
                result["errors"] = errors

            logger.info(f"Cron job completed: {result}")
            return jsonify(result), 200 if len(errors) == 0 else 207  # 207 = Multi-Status

        except Exception as e:
            logger.error(f"Critical error in cron job: {e}")
            raise