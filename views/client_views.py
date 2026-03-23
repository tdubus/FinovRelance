"""
Client management views for the Flask application
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, make_response
from flask_login import login_required, current_user
from datetime import datetime
import logging
from app import limiter
from utils.audit_service import log_action, log_client_action, AuditActions, EntityTypes

# Create blueprint
client_bp = Blueprint('client', __name__, url_prefix='/clients')

# Helper function for SQL-based aged balance calculation
def get_aged_balances_sql(client_ids, calculation_method='invoice_date'):
    """
    Calculate aged balances for multiple clients in SQL (replaces Python loops)
    Returns: dict[client_id] -> {'current': 0, '30_days': 0, '60_days': 0, '90_days': 0, 'over_90_days': 0}
    """
    from app import db
    from models import Invoice
    from sqlalchemy import case, func, and_

    # Build CASE statements for each aging bucket (PostgreSQL date arithmetic)
    # In PostgreSQL, CURRENT_DATE - date_column returns integer (number of days)
    # Use COALESCE to treat NULL dates as current (0 days overdue)
    if calculation_method == 'invoice_date':
        days_overdue_expr = func.coalesce(func.current_date() - Invoice.invoice_date, 0)
    else:  # due_date
        days_overdue_expr = func.coalesce(func.current_date() - Invoice.due_date, 0)

    # Query with conditional aggregation
    results = db.session.query(
        Invoice.client_id,
        func.sum(case((days_overdue_expr <= 0, Invoice.amount), else_=0)).label('current'),
        func.sum(case((and_(days_overdue_expr > 0, days_overdue_expr <= 30), Invoice.amount), else_=0)).label('30_days'),
        func.sum(case((and_(days_overdue_expr > 30, days_overdue_expr <= 60), Invoice.amount), else_=0)).label('60_days'),
        func.sum(case((and_(days_overdue_expr > 60, days_overdue_expr <= 90), Invoice.amount), else_=0)).label('90_days'),
        func.sum(case((days_overdue_expr > 90, Invoice.amount), else_=0)).label('over_90_days')
    ).filter(
        Invoice.client_id.in_(client_ids),
        Invoice.is_paid == False
    ).group_by(Invoice.client_id).all()

    # Convert to dictionary
    balances_dict = {}
    for row in results:
        balances_dict[row.client_id] = {
            'current': float(row.current or 0),
            '30_days': float(getattr(row, '30_days') or 0),
            '60_days': float(getattr(row, '60_days') or 0),
            '90_days': float(getattr(row, '90_days') or 0),
            'over_90_days': float(row.over_90_days or 0)
        }

    # Fill in zeros for clients with no unpaid invoices
    for client_id in client_ids:
        if client_id not in balances_dict:
            balances_dict[client_id] = {
                'current': 0,
                '30_days': 0,
                '60_days': 0,
                '90_days': 0,
                'over_90_days': 0
            }

    return balances_dict

def get_aged_balances_by_project_sql(client_ids, company_id, calculation_method='invoice_date'):
    """
    Calculate aged balances grouped by project for given clients
    Normalizes NULL and empty project names to "(Sans projet)"
    SECURITY: Enforces company_id isolation to prevent cross-tenant data leakage
    Returns: list of dicts [{'project_name': str, 'current': float, '30_days': float, ...}]
    """
    from app import db
    from models import Invoice
    from sqlalchemy import case, func, and_

    # SECURITY: Return empty list if no client_ids or no company_id provided
    if not client_ids or not company_id:
        return []

    # Build CASE statements for aging buckets
    if calculation_method == 'invoice_date':
        days_overdue_expr = func.coalesce(func.current_date() - Invoice.invoice_date, 0)
    else:  # due_date
        days_overdue_expr = func.coalesce(func.current_date() - Invoice.due_date, 0)

    # Normalize project_name: treat NULL and empty as "(Sans projet)"
    normalized_project = case(
        (func.coalesce(func.trim(Invoice.project_name), '') == '', '(Sans projet)'),
        else_=func.trim(Invoice.project_name)
    )

    # Query with conditional aggregation grouped by project
    # SECURITY: MUST filter by company_id to prevent cross-tenant data access
    results = db.session.query(
        normalized_project.label('project_name'),
        func.sum(case((days_overdue_expr <= 0, Invoice.amount), else_=0)).label('current'),
        func.sum(case((and_(days_overdue_expr > 0, days_overdue_expr <= 30), Invoice.amount), else_=0)).label('30_days'),
        func.sum(case((and_(days_overdue_expr > 30, days_overdue_expr <= 60), Invoice.amount), else_=0)).label('60_days'),
        func.sum(case((and_(days_overdue_expr > 60, days_overdue_expr <= 90), Invoice.amount), else_=0)).label('90_days'),
        func.sum(case((days_overdue_expr > 90, Invoice.amount), else_=0)).label('over_90_days')
    ).filter(
        Invoice.client_id.in_(client_ids),
        Invoice.company_id == company_id,  # SECURITY: Enforce company isolation
        Invoice.is_paid == False
    ).group_by(normalized_project).all()

    # Convert to list of dicts with totals
    projects_list = []
    for row in results:
        current = float(row.current or 0)
        days_30 = float(getattr(row, '30_days') or 0)
        days_60 = float(getattr(row, '60_days') or 0)
        days_90 = float(getattr(row, '90_days') or 0)
        over_90 = float(row.over_90_days or 0)

        projects_list.append({
            'project_name': row.project_name,
            'current': current,
            '30_days': days_30,
            '60_days': days_60,
            '90_days': days_90,
            'over_90_days': over_90,
            'total': current + days_30 + days_60 + days_90 + over_90
        })

    # Sort: named projects alphabetically, "(Sans projet)" last
    projects_list.sort(key=lambda x: ('z' if x['project_name'] == '(Sans projet)' else x['project_name']))

    return projects_list

@client_bp.route('/')
@login_required
def list_clients():
    """List clients page - LAZY LOADING (clients loaded via JavaScript)"""
    from models import UserCompany

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get filter parameters from URL (for state restoration)
    search = request.args.get('search', '').strip()
    balance_filter = request.args.get('balance_filter', '')
    collector_filter = request.args.get('collector_filter', '')

    # Get all active users of this company (potential collectors)
    # Security: Only users who are members of this company
    collectors = []
    for uc in company.user_companies:
        if uc.is_active and uc.user:
            collectors.append(uc.user)

    # Sort by full name
    collectors.sort(key=lambda u: (u.first_name or '', u.last_name or ''))

    # Return empty page - clients will be loaded via JavaScript API
    return render_template('clients/list.html',
                         search=search,
                         balance_filter=balance_filter,
                         collector_filter=collector_filter,
                         collectors=collectors)

@client_bp.route('/api/load')
@limiter.exempt
@login_required
def api_load_clients():
    """API endpoint to load clients dynamically - OPTIMIZED"""
    try:
        from app import db
        from models import Client, Invoice
        from sqlalchemy import func
        from sqlalchemy.orm import joinedload
        import logging

        logger = logging.getLogger(__name__)

        company = current_user.get_selected_company()
        if not company:
            return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

        # Get filter parameters
        search = request.args.get('search', '').strip()
        balance_filter = request.args.get('balance_filter', '')
        collector_filter = request.args.get('collector_filter', '')
        page = request.args.get('page', 1, type=int)
        per_page = 20

        # OPTIMIZATION: Create subquery to calculate outstanding balance in SQL
        outstanding_subquery = db.session.query(
            Invoice.client_id,
            func.sum(Invoice.amount).label('total_outstanding')
        ).filter(
            Invoice.company_id == company.id,
            Invoice.is_paid == False
        ).group_by(Invoice.client_id).subquery()

        # OPTIMIZATION: Build optimized query with LEFT JOIN and eager loading
        query = db.session.query(
            Client,
            func.coalesce(outstanding_subquery.c.total_outstanding, 0).label('outstanding_balance')
        ).outerjoin(
            outstanding_subquery, Client.id == outstanding_subquery.c.client_id
        ).filter(
            Client.company_id == company.id
        ).options(
            joinedload(Client.collector)
        )

        # Apply search filter
        if search:
            search_filter = f"%{search}%"
            query = query.filter(
                (Client.name.ilike(search_filter)) |
                (Client.code_client.ilike(search_filter))
            )

        # Apply balance filter
        if balance_filter == 'with_balance':
            query = query.filter(outstanding_subquery.c.total_outstanding > 0)
        elif balance_filter == 'without_balance':
            query = query.filter(
                (outstanding_subquery.c.total_outstanding == None) |
                (outstanding_subquery.c.total_outstanding == 0)
            )

        # Apply collector filter (with company membership validation)
        if collector_filter:
            if collector_filter == 'unassigned':
                query = query.filter(Client.collector_id.is_(None))
            else:
                try:
                    collector_id = int(collector_filter)
                    # Security: Validate collector belongs to this company
                    from models import UserCompany
                    is_valid_collector = UserCompany.query.filter_by(
                        user_id=collector_id,
                        company_id=company.id,
                        is_active=True
                    ).first() is not None

                    if not is_valid_collector:
                        # Security: Reject invalid collector ID with 400 error
                        return jsonify({
                            'clients': [],
                            'pagination': {'total': 0, 'pages': 0, 'current': page, 'per_page': per_page, 'has_prev': False, 'has_next': False},
                            'error': 'Collecteur invalide'
                        }), 400

                    query = query.filter(Client.collector_id == collector_id)
                except (ValueError, TypeError):
                    return jsonify({
                        'clients': [],
                        'pagination': {'total': 0, 'pages': 0, 'current': page, 'per_page': per_page, 'has_prev': False, 'has_next': False},
                        'error': 'Format de collecteur invalide'
                    }), 400

        # Order by code client
        query = query.order_by(Client.code_client.asc())

        # Execute pagination
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Build JSON response - materialize properties while session is active
        clients_data = []
        for item in pagination.items:
            client, outstanding_balance = item

            # Materialize collector_name from eagerly loaded relationship (with safety check)
            try:
                collector_display = client.collector.full_name if client.collector else None
            except Exception as e:
                logger.warning(f"Error getting collector full_name for client {client.id}: {e}")
                collector_display = None

            clients_data.append({
                'id': client.id,
                'code_client': client.code_client,
                'name': client.name,
                'email': client.email,
                'phone': client.phone,
                'payment_terms': client.payment_terms,
                'collector_name': collector_display,
                'representative_name': client.representative_name,
                'outstanding_balance': float(outstanding_balance) if outstanding_balance else 0
            })

        return jsonify({
            'clients': clients_data,
            'pagination': {
                'page': pagination.page,
                'pages': pagination.pages,
                'total': pagination.total,
                'per_page': pagination.per_page,
                'has_prev': pagination.has_prev,
                'has_next': pagination.has_next,
                'prev_num': pagination.prev_num,
                'next_num': pagination.next_num
            }
        })

    except Exception as e:
        # Log the error and return JSON error response instead of HTML
        import logging
        import traceback
        logger = logging.getLogger(__name__)
        logger.error(f"Error in api_load_clients: {e}")
        logger.error(traceback.format_exc())

        return jsonify({
            'error': 'Une erreur est survenue lors du chargement des clients',
            'clients': [],
            'pagination': {
                'page': 1,
                'pages': 0,
                'total': 0,
                'per_page': 20,
                'has_prev': False,
                'has_next': False,
                'prev_num': None,
                'next_num': None
            }
        }), 500

@client_bp.route('/<int:id>')
@login_required
def detail_client(id):
    """Show client details - OPTIMIZED with eager loading"""
    from app import db
    from models import Client, Invoice, CommunicationNote, ClientContact
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload, joinedload

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # OPTIMIZATION: Eager load relationships to avoid N+1 queries
    client = Client.query.filter_by(id=id, company_id=company.id).options(
        selectinload(Client.child_clients).selectinload(Client.collector),  # Load children + their collectors
        joinedload(Client.parent_client),  # Load parent if exists
        joinedload(Client.collector)  # Load collector
    ).first()

    if not client:
        flash('Client introuvable.', 'error')
        return redirect(url_for('client.list_clients'))

    # Check if we should include children's data (needed before queries)
    # Support both query param styles: include_children=true and view_filter=parent_children
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = (
        request.args.get('include_children', 'false').lower() == 'true'
        or view_filter == 'parent_children'
    ) and client.is_parent

    # Determine which client IDs to query
    query_client_ids = [client.id]
    if include_children and client.child_clients:
        query_client_ids.extend([child.id for child in client.child_clients if child.company_id == company.id])

    # Get invoices with pagination (parent + children if included)
    page = request.args.get('page', 1, type=int)
    if len(query_client_ids) == 1:
        invoices_query = Invoice.query.filter_by(client_id=id)
    else:
        invoices_query = Invoice.query.filter(Invoice.client_id.in_(query_client_ids))
    invoices_pagination = invoices_query.order_by(Invoice.invoice_date.desc()).paginate(
        page=page, per_page=10, error_out=False
    )

    # Get recent notes (parent + children if included)
    if len(query_client_ids) == 1:
        notes = CommunicationNote.query.filter_by(client_id=id).order_by(
            CommunicationNote.created_at.desc()
        ).limit(5).all()
    else:
        notes = CommunicationNote.query.filter(
            CommunicationNote.client_id.in_(query_client_ids)
        ).order_by(CommunicationNote.created_at.desc()).limit(5).all()

    # OPTIMIZATION: Get all contacts in one query (client + children always)
    all_contact_ids = [client.id]
    if client.child_clients:
        all_contact_ids.extend([child.id for child in client.child_clients])

    contacts = ClientContact.query.filter(ClientContact.client_id.in_(all_contact_ids)).all()

    # Sort by primary status then by name
    contacts.sort(key=lambda x: (not x.is_primary, x.full_name))

    # OPTIMIZATION: Combine statistics queries into one with conditional aggregation
    from sqlalchemy import case
    if len(query_client_ids) == 1:
        stats = db.session.query(
            func.count(Invoice.id).label('total_invoices'),
            func.sum(Invoice.amount).label('total_amount'),
            func.sum(case((Invoice.is_paid == False, Invoice.amount), else_=0)).label('unpaid_amount')
        ).filter_by(client_id=id).first()
    else:
        stats = db.session.query(
            func.count(Invoice.id).label('total_invoices'),
            func.sum(Invoice.amount).label('total_amount'),
            func.sum(case((Invoice.is_paid == False, Invoice.amount), else_=0)).label('unpaid_amount')
        ).filter(Invoice.client_id.in_(query_client_ids)).first()

    total_invoices = stats.total_invoices or 0
    total_amount = stats.total_amount or 0
    unpaid_amount = stats.unpaid_amount or 0

    # OPTIMIZATION: Calculate aged balances in SQL (replaces Python loops)
    calculation_method = company.aging_calculation_method if hasattr(company, 'aging_calculation_method') else 'invoice_date'

    # Calculate aged balances for client and all children in ONE SQL query
    all_client_ids = [client.id] + ([child.id for child in client.child_clients if child.company_id == company.id] if client.child_clients else [])
    aged_balances_all = get_aged_balances_sql(all_client_ids, calculation_method)

    if include_children:
        # Aggregate parent + children balances
        aged_balances = {'current': 0, '30_days': 0, '60_days': 0, '90_days': 0, 'over_90_days': 0}
        for cid in all_client_ids:
            client_aged = aged_balances_all.get(cid, {})
            for key in aged_balances:
                aged_balances[key] += client_aged.get(key, 0)
        total_outstanding_filtered = sum(aged_balances.values())
    else:
        # Only parent balances
        aged_balances = aged_balances_all.get(client.id, {
            'current': 0, '30_days': 0, '60_days': 0, '90_days': 0, 'over_90_days': 0
        })
        total_outstanding_filtered = sum(aged_balances.values())

    # For child clients, prepare their balances and totals (already calculated in one query)
    children_balances = {}
    children_total_outstanding = {}
    if client.child_clients:
        for child in client.child_clients:
            child_aged = aged_balances_all.get(child.id, {
                'current': 0, '30_days': 0, '60_days': 0, '90_days': 0, 'over_90_days': 0
            })
            children_balances[child.id] = child_aged
            children_total_outstanding[child.id] = sum(child_aged.values())

    # OPTIMIZATION: Removed all_invoices loading - was loading ALL invoices even when only showing 10 paginated
    # If needed for bulk delete, will be loaded on-demand via AJAX

    # Check if there is an active accounting connection for PDF download
    from models import AccountingConnection
    accounting_conn = AccountingConnection.query.filter_by(
        company_id=company.id,
        is_active=True
    ).first()
    has_accounting_connection = accounting_conn is not None
    accounting_system_type = accounting_conn.system_type if accounting_conn else None

    # Check if ANY invoice in the company has original_amount (for conditional column display)
    has_original_amount = db.session.query(
        db.exists().where(
            db.and_(
                Invoice.company_id == company.id,
                Invoice.original_amount.isnot(None)
            )
        )
    ).scalar()

    logger = logging.getLogger(__name__)
    logger.info(f"🔍 PDF DOWNLOAD DEBUG - Company ID: {company.id}, Connection found: {accounting_conn}, has_accounting_connection: {has_accounting_connection}")

    # Check if project feature is enabled
    from utils.project_helper import is_project_feature_enabled, get_project_label
    is_project_enabled = is_project_feature_enabled(company)
    project_label = get_project_label(company) if is_project_enabled else None

    # Check if client has at least one invoice with project_name
    has_project_invoices = False
    if is_project_enabled:
        has_project_invoices = db.session.query(
            db.exists().where(
                db.and_(
                    Invoice.client_id == id,
                    Invoice.project_name.isnot(None),
                    Invoice.project_name != ''
                )
            )
        ).scalar()

    # Check if group_by_project is requested
    group_by_project = request.args.get('group_by_project', 'false').lower() == 'true'

    # DEBUG: Log project feature status
    logger.info(f"🔍 PROJECT FEATURE DEBUG - Company: {company.name}, project_field_enabled attr: {getattr(company, 'project_field_enabled', 'NO ATTR')}, is_project_enabled: {is_project_enabled}, project_label: {project_label}, has_project_invoices: {has_project_invoices}")

    # Calculate DMP (Days Mean Payment) for this client
    from utils.dmp_calculator import calculate_client_dmp, calculate_client_dmp_both
    client_dmp = calculate_client_dmp(client.id, company.id)
    client_dmp_both = calculate_client_dmp_both(client.id, company.id)
    client_dmp_invoice = client_dmp_both['invoice_date']
    client_dmp_due = client_dmp_both['due_date']

    # Request filter parameters for template
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    # Format total outstanding for display
    from utils import format_currency
    from datetime import datetime, date
    total_outstanding_formatted = format_currency(total_outstanding_filtered, company.currency or 'CAD')
    today_date = datetime.now().strftime('%Y-%m-%d')

    # User full name for email templates
    if current_user.first_name and current_user.last_name:
        user_full_name = f"{current_user.first_name} {current_user.last_name}"
    elif current_user.first_name:
        user_full_name = current_user.first_name
    else:
        user_full_name = current_user.username

    # Initialize forms for the modals
    from forms import InvoiceForm, CommunicationNoteForm
    invoice_form = InvoiceForm(company_id=company.id)
    note_form = CommunicationNoteForm(company_id=company.id)

    return render_template('clients/detail.html',
                         client=client,
                         invoices=invoices_pagination.items,
                         pagination=invoices_pagination,
                         notes=notes,
                         contacts=contacts,
                         total_invoices=total_invoices,
                         total_amount=total_amount,
                         unpaid_amount=unpaid_amount,
                         aged_balances=aged_balances,
                         total_outstanding_filtered=total_outstanding_filtered,
                         children_balances=children_balances,
                         children_total_outstanding=children_total_outstanding,
                         include_children=include_children,
                         view_filter=view_filter,
                         has_accounting_connection=has_accounting_connection,
                         accounting_system_type=accounting_system_type,
                         has_original_amount=has_original_amount,
                         is_project_enabled=is_project_enabled,
                         project_label=project_label,
                         has_project_invoices=has_project_invoices,
                         group_by_project=group_by_project,
                         client_dmp=client_dmp,
                         client_dmp_invoice=client_dmp_invoice,
                         client_dmp_due=client_dmp_due,
                         date_from=date_from,
                         date_to=date_to,
                         total_outstanding_formatted=total_outstanding_formatted,
                         today_date=today_date,
                         user_full_name=user_full_name,
                         invoice_form=invoice_form,
                         note_form=note_form,
                         today=date.today().isoformat())

@client_bp.route('/<int:client_id>/contact/<int:contact_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_contact_form(client_id, contact_id):
    """Edit contact form"""
    from app import db
    from models import Client, ClientContact
    from forms import ClientContactForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    if current_user.is_read_only():
        flash('Accès refusé. Vous n\'avez pas les permissions pour modifier des contacts.', 'error')
        return redirect(url_for('client.detail_client', id=client_id))

    client = db.session.query(Client).filter_by(id=client_id, company_id=company.id).first()
    if not client:
        flash('Client introuvable.', 'error')
        return redirect(url_for('client.list_clients'))

    contact = db.session.query(ClientContact).filter_by(id=contact_id, client_id=client_id).first()
    if not contact:
        flash('Contact introuvable.', 'error')
        return redirect(url_for('client.detail_client', id=client_id))

    form = ClientContactForm(obj=contact)

    if form.validate_on_submit():
        try:
            contact.first_name = form.first_name.data
            contact.last_name = form.last_name.data
            contact.email = form.email.data
            contact.phone = form.phone.data
            contact.position = form.position.data
            contact.language = form.language.data
            contact.is_primary = form.is_primary.data

            db.session.commit()

            log_client_action(
                AuditActions.CLIENT_UPDATED,
                client,
                new_data={'contact_updated': contact.email}
            )
            flash('Contact modifié avec succès.', 'success')
            return redirect(url_for('client.detail_client', id=client_id))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erreur modification contact: {str(e)}")
            flash('Erreur lors de la modification du contact.', 'error')

    return render_template('clients/edit_contact.html', form=form, client=client, contact=contact)

@client_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
def delete_client(id):
    """Delete a client"""
    from app import db
    from models import Client, CommunicationNote, Invoice, ClientContact

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'success': False, 'message': 'Aucune entreprise sélectionnée'}), 400

    if current_user.is_read_only():
        return jsonify({'success': False, 'message': 'Accès refusé. Vous n\'avez pas les permissions pour supprimer des clients'}), 403

    client = Client.query.filter_by(id=id, company_id=company.id).first()
    if not client:
        return jsonify({'success': False, 'message': 'Client introuvable'}), 404

    try:
        client_name = client.name

        # Delete related data
        CommunicationNote.query.filter_by(client_id=id).delete()
        ClientContact.query.filter_by(client_id=id).delete()
        Invoice.query.filter_by(client_id=id).delete()

        # Delete client
        db.session.delete(client)
        db.session.commit()

        log_action(
            AuditActions.CLIENT_DELETED,
            entity_type=EntityTypes.CLIENT,
            entity_id=id,
            entity_name=client_name
        )

        from utils.secure_logging import sanitize_email_for_logs
        current_app.logger.info(f"Client supprimé par {sanitize_email_for_logs(current_user.email)}")
        return jsonify({'success': True, 'message': f'Client "{client_name}" supprimé avec succès'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur lors de la suppression du client: {str(e)}")
        return jsonify({'success': False, 'message': 'Erreur lors de la suppression du client.'}), 500

@client_bp.route('/delete-batch', methods=['POST'])
@login_required
def delete_batch_clients():
    """Delete multiple clients at once"""
    from app import db
    from models import Client, CommunicationNote, Invoice, ClientContact

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'success': False, 'message': 'Aucune entreprise sélectionnée'}), 400

    if current_user.is_read_only():
        return jsonify({'success': False, 'message': 'Accès refusé. Vous n\'avez pas les permissions pour supprimer des clients'}), 403

    # Récupérer les IDs des clients à supprimer
    client_ids = request.form.getlist('client_ids')
    if not client_ids:
        return jsonify({'success': False, 'message': 'Aucun client sélectionné'}), 400

    try:
        # Convertir en entiers et vérifier que les clients appartiennent à l'entreprise
        client_ids = [int(cid) for cid in client_ids]
        clients = Client.query.filter(
            Client.id.in_(client_ids),
            Client.company_id == company.id
        ).all()

        if len(clients) != len(client_ids):
            return jsonify({'success': False, 'message': 'Certains clients n\'ont pas été trouvés'}), 404

        deleted_count = 0
        client_names = []

        for client in clients:
            client_names.append(client.name)

            # Supprimer les données associées
            CommunicationNote.query.filter_by(client_id=client.id).delete()
            ClientContact.query.filter_by(client_id=client.id).delete()
            Invoice.query.filter_by(client_id=client.id).delete()

            # Supprimer le client
            db.session.delete(client)
            deleted_count += 1

        db.session.commit()

        for i, client_id in enumerate(client_ids):
            log_action(
                AuditActions.CLIENT_DELETED,
                entity_type=EntityTypes.CLIENT,
                entity_id=client_id,
                entity_name=client_names[i] if i < len(client_names) else None
            )

        from utils.secure_logging import sanitize_email_for_logs
        current_app.logger.info(f"{deleted_count} clients supprimés par {sanitize_email_for_logs(current_user.email)}")
        return jsonify({'success': True, 'message': f'{deleted_count} client(s) supprimé(s) avec succès'})

    except ValueError:
        return jsonify({'success': False, 'message': 'IDs de clients invalides'}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur lors de la suppression batch: {str(e)}")
        return jsonify({'success': False, 'message': 'Erreur lors de la suppression des clients.'}), 500


@client_bp.route('/export/excel')
@login_required
@limiter.limit("10 per minute")
def export_clients_excel():
    """Export clients to Excel with current filters"""
    from app import db
    from models import Client, Invoice
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload
    from io import BytesIO
    import xlsxwriter

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('client.list_clients'))

    # Get filter parameters
    search = request.args.get('search', '').strip()
    balance_filter = request.args.get('balance_filter', '')
    collector_filter = request.args.get('collector_filter', '')

    # Create subquery to calculate outstanding balance in SQL
    outstanding_subquery = db.session.query(
        Invoice.client_id,
        func.sum(Invoice.amount).label('total_outstanding')
    ).filter(
        Invoice.company_id == company.id,
        Invoice.is_paid == False
    ).group_by(Invoice.client_id).subquery()

    # Build query with same filters as list view
    query = db.session.query(
        Client,
        func.coalesce(outstanding_subquery.c.total_outstanding, 0).label('outstanding_balance')
    ).outerjoin(
        outstanding_subquery, Client.id == outstanding_subquery.c.client_id
    ).filter(
        Client.company_id == company.id
    ).options(
        joinedload(Client.collector)
    )

    # Apply search filter
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (Client.name.ilike(search_filter)) |
            (Client.code_client.ilike(search_filter))
        )

    # Apply balance filter
    if balance_filter == 'with_balance':
        query = query.filter(outstanding_subquery.c.total_outstanding > 0)
    elif balance_filter == 'without_balance':
        query = query.filter(
            (outstanding_subquery.c.total_outstanding == None) |
            (outstanding_subquery.c.total_outstanding == 0)
        )

    # Apply collector filter (with company membership validation)
    if collector_filter:
        if collector_filter == 'unassigned':
            query = query.filter(Client.collector_id.is_(None))
        else:
            try:
                collector_id = int(collector_filter)
                # Security: Validate collector belongs to this company
                from models import UserCompany
                from flask import abort
                is_valid_collector = UserCompany.query.filter_by(
                    user_id=collector_id,
                    company_id=company.id,
                    is_active=True
                ).first() is not None

                if not is_valid_collector:
                    # Security: Reject invalid collector ID with 400 error (no redirect)
                    abort(400, description='Collecteur invalide')

                query = query.filter(Client.collector_id == collector_id)
            except (ValueError, TypeError):
                # Security: Reject invalid format with 400 error (no redirect)
                abort(400, description='Format de collecteur invalide')

    # Order by code client
    query = query.order_by(Client.code_client.asc())

    # Get all results (no pagination for export)
    results = query.all()

    # Create Excel file in memory
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet('Clients')

    # Define formats
    header_format = workbook.add_format({
        'bold': True,
        'bg_color': '#4472C4',
        'font_color': 'white',
        'border': 1,
        'text_wrap': True,
        'valign': 'vcenter'
    })

    cell_format = workbook.add_format({
        'border': 1,
        'valign': 'vcenter'
    })

    # Get company currency for Excel formatting
    company_currency = company.currency if hasattr(company, 'currency') and company.currency else 'CAD'
    EXCEL_CURRENCY_FORMATS = {
        'CAD': '# ##0,00 $',
        'USD': '$#,##0.00',
        'EUR': '# ##0,00 €',
        'GBP': '£#,##0.00',
        'CHF': '# ##0,00 CHF'
    }
    currency_num_format = EXCEL_CURRENCY_FORMATS.get(company_currency, '# ##0,00 $')

    money_format = workbook.add_format({
        'border': 1,
        'valign': 'vcenter',
        'num_format': currency_num_format
    })

    # Set column widths
    worksheet.set_column('A:A', 15)  # Code client
    worksheet.set_column('B:B', 35)  # Nom
    worksheet.set_column('C:C', 30)  # Email
    worksheet.set_column('D:D', 18)  # Téléphone
    worksheet.set_column('E:E', 20)  # Collecteur
    worksheet.set_column('F:F', 20)  # Représentant
    worksheet.set_column('G:G', 20)  # Conditions paiement
    worksheet.set_column('H:H', 18)  # Solde

    # Write headers
    headers = ['Code client', 'Nom', 'Email', 'Téléphone', 'Collecteur', 'Représentant', 'Conditions paiement', 'Solde à recevoir']
    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)

    # Write data
    for row_idx, (client, outstanding_balance) in enumerate(results, start=1):
        worksheet.write(row_idx, 0, client.code_client or '', cell_format)
        worksheet.write(row_idx, 1, client.name or '', cell_format)
        worksheet.write(row_idx, 2, client.email or '', cell_format)
        worksheet.write(row_idx, 3, client.phone or '', cell_format)
        worksheet.write(row_idx, 4, client.collector.full_name if client.collector else '', cell_format)
        worksheet.write(row_idx, 5, client.representative_name or '', cell_format)
        worksheet.write(row_idx, 6, client.payment_terms or '', cell_format)
        worksheet.write(row_idx, 7, float(outstanding_balance) if outstanding_balance else 0, money_format)

    # Add filter info at the bottom
    info_row = len(results) + 3
    info_format = workbook.add_format({'italic': True, 'font_color': '#666666'})

    filter_parts = []
    if search:
        filter_parts.append(f"Recherche: {search}")
    if balance_filter == 'with_balance':
        filter_parts.append("Filtre: Avec solde")
    elif balance_filter == 'without_balance':
        filter_parts.append("Filtre: Sans solde")
    if collector_filter == 'unassigned':
        filter_parts.append("Collecteur: Non assigné")
    elif collector_filter:
        filter_parts.append(f"Collecteur ID: {collector_filter}")

    if filter_parts:
        worksheet.write(info_row, 0, f"Filtres appliqués: {', '.join(filter_parts)}", info_format)

    worksheet.write(info_row + 1, 0, f"Total: {len(results)} client(s)", info_format)

    workbook.close()
    output.seek(0)

    # Generate filename with date
    from datetime import datetime
    filename = f"clients_{company.name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    response = make_response(output.read())
    response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

    return response