"FICHIER NETTOYÉ LE 2025-12-31"
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, make_response, jsonify, current_app
from flask_login import login_required, current_user
from datetime import datetime
import os
# SECURITY: Using defusedcsv to prevent CSV injection attacks
# This completely replaces the standard csv module with the secure version
from defusedcsv import csv
import io
# WeasyPrint removed - using ReportLab for PDF generation

# Models, forms, and db imports moved to individual functions to avoid circular imports
from utils import get_local_today, company_has_original_amount
# PHASE 4 : license_audit imports supprimés - architecture V2
# PHASE 4 : cancel_company_subscription import supprimé - fonction inexistante
from app import db, limiter
# QuickBooksConnector import moved to functions to avoid circular imports
# stripe_integration import moved to functions to avoid circular imports
import stripe

# Create blueprints (specialized modules imported separately in app.py)
main_bp = Blueprint('main', __name__)
profile_bp = Blueprint('profile', __name__, url_prefix='/profile')


# Route de démonstration pour l'interface de mapping Business Central
@main_bp.route('/demo-interface-mapping')
def demo_interface_mapping():
    """Démonstration de l'interface de mapping Business Central avec listes déroulantes"""
    return render_template('demo_interface_mapping.html')


# Import specialized module blueprints (routes moved to dedicated files)
from views.receivable_views import receivable_bp
from views.import_views import import_bp
from views.user_views import users_bp

# REMOVED: Company routes moved to views/company_views.py
# This eliminates function conflicts between main views.py and modular architecture


def sanitize_csv_content(content):
    """Sanitize content to prevent CSV injection attacks.

    Prefixes potentially dangerous content with single quote to prevent
    formula execution in spreadsheet applications.
    """
    if not content or not isinstance(content, str):
        return content

    # Check for dangerous starting characters that could be interpreted as formulas
    dangerous_chars = ('=', '+', '-', '@', '\t', '\r')
    if content.startswith(dangerous_chars):
        return "'" + content

    return content


@main_bp.route('/health')
def health_check():
    """Health check endpoint for deployment verification and load balancers"""
    import time
    from app import db
    from sqlalchemy import text

    checks = {}

    # DB connectivity + latency
    try:
        start = time.time()
        db.session.execute(text('SELECT 1'))
        checks['database'] = {
            'status': 'ok',
            'latency_ms': round((time.time() - start) * 1000, 2)
        }
    except Exception as e:
        checks['database'] = {'status': 'error', 'error': str(e)}

    # DB pool stats
    try:
        pool = db.engine.pool
        checks['pool'] = {
            'size': pool.size(),
            'checked_out': pool.checkedout(),
            'overflow': pool.overflow(),
        }
    except Exception:
        checks['pool'] = {'status': 'unavailable'}

    overall = 'ok' if checks.get('database', {}).get('status') == 'ok' else 'degraded'

    return jsonify({
        'status': overall,
        'checks': checks,
        'timestamp': datetime.utcnow().isoformat(),
    }), 200 if overall == 'ok' else 503


@main_bp.route('/.well-known/<path:filename>')
def well_known(filename):
    """Serve .well-known files for domain verification and security"""
    if filename == 'mta-sts.txt':
        # RFC 8461 requires CRLF line endings for MTA-STS policy files
        policy_lines = [
            "version: STSv1", "mode: enforce",
            "mx: finovrelance-com01c.mail.protection.outlook.com",
            "max_age: 86400"
        ]
        policy_content = "\r\n".join(policy_lines)

        response = make_response(policy_content)
        response.headers['Content-Type'] = 'text/plain'
        return response

    # Return 404 for other .well-known requests
    from flask import abort
    abort(404)


@main_bp.route('/sitemap.xml')
def sitemap():
    """Serve sitemap for SEO"""
    import os
    from flask import send_file, current_app

    sitemap_path = os.path.join(current_app.root_path, 'sitemap.xml')

    if os.path.exists(sitemap_path):
        return send_file(sitemap_path, mimetype='application/xml')

    from flask import abort
    abort(404)


@main_bp.route('/robots.txt')
def robots():
    """Serve robots.txt for SEO"""
    import os
    from flask import send_file, current_app

    robots_path = os.path.join(current_app.root_path, 'robots.txt')

    if os.path.exists(robots_path):
        return send_file(robots_path, mimetype='text/plain')

    from flask import abort
    abort(404)


@main_bp.route('/export-activity-csv')
@limiter.limit("10 per minute")
@login_required
def export_activity_csv():
    """Export daily activity as CSV"""
    from app import db
    from models import Client, CommunicationNote

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get selected date
    selected_date = request.args.get('activity_date')
    if selected_date:
        try:
            activity_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            activity_date = datetime.now().date()
    else:
        activity_date = datetime.now().date()

    # Get communication notes for the selected date - filtered by user permissions
    start_datetime = datetime.combine(activity_date, datetime.min.time())
    end_datetime = datetime.combine(activity_date, datetime.max.time())

    # Filter notes based on user role
    user_role = current_user.get_role_in_company(company.id)

    if user_role in ['super_admin', 'admin']:
        # Admins can export all company notes
        daily_notes = db.session.query(CommunicationNote).join(Client).filter(
            Client.company_id == company.id, CommunicationNote.created_at
            >= start_datetime, CommunicationNote.created_at
            <= end_datetime).order_by(
                CommunicationNote.created_at.desc()).all()
    else:
        # Employees and readers can only export their own notes
        daily_notes = db.session.query(CommunicationNote).join(Client).filter(
            Client.company_id == company.id,
            CommunicationNote.user_id == current_user.id,
            CommunicationNote.created_at >= start_datetime,
            CommunicationNote.created_at <= end_datetime).order_by(
                CommunicationNote.created_at.desc()).all()

    # Create CSV content - using defusedcsv for security
    # Note: csv module is already imported as the secure defusedcsv version
    import io
    output = io.StringIO()
    writer = csv.writer(output)

    # Headers
    writer.writerow(['Heure', 'Client', 'Type', 'Utilisateur', 'Note'])

    # Data rows
    for note in daily_notes:
        # Truncate and sanitize note text
        note_text = note.note_text[:100] + '...' if len(
            note.note_text) > 100 else note.note_text

        writer.writerow([
            note.created_at.strftime('%H:%M'),
            sanitize_csv_content(note.client.name), {
                'call': 'Appel',
                'email': 'Email',
                'meeting': 'Rencontre',
                'general': 'Général'
            }.get(note.note_type, sanitize_csv_content(note.note_type)),
            sanitize_csv_content(note.user.full_name),
            sanitize_csv_content(note_text)
        ])

    # Create response
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers[
        'Content-Disposition'] = f'attachment; filename=activite_{activity_date.strftime("%Y%m%d")}.csv'

    return response


# Main routes
@main_bp.route('/')
def home():
    """Route racine pour health check de déploiement"""
    if current_user.is_authenticated:
        # Utilisateur connecté : rediriger vers dashboard
        return redirect(url_for('main.dashboard'))
    else:
        # Utilisateur non connecté : afficher page d'accueil
        return render_template('home.html')


