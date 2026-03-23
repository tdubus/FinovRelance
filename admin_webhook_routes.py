"""
Routes admin pour monitoring des webhooks Stripe
"""
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from models import WebhookLog, WebhookActionStatus, Company
from sqlalchemy import desc, func
from datetime import datetime, timedelta

admin_webhooks_bp = Blueprint('admin_webhooks', __name__, url_prefix='/admin/webhooks')

@admin_webhooks_bp.before_request
def require_superuser():
    """Ensure user is superuser before accessing webhook routes"""
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))

    if not (hasattr(current_user, 'is_superuser') and current_user.is_superuser):
        flash('Accès réservé aux super administrateurs', 'error')
        return redirect(url_for('main.dashboard'))

@admin_webhooks_bp.route('/')
@login_required
def webhooks_dashboard():
    """Interface principale du monitoring webhooks Stripe"""

    # Permission déjà vérifiée par before_request

    # Paramètres de pagination et filtrage
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    status_filter = request.args.get('status', 'all')
    event_type_filter = request.args.get('event_type', 'all')
    company_filter = request.args.get('company', 'all')
    date_filter = request.args.get('date_range', '7d')

    # Construire la requête de base
    query = WebhookLog.query

    # Filtre par période
    if date_filter == '24h':
        since = datetime.utcnow() - timedelta(hours=24)
        query = query.filter(WebhookLog.received_at >= since)
    elif date_filter == '7d':
        since = datetime.utcnow() - timedelta(days=7)
        query = query.filter(WebhookLog.received_at >= since)
    elif date_filter == '30d':
        since = datetime.utcnow() - timedelta(days=30)
        query = query.filter(WebhookLog.received_at >= since)

    # Filtre par statut
    if status_filter != 'all':
        if status_filter == 'success':
            query = query.filter(WebhookLog.action_status == WebhookActionStatus.SUCCESS)
        elif status_filter == 'failed':
            query = query.filter(WebhookLog.action_status == WebhookActionStatus.FAILED)
        elif status_filter == 'ignored':
            query = query.filter(WebhookLog.action_status == WebhookActionStatus.IGNORED)

    # Filtre par type d'événement
    if event_type_filter != 'all':
        query = query.filter(WebhookLog.event_type.ilike(f'%{event_type_filter}%'))

    # Filtre par entreprise
    if company_filter != 'all':
        query = query.filter(WebhookLog.company_id == company_filter)

    # Ordonner par date décroissante
    query = query.order_by(desc(WebhookLog.received_at))

    # Pagination
    webhooks = query.paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    # Statistiques pour le dashboard
    stats = get_webhook_statistics(date_filter)

    # Listes pour les filtres
    companies = Company.query.filter(Company.id.in_(
        db.session.query(WebhookLog.company_id).distinct()
    )).all()

    event_types = db.session.query(WebhookLog.event_type).distinct().all()
    event_types = [et[0] for et in event_types if et[0]]

    return render_template(
        'admin/webhooks_dashboard.html',
        webhooks=webhooks,
        stats=stats,
        companies=companies,
        event_types=event_types,
        filters={
            'status': status_filter,
            'event_type': event_type_filter,
            'company': company_filter,
            'date_range': date_filter
        }
    )

@admin_webhooks_bp.route('/api/stats')
@login_required
def webhook_stats_api():
    """API pour les statistiques temps réel"""

    # Permission déjà vérifiée par before_request

    date_filter = request.args.get('date_range', '24h')
    stats = get_webhook_statistics(date_filter)

    return jsonify(stats)

@admin_webhooks_bp.route('/<int:webhook_id>')
@login_required
def webhook_detail(webhook_id):
    """Détail complet d'un webhook spécifique"""

    # Permission déjà vérifiée par before_request

    webhook = WebhookLog.query.get_or_404(webhook_id)

    return render_template(
        'admin/webhook_detail.html',
        webhook=webhook
    )

def get_webhook_statistics(date_filter='24h'):
    """Calculer les statistiques des webhooks"""

    # Définir la période
    if date_filter == '24h':
        since = datetime.utcnow() - timedelta(hours=24)
        period_label = "dernières 24h"
    elif date_filter == '7d':
        since = datetime.utcnow() - timedelta(days=7)
        period_label = "7 derniers jours"
    elif date_filter == '30d':
        since = datetime.utcnow() - timedelta(days=30)
        period_label = "30 derniers jours"
    else:
        since = datetime.utcnow() - timedelta(hours=24)
        period_label = "dernières 24h"

    base_query = WebhookLog.query.filter(WebhookLog.received_at >= since)

    # Statistiques générales
    total_webhooks = base_query.count()
    success_count = base_query.filter(WebhookLog.action_status == WebhookActionStatus.SUCCESS).count()
    failed_count = base_query.filter(WebhookLog.action_status == WebhookActionStatus.FAILED).count()
    ignored_count = base_query.filter(WebhookLog.action_status == WebhookActionStatus.IGNORED).count()

    # Taux de succès
    success_rate = (success_count / total_webhooks * 100) if total_webhooks > 0 else 0

    # Temps moyen de traitement
    avg_processing = db.session.query(func.avg(WebhookLog.processing_time_ms)).filter(
        WebhookLog.received_at >= since,
        WebhookLog.processing_time_ms.isnot(None)
    ).scalar()
    avg_processing = int(avg_processing) if avg_processing else 0

    # Dernière activité
    last_webhook = base_query.order_by(desc(WebhookLog.received_at)).first()
    last_activity = last_webhook.received_at if last_webhook else None

    # Top des types d'événements
    top_events_result = db.session.query(
        WebhookLog.event_type,
        func.count(WebhookLog.id).label('count')
    ).filter(
        WebhookLog.received_at >= since
    ).group_by(WebhookLog.event_type).order_by(desc('count')).limit(5).all()

    top_events = [(event, count) for event, count in top_events_result]

    return {
        'period_label': period_label,
        'total_webhooks': total_webhooks,
        'success_count': success_count,
        'failed_count': failed_count,
        'ignored_count': ignored_count,
        'success_rate': round(success_rate, 1),
        'avg_processing_time': avg_processing,
        'last_activity': last_activity,
        'top_events': top_events
    }