@main_bp.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard"""
    from app import db, cache
    from models import Client, Invoice, CommunicationNote

    # Get summary statistics for current user's company
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    company_id = company.id

    # --- Stats cachees par company_id (TTL 5 min) ---
    cache_key = f'dashboard_stats_{company_id}'
    stats = cache.get(cache_key)

    if stats is None:
        # Total clients
        total_clients = Client.query.filter_by(company_id=company_id).count()

        # Total outstanding amount (seulement les factures impayées)
        total_outstanding = float(
            db.session.query(db.func.sum(Invoice.amount)).join(Client).filter(
                Client.company_id == company_id, Invoice.is_paid
                == False).scalar() or 0)

        # Overdue invoices (seulement les factures impayées)
        today = datetime.now().date()
        overdue_invoices = db.session.query(Invoice).join(Client).filter(
            Client.company_id == company_id, Invoice.due_date < today,
            Invoice.is_paid == False).count()

        # OPTIMISATION: Calculate aged balances using SQL instead of Python loops
        # This is much faster as it's computed directly in the database
        from sqlalchemy import case, func, and_

        calculation_method = company.aging_calculation_method
        calc_date_field = Invoice.invoice_date if calculation_method == 'invoice_date' else Invoice.due_date

        # SQL query to calculate aging totals in one shot
        aging_query = db.session.query(
            # Current (not overdue yet based on due_date)
            func.sum(case((Invoice.due_date >= today, Invoice.amount),
                          else_=0)).label('current'),
            # 1-30 days overdue (based on calculation method)
            func.sum(
                case((and_(Invoice.due_date < today,
                           (today - calc_date_field) <= 30), Invoice.amount),
                     else_=0)).label('days_30'),
            # 31-60 days overdue (based on calculation method)
            func.sum(
                case(
                    (and_(Invoice.due_date < today, (today - calc_date_field) > 30,
                          (today - calc_date_field) <= 60), Invoice.amount),
                    else_=0)).label('days_60'),
            # 61-90 days overdue (based on calculation method)
            func.sum(
                case(
                    (and_(Invoice.due_date < today, (today - calc_date_field) > 60,
                          (today - calc_date_field) <= 90), Invoice.amount),
                    else_=0)).label('days_90'),
            # 90+ days overdue (based on calculation method)
            func.sum(
                case((and_(Invoice.due_date < today,
                           (today - calc_date_field) > 90), Invoice.amount),
                     else_=0)).label('over_90_days'),
            # Count of clients with outstanding balances
            func.count(func.distinct(Invoice.client_id)
                       ).label('clients_count')).join(Client).filter(
                           Client.company_id == company_id,
                           Invoice.is_paid == False).first()

        # Extract results (handle None values)
        total_current = float(aging_query.current or 0)
        total_30_days = float(aging_query.days_30 or 0)
        total_60_days = float(aging_query.days_60 or 0)
        total_90_days = float(aging_query.days_90 or 0)
        total_over_90_days = float(aging_query.over_90_days or 0)
        clients_with_outstanding_count = aging_query.clients_count or 0

        # Calculate percentages for aging tranches
        if total_outstanding > 0:
            percentage_current = round((total_current / total_outstanding) * 100,
                                       2)
            percentage_30_days = round((total_30_days / total_outstanding) * 100,
                                       2)
            percentage_60_days = round((total_60_days / total_outstanding) * 100,
                                       2)
            percentage_90_days = round((total_90_days / total_outstanding) * 100,
                                       2)
            percentage_90_plus = round(
                (total_over_90_days / total_outstanding) * 100, 2)

            # Ensure percentages add up to 100% by adjusting the largest percentage
            total_percentage = percentage_current + percentage_30_days + percentage_60_days + percentage_90_days + percentage_90_plus
            if total_percentage != 100:
                # Find the largest percentage and adjust it
                percentages = [('current', percentage_current),
                               ('30_days', percentage_30_days),
                               ('60_days', percentage_60_days),
                               ('90_days', percentage_90_days),
                               ('90_plus', percentage_90_plus)]
                max_key, max_val = max(percentages, key=lambda x: x[1])
                adjustment = 100 - total_percentage

                if max_key == 'current':
                    percentage_current = round(percentage_current + adjustment, 2)
                elif max_key == '30_days':
                    percentage_30_days = round(percentage_30_days + adjustment, 2)
                elif max_key == '60_days':
                    percentage_60_days = round(percentage_60_days + adjustment, 2)
                elif max_key == '90_days':
                    percentage_90_days = round(percentage_90_days + adjustment, 2)
                elif max_key == '90_plus':
                    percentage_90_plus = round(percentage_90_plus + adjustment, 2)
        else:
            percentage_current = percentage_30_days = percentage_60_days = percentage_90_days = percentage_90_plus = 0

        # DMP global — mode configuré + les deux modes explicites
        from utils.dmp_calculator import calculate_global_dmp, calculate_global_dmp_both
        global_dmp = calculate_global_dmp(company_id)
        global_dmp_both = calculate_global_dmp_both(company_id)
        global_dmp_invoice = global_dmp_both['invoice_date']
        global_dmp_due = global_dmp_both['due_date']

        stats = {
            'total_clients': total_clients,
            'total_outstanding': total_outstanding,
            'overdue_invoices': overdue_invoices,
            'total_current': total_current,
            'total_30_days': total_30_days,
            'total_60_days': total_60_days,
            'total_90_days': total_90_days,
            'total_over_90_days': total_over_90_days,
            'clients_with_outstanding_count': clients_with_outstanding_count,
            'percentage_current': percentage_current,
            'percentage_30_days': percentage_30_days,
            'percentage_60_days': percentage_60_days,
            'percentage_90_days': percentage_90_days,
            'percentage_90_plus': percentage_90_plus,
            'global_dmp': global_dmp,
            'global_dmp_invoice': global_dmp_invoice,
            'global_dmp_due': global_dmp_due,
        }
        cache.set(cache_key, stats, timeout=300)
    # --- Fin stats cachees ---

    # Recent invoices (last 10, seulement les impayées) - non cache car peu couteux et dynamique
    today = datetime.now().date()
    recent_invoices = db.session.query(Invoice).join(Client).filter(
        Client.company_id == company_id, Invoice.is_paid == False).order_by(
            Invoice.invoice_date.desc()).limit(10).all()

    # Get activity for selected date (default to today)
    selected_date = request.args.get('activity_date')
    if selected_date:
        try:
            activity_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            activity_date = get_local_today()
    else:
        activity_date = get_local_today()

    # Get communication notes for the selected date - filtered to current user only
    from utils import convert_local_to_utc
    timezone_str = company.timezone if company.timezone else 'America/Montreal'

    # Convert local date to UTC range for database query
    start_local = datetime.combine(activity_date, datetime.min.time())
    end_local = datetime.combine(activity_date, datetime.max.time())

    start_utc = convert_local_to_utc(start_local, timezone_str)
    end_utc = convert_local_to_utc(end_local, timezone_str)

    # OPTIMISATION: Show only activities for the current user (personal dashboard view)
    daily_notes = db.session.query(CommunicationNote).join(Client).filter(
        Client.company_id == company_id,
        CommunicationNote.user_id == current_user.id,
        CommunicationNote.created_at >= start_utc, CommunicationNote.created_at
        <= end_utc).order_by(CommunicationNote.created_at.desc()).all()

    # Clean note text for display
    from utils import clean_note_text
    for note in daily_notes:
        if note.note_text:
            note.note_text = clean_note_text(note.note_text)

    # OPTIMISATION: Show only reminders for the current user (personal dashboard view)
    all_reminders = db.session.query(CommunicationNote).join(Client).filter(
        Client.company_id == company_id,
        CommunicationNote.user_id == current_user.id,
        CommunicationNote.reminder_date.isnot(None),
        CommunicationNote.is_reminder_completed == False).order_by(
            CommunicationNote.reminder_date).all()

    # Filter reminders: overdue + today + upcoming (limited to 10 total)
    overdue_reminders = [r for r in all_reminders if r.is_reminder_overdue()]
    today_reminders = [r for r in all_reminders if r.is_reminder_today()]
    upcoming_reminders = [r for r in all_reminders if r.is_reminder_upcoming()]

    # Combine and limit to 10 most important (overdue first, then today, then upcoming)
    reminders = (overdue_reminders + today_reminders + upcoming_reminders)[:10]

    for reminder in reminders:
        if reminder.note_text:
            reminder.note_text = clean_note_text(reminder.note_text)

    # Vérifier si l'utilisateur doit donner son consentement RGPD/Loi 25
    from utils.consent_helper import check_user_needs_new_consent, CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION, CURRENT_COOKIES_VERSION
    needs_consent = (
        check_user_needs_new_consent(current_user.id, 'terms')
        or check_user_needs_new_consent(current_user.id, 'privacy')
        or check_user_needs_new_consent(current_user.id, 'cookies'))

    # Avis de migration VPS (une seule fois)
    show_migration_notice = not getattr(current_user, 'migration_notice_dismissed', True)

    return render_template(
        'dashboard.html',
        global_dmp=stats['global_dmp'],
        global_dmp_invoice=stats['global_dmp_invoice'],
        global_dmp_due=stats['global_dmp_due'],
        total_clients=stats['total_clients'],
        total_outstanding=stats['total_outstanding'],
        recent_invoices=recent_invoices,
        overdue_invoices=stats['overdue_invoices'],
        daily_notes=daily_notes,
        activity_date=activity_date,
        reminders=reminders,
        clients_with_outstanding_count=stats['clients_with_outstanding_count'],
        total_current=stats['total_current'],
        total_30_days=stats['total_30_days'],
        total_60_days=stats['total_60_days'],
        total_90_days=stats['total_90_days'],
        total_over_90_days=stats['total_over_90_days'],
        percentage_current=stats['percentage_current'],
        percentage_30_days=stats['percentage_30_days'],
        show_migration_notice=show_migration_notice,
        needs_consent=needs_consent,
        terms_version=CURRENT_TERMS_VERSION,
        privacy_version=CURRENT_PRIVACY_VERSION,
        cookies_version=CURRENT_COOKIES_VERSION,
        percentage_60_days=stats['percentage_60_days'],
        percentage_90_days=stats['percentage_90_days'],
        percentage_90_plus=stats['percentage_90_plus'],
        company=company)


@main_bp.route('/api/dismiss-migration-notice', methods=['POST'])
@login_required
def dismiss_migration_notice():
    """Marquer l'avis de migration comme lu pour l'utilisateur courant."""
    from flask import jsonify
    current_user.migration_notice_dismissed = True
    db.session.commit()
    return jsonify({'success': True})


# REMOVED: auth_bp routes moved to views/auth_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/api/receivables-years')
@login_required
def api_receivables_years():
    """API endpoint pour récupérer les années disponibles de snapshots"""
    from flask import jsonify
    from models import ReceivablesSnapshot
    from sqlalchemy import func, extract
    from app import db

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    years = db.session.query(
        func.distinct(extract('year', ReceivablesSnapshot.snapshot_date))
    ).filter(
        ReceivablesSnapshot.company_id == company.id
    ).order_by(
        extract('year', ReceivablesSnapshot.snapshot_date).desc()
    ).all()

    return jsonify({
        'years': [int(y[0]) for y in years if y[0]]
    })


@main_bp.route('/api/receivables-history')
@login_required
def api_receivables_history():
    """API endpoint pour l'historique des comptes à recevoir (graphique)"""
    from flask import jsonify
    from models import ReceivablesSnapshot

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    period = request.args.get('period', 'month')
    bucket = request.args.get('bucket', 'total')
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    week_start = request.args.get('week_start')

    if period not in ['year', 'month', 'week', 'day']:
        period = 'month'
    if bucket not in ['total', 'current', '0-30', '31-60', '61-90', '90+']:
        bucket = 'total'

    history = ReceivablesSnapshot.get_history(
        company.id,
        period=period,
        bucket=bucket,
        year=year,
        month=month,
        week_start=week_start
    )

    return jsonify({
        'period': period,
        'bucket': bucket,
        'year': year,
        'month': month,
        'week_start': week_start,
        'data': history
    })


# CRITICAL FIX: Route manquante pour new_client
@main_bp.route('/clients/new', methods=['GET', 'POST'])
@login_required
def new_client():
    """Create new client"""
    from app import db
    from models import Client
    from forms import ClientForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour créer des clients.',
            'error')
        return redirect(url_for('client.list_clients'))

    form = ClientForm(company_id=company.id)
    if form.validate_on_submit():
        # Validate parent-child relationship for new client
        new_parent_id = form.parent_client_id.data if form.parent_client_id.data else None
        if new_parent_id:
            # Check if proposed parent can be a parent (not already a child)
            # SÉCURITÉ : Vérifier que le parent appartient à la même entreprise
            proposed_parent = Client.query.filter_by(
                id=new_parent_id, company_id=company.id).first()
            if proposed_parent and proposed_parent.parent_client_id is not None:
                flash(
                    'Erreur de relation parent-enfant: Un compte enfant ne peut pas être parent d\'autres comptes',
                    'error')
                return render_template('clients/form.html',
                                       form=form,
                                       title='Nouveau Client')

        client = Client()
        client.code_client = form.code_client.data
        client.name = form.name.data
        client.email = form.email.data
        client.phone = form.phone.data
        client.address = form.address.data
        client.collector_id = form.collector_id.data if form.collector_id.data else None
        client.representative_name = form.representative_name.data
        client.payment_terms = form.payment_terms.data
        client.language = form.language.data
        client.parent_client_id = new_parent_id
        client.company_id = company.id
        db.session.add(client)
        db.session.commit()

        flash(f'Client {client.name} créé avec succès.', 'success')
        return redirect(url_for('client.list_clients'))

    return render_template('clients/form.html',
                           form=form,
                           title='Nouveau Client')


# API Route for AJAX invoice table updates
@main_bp.route('/api/clients/<int:id>/invoices')
@login_required
def api_client_invoices(id):
    """API endpoint returning invoice table HTML for AJAX updates"""
    from models import Client, Invoice, AccountingConnection

    company = current_user.get_selected_company()
    if not company:
        return "Erreur: Aucune entreprise sélectionnée", 400

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    # Get filter parameters
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = view_filter == 'parent_children' and client.is_parent
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    search_invoice = request.args.get('search_invoice', '').strip()
    group_by_project = request.args.get('group_by_project',
                                        'false').lower() == 'true'

    # Pagination parameters (disabled when grouping by project)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    # Ensure valid pagination values
    if page < 1:
        page = 1
    if per_page < 1 or per_page > 500:
        per_page = 25

    # Build base query for invoices
    if include_children and client.is_parent:
        client_ids = [client.id] + [child.id for child in client.child_clients]
        base_query = Invoice.query.filter(Invoice.client_id.in_(client_ids))
    else:
        base_query = Invoice.query.filter_by(client_id=id)

    # Apply search filter if provided
    if search_invoice:
        base_query = base_query.filter(
            Invoice.invoice_number.ilike(f'%{search_invoice}%'))

    # When grouping by project, disable pagination and get all invoices
    if group_by_project:
        all_invoices = base_query.order_by(Invoice.due_date).all()
        total_invoices = len(all_invoices)
        total_pages = 0  # Disable pagination controls
        page = 1

        # Group invoices by project_name (normalize NULL and empty strings)
        from collections import defaultdict
        grouped_invoices = defaultdict(list)
        for invoice in all_invoices:
            # Normalize: treat NULL, empty string, and whitespace-only as "(Sans projet)"
            project_key = invoice.project_name.strip(
            ) if invoice.project_name else ''
            if not project_key:
                project_key = '(Sans projet)'
            grouped_invoices[project_key].append(invoice)

        # Convert to sorted list of tuples (project_name, invoices)
        # Sort: named projects alphabetically, then "(Sans projet)" last
        invoices_by_project = []
        for project_name in sorted(grouped_invoices.keys()):
            if project_name != '(Sans projet)':
                invoices_by_project.append(
                    (project_name, grouped_invoices[project_name]))
        if '(Sans projet)' in grouped_invoices:
            invoices_by_project.append(
                ('(Sans projet)', grouped_invoices['(Sans projet)']))
    else:
        # Normal pagination
        total_invoices = base_query.count()

        # Calculate pagination info
        total_pages = (total_invoices + per_page - 1) // per_page
        if page > total_pages and total_pages > 0:
            page = total_pages

        # Get paginated invoices for display
        all_invoices = base_query.order_by(
            Invoice.due_date).limit(per_page).offset(
                (page - 1) * per_page).all()
        invoices_by_project = None

    # Check accounting connection for PDF downloads
    accounting_connection = AccountingConnection.query.filter_by(
        company_id=company.id, is_active=True).first()
    has_accounting_connection = accounting_connection is not None
    accounting_system_type = accounting_connection.system_type if accounting_connection else None

    # Check if company has original_amount data
    has_original_amount = company_has_original_amount(company.id)

    # Check if project feature is enabled
    from utils.project_helper import is_project_feature_enabled, get_project_label
    is_project_enabled = is_project_feature_enabled(company)
    project_label = get_project_label(company) if is_project_enabled else None

    # Render only the invoice table partial
    return render_template('clients/_invoice_table.html',
                           client=client,
                           all_invoices=all_invoices,
                           include_children=include_children,
                           has_accounting_connection=has_accounting_connection,
                           accounting_system_type=accounting_system_type,
                           has_original_amount=has_original_amount,
                           is_project_enabled=is_project_enabled,
                           project_label=project_label,
                           group_by_project=group_by_project,
                           invoices_by_project=invoices_by_project,
                           page=page,
                           per_page=per_page,
                           total_invoices=total_invoices,
                           total_pages=total_pages,
                           view_filter=view_filter,
                           date_from=date_from,
                           date_to=date_to,
                           search_invoice=search_invoice)


# API Route for AJAX children hierarchy
@main_bp.route('/api/clients/<int:id>/children')
@login_required
def api_client_children(id):
    """API endpoint returning children hierarchy HTML for AJAX updates"""
    from models import Client
    from views.client_views import get_aged_balances_sql

    company = current_user.get_selected_company()
    if not company:
        return "Erreur: Aucune entreprise sélectionnée", 400

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    if not client.is_parent:
        return "Ce client n'a pas d'enfants", 404

    # Get view_filter for passing to template
    view_filter = request.args.get('view_filter', 'parent_only')

    # Load children with their balances
    from sqlalchemy.orm import selectinload
    client = Client.query.filter_by(id=id, company_id=company.id).options(
        selectinload(Client.child_clients)).first_or_404()

    # Calculate aged balances for all children
    client_ids_for_balances = [child.id for child in client.child_clients]
    aged_balances_all = get_aged_balances_sql(client_ids_for_balances,
                                              company.aging_calculation_method)

    # Precalculate balances and totals for each child
    children_balances = {}
    children_total_outstanding = {}
    for child in client.child_clients:
        child_balances = aged_balances_all.get(
            child.id, {
                'current': 0,
                '30_days': 0,
                '60_days': 0,
                '90_days': 0,
                'over_90_days': 0
            })
        children_balances[child.id] = child_balances
        children_total_outstanding[child.id] = sum(child_balances.values())

    # Render children hierarchy partial
    return render_template(
        'clients/_children_hierarchy.html',
        client=client,
        children_balances=children_balances,
        children_total_outstanding=children_total_outstanding,
        view_filter=view_filter)


# API Route for AJAX project receivables
@main_bp.route('/api/clients/<int:id>/projects')
@login_required
def api_client_projects(id):
    """API endpoint returning project receivables HTML for AJAX updates"""
    from models import Client
    from views.client_views import get_aged_balances_by_project_sql
    from utils.project_helper import is_project_feature_enabled, get_project_label

    company = current_user.get_selected_company()
    if not company:
        return "Erreur: Aucune entreprise sélectionnée", 400

    # Check if project feature is enabled
    if not is_project_feature_enabled(company):
        return "", 204  # No content

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    # Get view_filter to respect Parent + enfants
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = view_filter == 'parent_children' and client.is_parent

    # Build client_ids list
    if include_children:
        from sqlalchemy.orm import selectinload
        client = Client.query.filter_by(id=id, company_id=company.id).options(
            selectinload(Client.child_clients)).first_or_404()
        client_ids = [client.id] + [child.id for child in client.child_clients]
    else:
        client_ids = [client.id]

    # Calculate aged balances grouped by project
    # SECURITY: Pass company_id to enforce tenant isolation
    projects_balances = get_aged_balances_by_project_sql(
        client_ids, company.id, company.aging_calculation_method)

    # Get project label
    project_label = get_project_label(company)

    # Render project receivables partial
    return render_template('clients/_project_receivables.html',
                           projects_balances=projects_balances,
                           project_label=project_label,
                           view_filter=view_filter)


# API Route for AJAX notes list
@main_bp.route('/api/clients/<int:id>/notes')
@login_required
def api_client_notes(id):
    """API endpoint returning notes list HTML for AJAX updates"""
    from models import Client, CommunicationNote

    company = current_user.get_selected_company()
    if not company:
        return "Erreur: Aucune entreprise sélectionnée", 400

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    # Get filter parameters
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = view_filter == 'parent_children' and client.is_parent
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    # Get notes
    if include_children and client.is_parent:
        from sqlalchemy.orm import selectinload
        client = Client.query.filter_by(id=id, company_id=company.id).options(
            selectinload(Client.child_clients)).first_or_404()

        client_ids_for_notes = [client.id] + [
            child.id for child in client.child_clients if child.company_id == company.id
        ]
        notes = CommunicationNote.query.filter(
            CommunicationNote.client_id.in_(client_ids_for_notes)).order_by(
                CommunicationNote.created_at.desc()).all()
    else:
        notes = CommunicationNote.query.filter_by(client_id=id).order_by(
            CommunicationNote.created_at.desc()).all()

    # Date filtering
    if date_from:
        try:
            from datetime import datetime
            from_date = datetime.strptime(date_from, '%Y-%m-%d').date()
            notes = [note for note in notes if note.note_date >= from_date]
        except ValueError:
            pass
    if date_to:
        try:
            from datetime import datetime
            to_date = datetime.strptime(date_to, '%Y-%m-%d').date()
            notes = [note for note in notes if note.note_date <= to_date]
        except ValueError:
            pass

    # Clean notes
    from utils import clean_note_text
    for note in notes:
        if note.note_text:
            note.note_text = clean_note_text(note.note_text)

    # Pré-calculer conversation counts et données (newest/oldest) pour les emails
    from utils.note_grouping import get_conversation_counts_and_data, group_notes_by_conversation
    conv_ids = list(
        set([n.conversation_id for n in notes if n.conversation_id]))
    conversation_counts, conversation_data = get_conversation_counts_and_data(
        db.session, conv_ids, company.id)

    # Regrouper les notes par conversation - newest en parent, oldest en référence
    # load_children=True pour page Client Detail (enfants dans accordéon statique)
    note_groups = group_notes_by_conversation(notes,
                                              conversation_counts,
                                              conversation_data,
                                              load_children=True)

    # Render notes list partial
    return render_template('clients/_notes_list.html',
                           notes=notes,
                           note_groups=note_groups,
                           client=client,
                           company=company,
                           view_filter=view_filter,
                           date_from=date_from,
                           date_to=date_to)


# CRITICAL FIX: Route manquante pour export_notes_excel
@main_bp.route('/clients/<int:id>/notes/export')
@limiter.limit("10 per minute")
@login_required
def export_notes_excel(id):
    """Export client communications notes to Excel"""
    from models import Client, CommunicationNote

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    # Get filter parameters from URL
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = view_filter == 'parent_children' and client.is_parent
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    # Get notes based on filter
    if include_children:
        notes = client.get_consolidated_notes(include_children=True)
    else:
        notes = CommunicationNote.query.filter_by(client_id=id).order_by(
            CommunicationNote.created_at.desc()).all()

    # Apply date filtering
    if date_from:
        try:
            from datetime import datetime
            from_date = datetime.strptime(date_from, '%Y-%m-%d').date()
            notes = [
                note for note in notes if note.created_at.date() >= from_date
            ]
        except ValueError:
            pass

    if date_to:
        try:
            from datetime import datetime
            to_date = datetime.strptime(date_to, '%Y-%m-%d').date()
            notes = [
                note for note in notes if note.created_at.date() <= to_date
            ]
        except ValueError:
            pass

    if not notes:
        flash('Aucune communication à exporter pour ce client.', 'warning')
        return redirect(url_for('client.detail_client', id=id))

    # Create Excel file
    import xlsxwriter
    import io
    from datetime import datetime

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet('Communications')

    # Formats
    header_format = workbook.add_format({
        'bold': True,
        'bg_color': '#D9E1F2',
        'border': 1,
        'align': 'center'
    })
    text_format = workbook.add_format({'border': 1, 'text_wrap': True})
    date_format = workbook.add_format({
        'num_format': 'yyyy/mm/dd hh:mm',
        'border': 1,
        'align': 'center'
    })

    # Simple translations
    translations = {
        'fr': {
            'communications': 'Communications',
            'parent_and_subsidiaries': 'Parent et filiales',
            'client_code': 'Code client',
            'export_date': 'Date d\'export',
            'includes_children': 'Inclut {} clients enfants',
            'date': 'Date',
            'client': 'Client',
            'type': 'Type',
            'user': 'Utilisateur',
            'subject_title': 'Sujet',
            'note': 'Note',
            'reminder': 'Rappel'
        },
        'en': {
            'communications': 'Communications',
            'parent_and_subsidiaries': 'Parent and subsidiaries',
            'client_code': 'Client code',
            'export_date': 'Export date',
            'includes_children': 'Includes {} child clients',
            'date': 'Date',
            'client': 'Client',
            'type': 'Type',
            'user': 'User',
            'subject_title': 'Subject',
            'note': 'Note',
            'reminder': 'Reminder'
        }
    }

    language = 'fr'  # Default to French
    t = translations.get(language, translations['fr'])

    # Title with filter indication
    if include_children:
        title_suffix = f" ({t['parent_and_subsidiaries']})"
    else:
        title_suffix = ""

    title_format = workbook.add_format({
        'bold': True,
        'font_size': 14,
        'align': 'center'
    })
    worksheet.merge_range(
        0, 0, 0, 6, f"{t['communications']} - {client.name}{title_suffix}",
        title_format)

    # Client info
    from datetime import datetime
    worksheet.write(1, 0, f"{t['client_code']}: {client.code_client or 'N/A'}",
                    text_format)
    worksheet.write(
        2, 0, f"{t['export_date']}: {datetime.now().strftime('%Y/%m/%d')}",
        text_format)
    if include_children:
        child_count = len(client.child_clients) if hasattr(
            client, 'child_clients') and client.child_clients else 0
        includes_text = t['includes_children'].format(child_count)
        worksheet.write(3, 0, includes_text, text_format)

    # Headers
    if include_children:
        headers = [
            t['date'], t['client'], t['type'], t['user'], t['subject_title'],
            t['note'], t['reminder']
        ]
    else:
        headers = [
            t['date'], t['type'], t['user'], t['subject_title'], t['note'],
            t['reminder']
        ]

    header_row = 5 if include_children else 4
    for col, header in enumerate(headers):
        worksheet.write(header_row, col, header, header_format)

    # Data
    row = header_row + 1

    for note in notes:
        col = 0
        worksheet.write(row, col, note.created_at, date_format)
        col += 1

        if include_children:
            # Add client name column
            worksheet.write(row, col, note.client.name, text_format)
            col += 1

        # Type
        type_display = {
            'call': 'Appel',
            'email': 'Email',
            'meeting': 'Rencontre',
            'general': 'Général'
        }.get(note.note_type, note.note_type)
        worksheet.write(row, col, type_display, text_format)
        col += 1

        # User
        user_name = note.user.full_name if hasattr(
            note, 'user') and note.user else 'N/A'
        worksheet.write(row, col, user_name, text_format)
        col += 1

        # Subject/Title
        subject = note.email_subject if note.note_type == 'email' and note.email_subject else '-'
        worksheet.write(row, col, subject, text_format)
        col += 1

        # Note content
        note_content = note.note_text if note.note_text else ''
        if note.note_type == 'email' and note.email_body:
            note_content = note.email_body
        worksheet.write(
            row, col, note_content[:500] +
            '...' if len(note_content) > 500 else note_content, text_format)
        col += 1

        # Reminder
        reminder_text = note.reminder_date.strftime(
            '%Y/%m/%d') if note.reminder_date else '-'
        worksheet.write(row, col, reminder_text, text_format)

        row += 1

    # Auto-adjust column widths
    if include_children:
        worksheet.set_column(0, 0, 15)  # Date
        worksheet.set_column(1, 1, 20)  # Client name
        worksheet.set_column(2, 2, 10)  # Type
        worksheet.set_column(3, 3, 15)  # User
        worksheet.set_column(4, 4, 20)  # Subject
        worksheet.set_column(5, 5, 40)  # Note
        worksheet.set_column(6, 6, 12)  # Reminder
    else:
        worksheet.set_column(0, 0, 15)  # Date
        worksheet.set_column(1, 1, 10)  # Type
        worksheet.set_column(2, 2, 15)  # User
        worksheet.set_column(3, 3, 20)  # Subject
        worksheet.set_column(4, 4, 40)  # Note
        worksheet.set_column(5, 5, 12)  # Reminder

    workbook.close()
    output.seek(0)

    # Create response
    response = make_response(output.getvalue())
    response.headers[
        'Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers[
        'Content-Disposition'] = f'attachment; filename=communications_{client.code_client or "client"}_{datetime.now().strftime("%Y%m%d")}.xlsx'

    return response


# CRITICAL FIX: Route manquante pour export_invoices_excel - ERREUR 500 DÉTAIL CLIENT CORRIGÉE
@main_bp.route('/clients/<int:id>/invoices/export')
@limiter.limit("10 per minute")
@login_required
def export_invoices_excel(id):
    """Export client invoices to Excel"""
    from app import db
    from models import Client, Invoice

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    # Get filter parameters from URL
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = view_filter == 'parent_children' and client.is_parent

    # Get invoices based on filter
    if include_children:
        invoices = client.get_consolidated_invoices(include_children=True)
        # Sort consolidated invoices by due date
        invoices.sort(key=lambda x: x.due_date)
    else:
        invoices = Invoice.query.filter_by(client_id=id).order_by(
            Invoice.due_date).all()

    if not invoices:
        flash('Aucune facture à exporter pour ce client.', 'warning')
        return redirect(url_for('client.detail_client', id=id))

    # Check if ANY invoice in company has original_amount
    has_original_amount = db.session.query(db.exists().where(
        db.and_(Invoice.company_id == company.id,
                Invoice.original_amount.isnot(None)))).scalar()

    # Check if project feature is enabled
    from utils.project_helper import is_project_feature_enabled, get_project_label
    is_project_enabled = is_project_feature_enabled(company)
    project_label = get_project_label(company) if is_project_enabled else None

    # Create Excel file using same approach as receivables export
    import xlsxwriter
    import io
    from datetime import datetime

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet('Factures')

    # Get company currency for Excel formatting
    company_currency = company.currency if hasattr(company, 'currency') and company.currency else 'CAD'

    # Currency format patterns for Excel
    EXCEL_CURRENCY_FORMATS = {
        'CAD': '# ##0,00 $',
        'USD': '$#,##0.00',
        'EUR': '# ##0,00 €',
        'GBP': '£#,##0.00',
        'CHF': '# ##0,00 CHF'
    }
    currency_num_format = EXCEL_CURRENCY_FORMATS.get(company_currency, '# ##0,00 $')

    # Formats (same style as receivables export)
    header_format = workbook.add_format({
        'bold': True,
        'bg_color': '#D9E1F2',
        'border': 1,
        'align': 'center'
    })
    money_format = workbook.add_format({
        'num_format': currency_num_format,
        'border': 1
    })
    text_format = workbook.add_format({'border': 1})
    date_format = workbook.add_format({
        'num_format': 'yyyy/mm/dd',
        'border': 1,
        'align': 'center'
    })

    # Title with filter indication - UPDATED for optional original_amount and project columns
    base_cols = 5
    if include_children:
        base_cols += 1
    if is_project_enabled:
        base_cols += 1
    if has_original_amount:
        base_cols += 1

    col_letter = chr(ord('A') + base_cols - 1)
    title_range = f'A1:{col_letter}1'

    if include_children:
        title_suffix = " (Parent et filiales)"
    else:
        title_suffix = ""
    worksheet.merge_range(
        title_range, f'Factures - {client.name}{title_suffix}',
        workbook.add_format({
            'bold': True,
            'font_size': 14,
            'align': 'center'
        }))

    # Client info
    worksheet.write('A2', f'Code client: {client.code_client}', text_format)
    worksheet.write('A3',
                    f'Date d\'export: {datetime.now().strftime("%Y/%m/%d")}',
                    text_format)
    if include_children:
        includes_text = f'Inclut {len(client.child_clients)} compte(s) enfant(s)'
        worksheet.write('A4', includes_text, text_format)

    # Headers - UPDATED to include optional project and original_amount columns
    headers = ['N° Facture']
    if include_children:
        headers.append('Client')
    if is_project_enabled:
        headers.append(project_label)
    if has_original_amount:
        headers.append('Montant Original')
    headers.extend(['Date facture', 'Date échéance', 'Jours', 'Montant'])

    header_row = 5 if include_children else 4
    for col, header in enumerate(headers):
        worksheet.write(header_row, col, header, header_format)

    # Data
    row = header_row + 1
    total_amount = 0
    total_original_amount = 0

    for invoice in invoices:
        days = invoice.days_outstanding(company.aging_calculation_method)

        col = 0
        worksheet.write(row, col, invoice.invoice_number, text_format)
        col += 1

        if include_children:
            worksheet.write(row, col, invoice.client.name, text_format)
            col += 1

        if is_project_enabled:
            project_value = invoice.project_name if invoice.project_name else ''
            worksheet.write(row, col, project_value, text_format)
            col += 1

        if has_original_amount:
            if invoice.original_amount is not None:
                worksheet.write(row, col, float(invoice.original_amount),
                                money_format)
                total_original_amount += invoice.original_amount
            else:
                worksheet.write(row, col, '', text_format)
            col += 1

        worksheet.write(row, col, invoice.invoice_date, date_format)
        col += 1
        worksheet.write(row, col, invoice.due_date, date_format)
        col += 1
        days_text = f'{days} jours'
        worksheet.write(row, col, days_text, text_format)
        col += 1
        worksheet.write(row, col, float(invoice.amount), money_format)

        total_amount += invoice.amount
        row += 1

    # Add totals row
    col = 0
    worksheet.write(row, col, 'Total', header_format)
    col += 1

    if include_children:
        worksheet.write(row, col, '', header_format)
        col += 1

    if is_project_enabled:
        worksheet.write(row, col, '', header_format)
        col += 1

    if has_original_amount:
        worksheet.write(row, col, float(total_original_amount), money_format)
        col += 1

    worksheet.write(row, col, '', header_format)  # Date facture
    col += 1
    worksheet.write(row, col, '', header_format)  # Date échéance
    col += 1
    worksheet.write(row, col, '', header_format)  # Jours
    col += 1
    worksheet.write(row, col, float(total_amount), money_format)  # Montant

    # Auto-adjust column widths - UPDATED for optional project and original_amount columns
    col_idx = 0
    worksheet.set_column(col_idx, col_idx, 15)  # Invoice number
    col_idx += 1

    if include_children:
        worksheet.set_column(col_idx, col_idx, 20)  # Client name
        col_idx += 1

    if is_project_enabled:
        worksheet.set_column(col_idx, col_idx, 20)  # Project
        col_idx += 1

    if has_original_amount:
        worksheet.set_column(col_idx, col_idx, 14)  # Original amount
        col_idx += 1

    worksheet.set_column(col_idx, col_idx + 1, 12)  # Dates
    col_idx += 2
    worksheet.set_column(col_idx, col_idx, 10)  # Days
    col_idx += 1
    worksheet.set_column(col_idx, col_idx, 12)  # Amount

    workbook.close()
    output.seek(0)

    # Create response
    response = make_response(output.getvalue())
    response.headers[
        'Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers[
        'Content-Disposition'] = f'attachment; filename=factures_{client.code_client}_{datetime.now().strftime("%Y%m%d")}.xlsx'

    return response


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/clients/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_client(id):
    """Edit client"""
    from app import db
    from models import Client
    from forms import ClientForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour modifier des clients.',
            'error')
        return redirect(url_for('client.detail_client', id=id))

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    form = ClientForm(company_id=company.id, current_client_id=id, obj=client)
    if form.validate_on_submit():
        # Validate parent-child relationship before saving
        new_parent_id = form.parent_client_id.data if form.parent_client_id.data else None
        is_valid, error_msg = client.validate_parent_child_relationship(
            new_parent_id)

        if not is_valid:
            flash(f'Erreur de relation parent-enfant: {error_msg}', 'error')
            return render_template('clients/form.html',
                                   form=form,
                                   title='Modifier Client',
                                   client=client)

        client.code_client = form.code_client.data
        client.name = form.name.data
        client.email = form.email.data
        client.phone = form.phone.data
        client.address = form.address.data
        client.collector_id = form.collector_id.data if form.collector_id.data else None
        client.representative_name = form.representative_name.data
        client.payment_terms = form.payment_terms.data
        client.language = form.language.data
        client.parent_client_id = new_parent_id

        db.session.commit()
        flash(f'Client {client.name} modifié avec succès.', 'success')
        return redirect(url_for('client.detail_client', id=id))

    return render_template('clients/form.html',
                           form=form,
                           title='Modifier Client',
                           client=client)


# CRITICAL FIX: Route manquante pour delete_note_simple - ERREUR 500 TEMPLATE CORRIGÉE
@main_bp.route('/clients/<int:client_id>/notes/<int:note_id>/delete',
               methods=['POST'])
@main_bp.route('/client/<int:client_id>/note/<int:note_id>/delete',
               methods=['POST'])
@login_required
def delete_note_simple(client_id, note_id):
    """Delete note - plural path for AJAX frontend, singular for form backward compatibility"""
    from app import db
    from models import CommunicationNote

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash('Accès refusé.', 'error')
        return redirect(url_for('client.detail_client', id=client_id))

    # Get note
    note = CommunicationNote.query.filter_by(
        id=note_id, client_id=client_id, company_id=company.id).first_or_404()

    # Check permissions
    if note.user_id != current_user.id and not current_user.is_admin():
        flash('Vous pouvez seulement supprimer vos propres notes.', 'error')
        return redirect(url_for('client.detail_client', id=client_id))

    # Delete the note
    db.session.delete(note)
    db.session.commit()

    flash('Note supprimée avec succès.', 'success')
    return redirect(url_for('client.detail_client', id=client_id))


@main_bp.route('/api/email-template/<int:template_id>')
@login_required
def api_get_email_template(template_id):
    """API endpoint to get email template data"""
    from app import db
    from models import Client, EmailTemplate

    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({
                'success': False,
                'message': 'Aucune entreprise sélectionnée'
            })

        # Get template and verify access
        template = EmailTemplate.query.filter_by(
            id=template_id, company_id=company.id).first()

        if not template:
            return jsonify({'success': False, 'message': 'Modèle non trouvé'})

        # Check if user can access this template
        if not (template.is_shared or template.created_by == current_user.id
                or current_user.is_admin()):
            return jsonify({'success': False, 'message': 'Accès refusé'})

        # Si un client_id est fourni, remplacer les variables pour prévisualisation
        subject = template.subject
        content = template.content

        client_id = request.args.get('client_id')
        if client_id:
            try:
                client = Client.query.filter_by(id=int(client_id),
                                                company_id=company.id).first()
                if client:
                    # Déterminer si on inclut les enfants (peut être passé en paramètre)
                    include_children = request.args.get(
                        'include_children') == 'true' and client.is_parent

                    from utils import replace_email_variables
                    subject = replace_email_variables(
                        template.subject,
                        client=client,
                        company=company,
                        user=current_user,
                        include_children=include_children)
                    content = replace_email_variables(
                        template.content,
                        client=client,
                        company=company,
                        user=current_user,
                        include_children=include_children)
            except (ValueError, TypeError):
                pass  # Ignorer les erreurs de conversion, utiliser le template original

        return jsonify({
            'success': True,
            'subject': subject,
            'content': content
        })

    except Exception as e:
        current_app.logger.error(f"Error in api_get_email_template: {str(e)}")
        return jsonify({
            'success': False,
            'message': 'Une erreur interne est survenue'
        })


@main_bp.route('/api/note/<int:note_id>')
@login_required
def api_get_note_simple(note_id):
    """Simple API endpoint to get note data - works from any context"""
    from app import db
    from models import Client, CommunicationNote

    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({
                'success': False,
                'message': 'Aucune entreprise sélectionnée'
            })

        # Get note and verify it belongs to this company
        note = db.session.query(CommunicationNote).join(Client).filter(
            CommunicationNote.id == note_id,
            Client.company_id == company.id).first()

        if not note:
            return jsonify({'success': False, 'message': 'Note non trouvée'})

        # Check permissions - allow note author and admins to edit
        if current_user.is_read_only():
            return jsonify({
                'success': False,
                'message': 'Accès refusé - lecture seule'
            })

        if note.user_id != current_user.id and not current_user.is_admin():
            return jsonify({
                'success':
                False,
                'message':
                'Vous pouvez seulement modifier vos propres notes'
            })

        # Format dates properly for HTML date inputs
        note_date_str = None
        if note.note_date:
            try:
                if hasattr(note.note_date, 'isoformat'):
                    note_date_str = note.note_date.isoformat()
                else:
                    note_date_str = str(note.note_date)
            except Exception:
                note_date_str = str(note.note_date)

        reminder_date_str = None
        if note.reminder_date:
            try:
                if hasattr(note.reminder_date, 'date'):
                    reminder_date_str = note.reminder_date.date().isoformat()
                elif hasattr(note.reminder_date, 'isoformat'):
                    reminder_date_str = note.reminder_date.isoformat().split(
                        'T')[0]
                else:
                    reminder_date_str = str(note.reminder_date)
            except Exception:
                reminder_date_str = str(note.reminder_date)

        return jsonify({
            'success': True,
            'note_text': note.note_text or '',
            'note_type': note.note_type or 'general',
            'note_date': note_date_str,
            'reminder_date': reminder_date_str
        })

    except Exception as e:
        current_app.logger.error(f"Error in api_get_note_simple: {str(e)}")
        return jsonify({
            'success': False,
            'message': 'Une erreur interne est survenue'
        })


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/clients/<int:client_id>/notes/<int:note_id>/edit',
               methods=['GET', 'POST'])
@login_required
def edit_note_form(client_id, note_id):
    """Show edit form for a note"""
    from models import Client, CommunicationNote
    from forms import CommunicationNoteForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash('Accès refusé.', 'error')
        return redirect(url_for('client.detail_client', id=client_id))

    # Get client
    client = Client.query.filter_by(id=client_id,
                                    company_id=company.id).first_or_404()

    # Get note
    note = CommunicationNote.query.filter_by(
        id=note_id, client_id=client_id, company_id=company.id).first_or_404()

    # Check permissions
    if note.user_id != current_user.id and not current_user.is_admin():
        flash('Vous pouvez seulement modifier vos propres notes.', 'error')
        return redirect(url_for('client.detail_client', id=client_id))

    # Create form with note data
    form = CommunicationNoteForm(company_id=company.id, obj=note)
    form.note_type.data = note.note_type
    form.note_date.data = note.note_date
    form.note_text.data = note.note_text
    if note.reminder_date:
        form.reminder_date.data = note.reminder_date.date()

    return render_template('clients/edit_note.html',
                           form=form,
                           client=client,
                           note=note)


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/clients/<int:client_id>/edit-note/<int:note_id>',
               methods=['POST'])
@login_required
def edit_note_ajax(client_id, note_id):
    """Edit a communication note via AJAX (alias for frontend compatibility)"""
    from app import db
    from models import Client, CommunicationNote

    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({
                'success': False,
                'message': 'Aucune entreprise sélectionnée.'
            }), 400

        if current_user.is_read_only():
            return jsonify({'success': False, 'message': 'Accès refusé.'}), 403

        # Vérifier que le client appartient à l'entreprise
        client = Client.query.filter_by(id=client_id,
                                        company_id=company.id).first()
        if not client:
            return jsonify({
                'success': False,
                'message': 'Client non trouvé.'
            }), 404

        # Vérifier que la note existe et appartient au client
        note = CommunicationNote.query.filter_by(
            id=note_id, client_id=client_id, company_id=company.id).first()
        if not note:
            return jsonify({
                'success': False,
                'message': 'Note non trouvée.'
            }), 404

        # Vérifier les permissions (admin ou propriétaire de la note)
        if not current_user.is_admin() and note.user_id != current_user.id:
            return jsonify({
                'success': False,
                'message': 'Permissions insuffisantes.'
            }), 403

        # Mettre à jour la note
        note.note_text = request.form.get('note_text', '').strip()
        note.note_type = request.form.get('note_type', 'general')

        # Gestion de la date de note
        note_date_str = request.form.get('note_date')
        if note_date_str:
            try:
                note.note_date = datetime.strptime(note_date_str,
                                                   '%Y-%m-%d').date()
            except ValueError:
                note.note_date = datetime.utcnow().date()
        else:
            note.note_date = datetime.utcnow().date()

        # Gestion de la date de rappel
        reminder_date_str = request.form.get('reminder_date')
        if reminder_date_str:
            try:
                reminder_date = datetime.strptime(reminder_date_str,
                                                  '%Y-%m-%d').date()
                note.reminder_date = datetime.combine(
                    reminder_date,
                    datetime.min.time().replace(hour=23, minute=59))
            except ValueError:
                note.reminder_date = None
        else:
            note.reminder_date = None

        note.updated_at = datetime.utcnow()

        # Tag processing removed as @ mention system was removed

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Note modifiée avec succès.'
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error updating note {note_id}: {str(e)}')
        return jsonify({
            'success': False,
            'message': 'Erreur lors de la modification'
        }), 500


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/client/<int:id>/note/add', methods=['POST'])
@login_required
def add_note(id):
    """Add communication note to client"""
    from app import db
    from models import Client, CommunicationNote
    from forms import CommunicationNoteForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour ajouter des notes.',
            'error')
        return redirect(url_for('client.detail_client', id=id))

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    form = CommunicationNoteForm(company_id=company.id)
    if form.validate_on_submit():
        # Set reminder_date to 23:59 if date is provided
        reminder_datetime = None
        if form.reminder_date.data:
            reminder_datetime = datetime.combine(
                form.reminder_date.data,
                datetime.min.time().replace(hour=23, minute=59))

        note = CommunicationNote()
        note.client_id = id
        note.user_id = current_user.id
        note.note_text = form.note_text.data.strip(
        ) if form.note_text.data else ''
        note.note_type = form.note_type.data
        note.note_date = form.note_date.data
        note.reminder_date = reminder_datetime
        note.company_id = company.id

        db.session.add(note)
        db.session.commit()

        # Message de succès avec détails
        success_msg = 'Note ajoutée avec succès.'
        if form.reminder_date.data:
            success_msg += f' Rappel programmé pour le {form.reminder_date.data.strftime("%d/%m/%Y")}.'

        flash(success_msg, 'success')

        # Check if we need to return to receivables page
        return_to = request.args.get('return_to')
        if return_to == 'receivables':
            return redirect(url_for('receivable.overview'))
        else:
            return redirect(url_for('client.detail_client', id=id))

    # Afficher les erreurs spécifiques du formulaire
    for field, errors in form.errors.items():
        for error in errors:
            flash(f'Erreur {field}: {error}', 'error')

    # Check where to redirect on error too
    return_to = request.args.get('return_to')
    if return_to == 'receivables':
        return redirect(url_for('receivable.overview'))
    else:
        return redirect(url_for('client.detail_client', id=id))


@main_bp.route('/api/team-members')
@login_required
def api_team_members():
    """API endpoint to get team members for @ mentions"""
    from app import db
    company = current_user.get_selected_company()
    if not company:
        return jsonify({
            'success': False,
            'message': 'Aucune entreprise sélectionnée'
        })

    from models import UserCompany, User
    team_members = db.session.query(User).join(UserCompany).filter(
        UserCompany.company_id == company.id).all()

    members_data = []
    for user in team_members:
        members_data.append({
            'id': user.id,
            'name': f"{user.first_name} {user.last_name}",
            'role': user.role
        })

    return jsonify({'success': True, 'members': members_data})


@main_bp.route('/client/<int:id>/invoice/add', methods=['POST'])
@login_required
def add_invoice(id):
    """Add invoice to client"""
    from app import db
    from models import Client, Invoice
    from forms import InvoiceForm
    from sqlalchemy.exc import IntegrityError

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour ajouter des factures.',
            'error')
        return redirect(url_for('client.detail_client', id=id))

    client = Client.query.filter_by(id=id,
                                    company_id=company.id).first_or_404()

    form = InvoiceForm(company_id=company.id)
    if form.validate_on_submit():
        invoice = Invoice()
        invoice.invoice_number = form.invoice_number.data
        invoice.client_id = id
        invoice.original_amount = form.original_amount.data
        invoice.amount = form.amount.data
        invoice.invoice_date = form.invoice_date.data
        invoice.due_date = form.due_date.data
        invoice.project_name = form.project_name.data if form.project_name.data else None
        invoice.company_id = company.id
        db.session.add(invoice)

        try:
            db.session.commit()
            flash(f'Facture {invoice.invoice_number} ajoutée avec succès.',
                  'success')
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.error(f"Duplicate invoice error: {str(e)}")
            flash(
                f'Cette facture ({invoice.invoice_number}) existe déjà pour ce client.',
                'error')
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error adding invoice: {str(e)}")
            flash('Erreur lors de l\'ajout de la facture. Veuillez réessayer.',
                  'error')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'Erreur {field}: {error}', 'error')

    return redirect(url_for('client.detail_client', id=id))


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/clients/<int:client_id>/contact/add', methods=['POST'])
@login_required
def add_contact(client_id):
    """Add a new contact to a client"""
    from app import db
    from models import Client, ClientContact
    from forms import ClientContactForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour ajouter des contacts.',
            'error')
        return redirect(url_for('client.detail_client', id=client_id))

    client = Client.query.filter_by(id=client_id,
                                    company_id=company.id).first_or_404()

    try:
        # Get form data
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        position = request.form.get('position', '').strip()
        language = request.form.get('language', 'fr')  # Default to French
        # Safe boolean conversion - only accept explicit truthy values
        is_primary_value = request.form.get('is_primary', '').strip().lower()
        is_primary = is_primary_value in ('1', 'true', 'on', 'yes')
        campaign_allowed_value = request.form.get('campaign_allowed',
                                                  '').strip().lower()
        campaign_allowed = campaign_allowed_value in ('1', 'true', 'on', 'yes')

        if not first_name or not last_name or not email:
            flash('Le prénom, nom et courriel sont requis.', 'error')
            return redirect(url_for('client.detail_client', id=client_id))

        # If setting as primary, remove primary flag from other contacts
        if is_primary:
            ClientContact.query.filter_by(client_id=client_id,
                                          company_id=company.id).update(
                                              {'is_primary': False})

        # Create new contact - PHASE 4 : constructeur corrigé
        contact = ClientContact()
        contact.client_id = client_id
        contact.company_id = company.id
        contact.first_name = first_name
        contact.last_name = last_name
        contact.email = email
        contact.phone = phone or None
        contact.position = position or None
        contact.language = language
        contact.is_primary = is_primary
        contact.campaign_allowed = campaign_allowed

        db.session.add(contact)
        db.session.commit()
        flash('Contact ajouté avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error adding contact for client {client_id}: {e}")
        flash('Une erreur est survenue lors de l\'ajout du contact. Veuillez reessayer.', 'error')

    return redirect(url_for('client.detail_client', id=client_id))


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/clients/<int:client_id>/contact/<int:contact_id>/edit',
               methods=['POST'])
@login_required
def edit_contact(client_id, contact_id):
    """Edit a client contact"""
    from app import db
    from models import Client, ClientContact
    from forms import ClientContactForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour modifier des contacts.',
            'error')
        return redirect(url_for('client.detail_client', id=client_id))

    client = Client.query.filter_by(id=client_id,
                                    company_id=company.id).first_or_404()
    contact = ClientContact.query.filter_by(
        id=contact_id, client_id=client_id,
        company_id=company.id).first_or_404()

    try:
        # Get form data
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        position = request.form.get('position', '').strip()
        language = request.form.get('language', 'fr')  # Default to French
        # Safe boolean conversion - only accept explicit truthy values
        is_primary_value = request.form.get('is_primary', '').strip().lower()
        is_primary = is_primary_value in ('1', 'true', 'on', 'yes')
        campaign_allowed_value = request.form.get('campaign_allowed',
                                                  '').strip().lower()
        campaign_allowed = campaign_allowed_value in ('1', 'true', 'on', 'yes')

        if not first_name or not last_name or not email:
            flash('Le prénom, nom et courriel sont requis.', 'error')
            return redirect(url_for('client.detail_client', id=client_id))

        # If setting as primary, remove primary flag from other contacts
        if is_primary and not contact.is_primary:
            ClientContact.query.filter_by(
                client_id=client_id, company_id=company.id).filter(
                    ClientContact.id != contact_id).update(
                        {'is_primary': False})

        # Update contact
        contact.first_name = first_name
        contact.last_name = last_name
        contact.email = email
        contact.phone = phone or None
        contact.position = position or None
        contact.language = language
        contact.is_primary = is_primary
        contact.campaign_allowed = campaign_allowed
        contact.updated_at = datetime.utcnow()

        db.session.commit()
        flash('Contact modifié avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error editing contact {contact_id} for client {client_id}: {e}")
        flash('Une erreur est survenue lors de la modification du contact. Veuillez reessayer.', 'error')

    return redirect(url_for('client.detail_client', id=client_id))


# REMOVED: client_bp routes moved to views/client_views.py
# This eliminates function conflicts between main views.py and modular architecture


@main_bp.route('/clients/<int:client_id>/contact/<int:contact_id>/delete',
               methods=['POST'])
@login_required
def delete_contact(client_id, contact_id):
    """Delete a client contact"""
    from app import db
    from models import Client, ClientContact

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash(
            'Accès refusé. Vous n\'avez pas les permissions pour supprimer des contacts.',
            'error')
        return redirect(url_for('client.detail_client', id=client_id))

    client = Client.query.filter_by(id=client_id,
                                    company_id=company.id).first_or_404()
    contact = ClientContact.query.filter_by(
        id=contact_id, client_id=client_id,
        company_id=company.id).first_or_404()

    try:
        db.session.delete(contact)
        db.session.commit()
        flash('Contact supprimé avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting contact {contact_id} for client {client_id}: {e}")
        flash('Une erreur est survenue lors de la suppression du contact. Veuillez reessayer.', 'error')

    return redirect(url_for('client.detail_client', id=client_id))


@main_bp.errorhandler(500)
def internal_error(error):
    from app import db
    db.session.rollback()
    return render_template('errors/500.html'), 500


@profile_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """User profile settings"""
    from forms import UserProfileForm
    from models import EmailConfiguration, User

    form = UserProfileForm(obj=current_user)
    if form.validate_on_submit():
        # Check for duplicate email before committing
        new_email = form.email.data
        if new_email != current_user.email:
            existing = User.query.filter(User.email == new_email, User.id != current_user.id).first()
            if existing:
                flash('Ce courriel est déjà utilisé par un autre compte.', 'error')
                return redirect(url_for('profile.settings'))

        current_user.first_name = form.first_name.data
        current_user.last_name = form.last_name.data
        current_user.email = new_email
        db.session.commit()
        flash('Profil mis à jour avec succès.', 'success')
        return redirect(url_for('profile.settings'))

    # Check company-level OAuth status
    company = current_user.get_selected_company()
    company_oauth_valid = False
    if company:
        email_config = EmailConfiguration.query.filter_by(
            user_id=current_user.id, company_id=company.id).first()
        if email_config and email_config.outlook_oauth_access_token and email_config.outlook_oauth_token_expires:
            company_oauth_valid = email_config.outlook_oauth_token_expires > datetime.now(
            )

    # Security section: TOTP and recovery codes
    from models import UserTOTP, RecoveryCode
    totp_active = UserTOTP.query.filter_by(user_id=current_user.id, is_active=True).first() is not None
    recovery_count = RecoveryCode.query.filter_by(user_id=current_user.id, used=False).count()

    return render_template('profile/settings.html',
                           form=form,
                           company_oauth_valid=company_oauth_valid,
                           company=company,
                           totp_active=totp_active,
                           recovery_count=recovery_count)


# Legal routes - moved to main blueprint for better accessibility
@main_bp.route('/legal/cgu')
def legal_cgu():
    """Conditions Générales d'Utilisation"""
    return render_template('legal/terms.html')


@main_bp.route('/legal/confidentialite')
def legal_confidentialite():
    """Politique de Confidentialité"""
    return render_template('legal/privacy.html')


@main_bp.route('/legal/cookies')
def legal_cookies():
    """Politique de Cookies"""
    return render_template('legal/cookies.html')


@profile_bp.route('/email-configuration', methods=['GET', 'POST'])
@login_required
def email_configuration():
    """Email configuration for current user and company"""
    from app import db
    from forms import EmailConfigurationForm
    from models import EmailConfiguration

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('main.dashboard'))

    # Check permissions for email connection
    from permissions import PermissionService
    can_access, restriction_reason = PermissionService.can_access_feature(
        current_user, company, 'email_connection')
    if not can_access:
        message = PermissionService.get_restriction_message(restriction_reason)
        flash(message, 'error')
        return redirect(url_for('main.dashboard'))

    # Get or create email configuration for this user and company
    # Use a fresh query to avoid cache issues with disconnect
    email_config = db.session.query(EmailConfiguration).filter_by(
        user_id=current_user.id, company_id=company.id).first()

    if not email_config:
        email_config = EmailConfiguration(user_id=current_user.id,
                                          company_id=company.id)
        db.session.add(email_config)
        db.session.commit()

    form = EmailConfigurationForm(obj=email_config)

    if form.validate_on_submit():
        current_app.logger.info(f"=== DÉBUT SAUVEGARDE EMAIL CONFIG ===")
        current_app.logger.info(
            f"User: {current_user.id}, Company: {company.id}")
        current_app.logger.info(f"Email config ID avant: {email_config.id}")
        current_app.logger.info(
            f"Signature avant: {email_config.email_signature[:100] if email_config.email_signature else 'None'}..."
        )

        email_config.outlook_email = form.outlook_email.data
        email_config.gmail_email = form.gmail_email.data

        # Signature management - manual editing only

        # Update signature from form data avec validation de taille
        form_signature = form.email_signature.data or ''

        # Validation de la taille de la signature (limite: 2.5MB)
        signature_size = len(form_signature.encode('utf-8'))
        max_signature_size = 2.5 * 1024 * 1024  # 2.5MB

        current_app.logger.info(
            f"Taille signature: {signature_size} bytes (max: {max_signature_size})"
        )

        if signature_size > max_signature_size:
            flash(
                'La signature est trop volumineuse (max 1MB). Veuillez réduire la taille des images.',
                'error')
            return render_template('profile/email_signature_templates.html',
                                   form=form)

        current_app.logger.info(
            f"Mise à jour signature depuis formulaire: {len(form_signature)} caractères"
        )
        current_app.logger.info(
            f"Contenu signature formulaire: {form_signature[:200]}...")

        # NOUVEAU: Convertir les images locales en Base64 pour survie aux redéploiements
        from utils import convert_signature_images_to_base64
        form_signature = convert_signature_images_to_base64(form_signature)

        email_config.email_signature = form_signature

        current_app.logger.info(
            f"Signature finale (après conversion Base64): {email_config.email_signature[:100] if email_config.email_signature else 'None'}..."
        )

        # Sauvegarder les autres changements
        db.session.commit()

        # Rafraîchir l'objet pour avoir les dernières données
        db.session.refresh(email_config)

        current_app.logger.info(f"=== SAUVEGARDE TERMINÉE ===")
        current_app.logger.info(
            f"Signature après rafraîchissement: {len(email_config.email_signature)} caractères"
        )
        flash('Configuration de courriel mise à jour avec succès.', 'success')
        return redirect(url_for('profile.email_configuration'))

    # Force refresh de l'objet pour éviter les problèmes de cache après déconnexion
    if email_config.id:  # Seulement si l'objet existe déjà en DB
        db.session.refresh(email_config)

    # Vérifier le statut OAuth pour cette entreprise
    oauth_connected = False
    if email_config.outlook_oauth_access_token and email_config.outlook_oauth_token_expires:
        oauth_connected = email_config.outlook_oauth_token_expires > datetime.now(
        )

    return render_template('profile/email_configuration.html',
                           form=form,
                           company=company,
                           email_config=email_config,
                           oauth_connected=oauth_connected)


# # ============================================================================
# # SYSTÈME OAUTH COMPANY-LEVEL POUR EMAILCONFIGURATION
# # ============================================================================


@profile_bp.route("/outlook/connect")
@login_required
def outlook_connect():
    """Connecter Outlook pour l'entreprise actuelle ou système email admin"""
    from models import EmailConfiguration, SystemEmailConfiguration

    # Check if this is a system email OAuth flow (from admin panel)
    system_email_oauth_flow = session.get('system_email_oauth_flow', False)

    if system_email_oauth_flow:
        # System email flow - no company or plan restrictions
        if not current_user.is_superuser:
            flash("Accès non autorisé.", "error")
            return redirect(url_for("admin.system_email_configs"))
    else:
        # Regular company email flow
        company = current_user.get_selected_company()
        if not company:
            flash("Aucune entreprise sélectionnée.", "error")
            return redirect(url_for("profile.email_configuration"))

        # Check permissions for email connection
        from permissions import PermissionService
        can_access, restriction_reason = PermissionService.can_access_feature(
            current_user, company, 'email_connection')
        if not can_access:
            message = PermissionService.get_restriction_message(
                restriction_reason)
            flash(message, 'error')
            return redirect(url_for('profile.email_configuration'))

    if system_email_oauth_flow:
        # System email configuration flow
        system_config_id = session.get('system_email_config_id')
        if not system_config_id:
            flash("Configuration système introuvable.", "error")
            return redirect(url_for("admin.system_email_configs"))

        system_config = SystemEmailConfiguration.query.get(system_config_id)
        if not system_config:
            flash("Configuration système introuvable.", "error")
            return redirect(url_for("admin.system_email_configs"))

        # Store system config info for callback
        session['oauth_target_email'] = system_config.email_address
        session['oauth_system_config_id'] = system_config_id
    else:
        # Regular company email flow
        # Récupérer la configuration email
        email_config = EmailConfiguration.query.filter_by(
            user_id=current_user.id, company_id=company.id).first()

        if not email_config or not email_config.outlook_email:
            flash("Veuillez d'abord saisir votre adresse Outlook.", "error")
            return redirect(url_for("profile.email_configuration"))

        # Stocker les informations dans la session pour le callback
        session['oauth_company_id'] = company.id
        session['oauth_target_email'] = email_config.outlook_email

    try:
        from microsoft_oauth import MicrosoftOAuthConnector
        connector = MicrosoftOAuthConnector()
        auth_url = connector.get_authorization_url()
        return redirect(auth_url)
    except Exception as e:
        current_app.logger.error(f"Error connecting Outlook OAuth: {e}")
        flash('Une erreur est survenue lors de la connexion. Veuillez reessayer.', 'error')
        return redirect(url_for('profile.email_configuration'))


@profile_bp.route("/outlook/disconnect", methods=['POST'])
@login_required
def outlook_disconnect():
    """Déconnecter Outlook pour l'entreprise actuelle"""
    from app import db
    from models import EmailConfiguration

    current_app.logger.info(f"=== ROUTE OUTLOOK_DISCONNECT APPELÉE ===")
    current_app.logger.info(f"Method: {request.method}")
    current_app.logger.info(
        f"User: {current_user.id if current_user.is_authenticated else 'Anonymous'}"
    )

    company = current_user.get_selected_company()
    if not company:
        flash("Aucune entreprise sélectionnée.", "error")
        return redirect(url_for("profile.email_configuration"))

    # Supprimer les tokens OAuth en utilisant l'objet directement
    email_config = EmailConfiguration.query.filter_by(
        user_id=current_user.id, company_id=company.id).first()

    if email_config:
        current_app.logger.info(
            f"=== DEBUT DECONNEXION - SUPPRESSION COMPLETE ===")
        current_app.logger.info(
            f"User: {current_user.id}, Company: {company.id}")
        current_app.logger.info(f"Email config ID: {email_config.id}")
        current_app.logger.info(
            f"Avant suppression - Email: {email_config.outlook_email}")

        # Supprimer complètement la configuration email
        db.session.delete(email_config)
        db.session.commit()

        current_app.logger.info(f"Configuration email supprimée complètement")

        # Vérifier que la suppression a bien eu lieu
        verification = db.session.query(EmailConfiguration).filter_by(
            user_id=current_user.id, company_id=company.id).first()
        current_app.logger.info(
            f"Vérification après suppression - Config exists: {verification is not None}"
        )
        current_app.logger.info(f"=== FIN DECONNEXION ===")

        flash(
            "Connexion Outlook déconnectée. Vous devrez reconfigurer votre adresse email.",
            "success")
    else:
        flash("Aucune configuration trouvée.", "warning")

    return redirect(url_for('profile.email_configuration'))


@profile_bp.route("/gmail/connect", methods=['POST'])
@login_required
def gmail_connect():
    """Configurer Gmail SMTP pour l'entreprise actuelle"""
    from models import EmailConfiguration
    from app import db

    company = current_user.get_selected_company()
    if not company:
        flash("Aucune entreprise sélectionnée.", "error")
        return redirect(url_for("profile.email_configuration"))

    # Check permissions for email connection
    from permissions import PermissionService
    can_access, restriction_reason = PermissionService.can_access_feature(
        current_user, company, 'email_connection')
    if not can_access:
        message = PermissionService.get_restriction_message(restriction_reason)
        flash(message, 'error')
        return redirect(url_for('profile.email_configuration'))

    # Récupérer la configuration email
    email_config = EmailConfiguration.query.filter_by(
        user_id=current_user.id, company_id=company.id).first()

    if not email_config or not email_config.gmail_email:
        flash("Veuillez d'abord saisir votre adresse Gmail.", "error")
        return redirect(url_for("profile.email_configuration"))

    # Vérifier si Outlook est déjà connecté
    if email_config.is_outlook_connected():
        flash("Vous devez déconnecter Outlook avant de connecter Gmail.",
              "warning")
        return redirect(url_for("profile.email_configuration"))

    # Récupérer le mot de passe d'application depuis le formulaire
    app_password = request.form.get('gmail_app_password')

    if not app_password:
        flash("Veuillez fournir un mot de passe d'application Gmail.", "error")
        return redirect(url_for("profile.email_configuration"))

    # Tester la connexion SMTP avant de sauvegarder
    try:
        from gmail_smtp import GmailSMTPConnector
        connector = GmailSMTPConnector()

        if connector.test_connection(email_config.gmail_email, app_password):
            # Sauvegarder le mot de passe d'application (chiffré)
            email_config.gmail_smtp_app_password = app_password

            # Effacer les anciens tokens OAuth s'ils existent
            email_config.gmail_oauth_access_token = None
            email_config.gmail_oauth_refresh_token = None
            email_config.gmail_oauth_token_expires = None
            email_config.gmail_oauth_connected_at = None

            db.session.commit()

            flash(
                f'Connexion Gmail SMTP réussie pour {email_config.gmail_email}',
                'success')
        else:
            flash(
                "Échec de la connexion Gmail SMTP. Vérifiez votre mot de passe d'application.",
                "error")
    except Exception as e:
        current_app.logger.error(f"Error connecting Gmail SMTP: {e}")
        flash('Une erreur est survenue lors de la connexion Gmail SMTP. Veuillez reessayer.', 'error')

    return redirect(url_for('profile.email_configuration'))


@profile_bp.route("/gmail/disconnect", methods=['POST'])
@login_required
def gmail_disconnect():
    """Déconnecter Gmail pour l'entreprise actuelle"""
    from app import db
    from models import EmailConfiguration

    current_app.logger.info(f"=== ROUTE GMAIL_DISCONNECT APPELÉE ===")
    current_app.logger.info(f"Method: {request.method}")
    current_app.logger.info(
        f"User: {current_user.id if current_user.is_authenticated else 'Anonymous'}"
    )

    company = current_user.get_selected_company()
    if not company:
        flash("Aucune entreprise sélectionnée.", "error")
        return redirect(url_for("profile.email_configuration"))

    # Supprimer les tokens OAuth Gmail
    email_config = EmailConfiguration.query.filter_by(
        user_id=current_user.id, company_id=company.id).first()

    if email_config:
        current_app.logger.info(
            f"=== DEBUT DECONNEXION GMAIL - SUPPRESSION TOKENS ===")
        current_app.logger.info(
            f"User: {current_user.id}, Company: {company.id}")
        current_app.logger.info(f"Email config ID: {email_config.id}")
        current_app.logger.info(
            f"Avant suppression - Email: {email_config.gmail_email}")

        # Supprimer uniquement le mot de passe SMTP Gmail (garder l'email pour reconfiguration)
        email_config.gmail_smtp_app_password = None

        # Supprimer aussi les anciens tokens OAuth s'ils existent
        email_config.gmail_oauth_access_token = None
        email_config.gmail_oauth_refresh_token = None
        email_config.gmail_oauth_token_expires = None
        email_config.gmail_oauth_connected_at = None

        db.session.commit()

        current_app.logger.info(f"Configuration Gmail SMTP supprimée")
        current_app.logger.info(f"=== FIN DECONNEXION GMAIL ===")

        flash(
            "Connexion Gmail déconnectée. Vous pouvez reconnecter Gmail ou Outlook.",
            "success")
    else:
        flash("Aucune configuration trouvée.", "warning")

    return redirect(url_for('profile.email_configuration'))


# # ============================================================================
# # NOUVELLES ROUTES OAUTH PAR ENTREPRISE (Phase 2 - Migration)
# # ============================================================================


@main_bp.route('/mes-entreprises')
@login_required
def my_companies():
    """Page listant les entreprises dont l'utilisateur est super_admin"""
    from models import Company, UserCompany

    # Récupérer les entreprises où l'utilisateur est super_admin
    super_admin_companies = db.session.query(Company).join(UserCompany).filter(
        UserCompany.user_id == current_user.id,
        UserCompany.role == 'super_admin',
        UserCompany.is_active == True).order_by(
            Company.created_at.desc()).all()

    return render_template('my_companies.html',
                           companies=super_admin_companies)


