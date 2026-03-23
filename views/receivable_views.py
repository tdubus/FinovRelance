"""
Views for receivables management
Extracted from views.py monolith - Phase 6 Refactoring
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify, current_app
from flask_login import login_required, current_user
from datetime import datetime, timedelta
import io
import math
from app import limiter

# Create receivables blueprint
receivable_bp = Blueprint('receivable', __name__, url_prefix='/receivables')

# Pagination helper class (used by multiple routes)
class MockPagination:
    """Pagination object for templates"""
    def __init__(self, page, per_page, total, items):
        self.page = page
        self.per_page = per_page
        self.total = total
        self.items = items
        self.pages = math.ceil(total / per_page) if total > 0 else 1
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1 if self.has_prev else None
        self.next_num = page + 1 if self.has_next else None

    def iter_pages(self, left_edge=2, left_current=2, right_current=3, right_edge=2):
        """Generate page numbers for pagination"""
        last = self.pages
        for num in range(1, last + 1):
            if num <= left_edge or \
               (self.page - left_current - 1 < num < self.page + right_current) or \
               num > last - right_edge:
                yield num

@receivable_bp.route('/')
@login_required
def overview():
    """Display accounts receivable dashboard - OPTIMIZED with SQL aging calculations"""
    from app import db
    from models import Client, Invoice, Company, User
    from utils import get_local_today
    from sqlalchemy import func, case, and_, or_, cast, Date

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe', 'lecteur']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get filter parameters
    client_filter = request.args.get('client_filter')
    aging_filter = request.args.get('aging_filter')
    amount_filter = request.args.get('amount_filter')
    search = request.args.get('search', '')
    collector_filter = request.args.get('collector_id', '')

    # Get sorting parameters
    sort_by = request.args.get('sort_by', 'overdue_amount')  # Default sort by overdue amount
    sort_order = request.args.get('sort_order', 'desc')  # Default descending

    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = 75

    today = get_local_today()

    # OPTIMIZATION: Calculate aging buckets in SQL using CASE statements
    # Determine which date field to use based on company settings
    calc_date_field = Invoice.invoice_date if company.aging_calculation_method == 'invoice_date' else Invoice.due_date

    # Calculate days old in SQL (PostgreSQL-compatible)
    # NOTE: PostgreSQL-specific - DATE - DATE returns integer days
    # If supporting SQLite in future, add dialect check for func.julianday()
    days_old = cast(today, Date) - calc_date_field
    is_overdue = Invoice.due_date < today

    # Build CASE statements for each aging bucket
    current_bucket = case(
        (is_overdue == False, Invoice.amount),
        else_=0
    )

    days_30_bucket = case(
        (and_(is_overdue == True, days_old <= 30), Invoice.amount),
        else_=0
    )

    days_60_bucket = case(
        (and_(is_overdue == True, days_old > 30, days_old <= 60), Invoice.amount),
        else_=0
    )

    days_90_bucket = case(
        (and_(is_overdue == True, days_old > 60, days_old <= 90), Invoice.amount),
        else_=0
    )

    over_90_bucket = case(
        (and_(is_overdue == True, days_old > 90), Invoice.amount),
        else_=0
    )

    # Check if client has any invoices with project_name (for project expansion feature)
    has_projects = func.bool_or(Invoice.project_name.isnot(None)).label('has_projects')

    # OPTIMIZATION: Build aggregated query that calculates aging buckets per client in SQL
    clients_query = db.session.query(
        Client,
        func.sum(current_bucket).label('current'),
        func.sum(days_30_bucket).label('days_30'),
        func.sum(days_60_bucket).label('days_60'),
        func.sum(days_90_bucket).label('days_90'),
        func.sum(over_90_bucket).label('over_90'),
        func.sum(Invoice.amount).label('total_outstanding'),
        has_projects
    ).join(
        Invoice, Client.id == Invoice.client_id
    ).outerjoin(
        User, Client.collector_id == User.id  # Eager load collector
    ).filter(
        Client.company_id == company.id,
        Invoice.is_paid == False
    ).group_by(Client.id)

    # Apply filters
    if client_filter:
        clients_query = clients_query.filter(Client.name.ilike(f'%{client_filter}%'))

    if search:
        clients_query = clients_query.filter(or_(
            Client.name.ilike(f'%{search}%'),
            Client.code_client.ilike(f'%{search}%')
        ))

    # Validate and store collector filter for reuse in count_query
    validated_collector_id = None
    collector_is_unassigned = False
    if collector_filter:
        if collector_filter == 'unassigned':
            collector_is_unassigned = True
            clients_query = clients_query.filter(Client.collector_id.is_(None))
        else:
            try:
                collector_id = int(collector_filter)
                from models import UserCompany
                is_valid_collector = UserCompany.query.filter_by(
                    user_id=collector_id,
                    company_id=company.id,
                    is_active=True
                ).first() is not None
                if is_valid_collector:
                    validated_collector_id = collector_id
                    clients_query = clients_query.filter(Client.collector_id == collector_id)
            except (ValueError, TypeError):
                pass

    # Apply aging filter
    if aging_filter:
        # Use HAVING clause to filter on aggregated aging buckets
        if aging_filter == '0-30':
            clients_query = clients_query.having(func.sum(days_30_bucket) > 0)
        elif aging_filter == '31-60':
            clients_query = clients_query.having(func.sum(days_60_bucket) > 0)
        elif aging_filter == '61-90':
            clients_query = clients_query.having(func.sum(days_90_bucket) > 0)
        elif aging_filter == '90+':
            clients_query = clients_query.having(func.sum(over_90_bucket) > 0)

    if amount_filter:
        # Guard against NaN and infinity injection before typecasting
        amount_str = amount_filter.strip().lower()
        if amount_str not in ('nan', 'inf', '-inf', 'infinity', '-infinity'):
            try:
                min_amount = float(amount_filter)
                # Additional guard against special float values
                if math.isfinite(min_amount):
                    clients_query = clients_query.having(func.sum(Invoice.amount) >= min_amount)
            except ValueError:
                pass

    # OPTIMIZATION: Apply sorting in SQL when possible
    sort_column_map = {
        'name': Client.name,
        'total': func.sum(Invoice.amount),
        'current': func.sum(current_bucket),
        '30_days': func.sum(days_30_bucket),
        '60_days': func.sum(days_60_bucket),
        '90_days': func.sum(days_90_bucket),
        'over_90_days': func.sum(over_90_bucket),
        'overdue_amount': func.sum(days_30_bucket) + func.sum(days_60_bucket) + func.sum(days_90_bucket) + func.sum(over_90_bucket)
    }

    # CRITICAL OPTIMIZATION: Use SQL pagination for all sorts except 'collector'
    # Collector sorting requires loading all data because of complex join
    use_sql_pagination = sort_by != 'collector'

    if use_sql_pagination:
        # Apply SQL ordering for supported columns
        if sort_by in sort_column_map:
            if sort_order == 'desc':
                clients_query = clients_query.order_by(sort_column_map[sort_by].desc())
            else:
                clients_query = clients_query.order_by(sort_column_map[sort_by].asc())
        else:
            # Default: sort by overdue amount descending
            clients_query = clients_query.order_by(sort_column_map['overdue_amount'].desc())

        # CRITICAL OPTIMIZATION: Fast count using simple DISTINCT query
        # Avoid expensive .count() on complex GROUP BY query
        count_query = db.session.query(func.count(func.distinct(Client.id))).join(
            Invoice, Client.id == Invoice.client_id
        ).filter(
            Client.company_id == company.id,
            Invoice.is_paid == False
        )

        # Apply same filters to count query
        if client_filter:
            count_query = count_query.filter(Client.name.ilike(f'%{client_filter}%'))
        if search:
            count_query = count_query.filter(or_(
                Client.name.ilike(f'%{search}%'),
                Client.code_client.ilike(f'%{search}%')
            ))
        if collector_is_unassigned:
            count_query = count_query.filter(Client.collector_id.is_(None))
        elif validated_collector_id:
            count_query = count_query.filter(Client.collector_id == validated_collector_id)

        total_clients = count_query.scalar()

        # Apply SQL LIMIT/OFFSET for pagination (MUCH faster!)
        offset = (page - 1) * per_page
        clients_results = clients_query.limit(per_page).offset(offset).all()

        # Format results into clients_data structure
        clients_data = []
        for result in clients_results:
            client_obj = result[0]
            clients_data.append({
                'client': client_obj,
                'aged_balances': {
                    'current': float(result[1] or 0),
                    '30_days': float(result[2] or 0),
                    '60_days': float(result[3] or 0),
                    '90_days': float(result[4] or 0),
                    'over_90_days': float(result[5] or 0)
                },
                'total_outstanding': float(result[6] or 0),
                'has_projects': bool(result[7] or False)
            })

        # Create pagination object for template
        clients_pagination = MockPagination(page, per_page, total_clients, clients_data)

    else:
        # Collector sorting: must load all data (no way around it)
        clients_results = clients_query.all()

        # Format results
        clients_data = []
        for result in clients_results:
            client_obj = result[0]
            clients_data.append({
                'client': client_obj,
                'aged_balances': {
                    'current': float(result[1] or 0),
                    '30_days': float(result[2] or 0),
                    '60_days': float(result[3] or 0),
                    '90_days': float(result[4] or 0),
                    'over_90_days': float(result[5] or 0)
                },
                'total_outstanding': float(result[6] or 0),
                'has_projects': bool(result[7] or False)
            })

        # Sort by collector in Python
        def get_collector_name(data_item):
            return data_item['client'].collector.full_name.lower() if data_item['client'].collector else 'zzz'
        clients_data.sort(key=get_collector_name, reverse=(sort_order == 'desc'))

        # Apply Python pagination
        total_clients = len(clients_data)
        offset = (page - 1) * per_page
        paginated_clients_data = clients_data[offset:offset + per_page]

        # Create pagination object
        clients_pagination = MockPagination(page, per_page, total_clients, paginated_clients_data)

    # OPTIMIZATION: Calculate global aging summary in SQL
    aging_summary_query = db.session.query(
        func.sum(current_bucket).label('current'),
        func.sum(days_30_bucket).label('days_30'),
        func.sum(days_60_bucket).label('days_60'),
        func.sum(days_90_bucket).label('days_90'),
        func.sum(over_90_bucket).label('over_90'),
        func.sum(Invoice.amount).label('total')
    ).join(
        Client, Invoice.client_id == Client.id
    ).filter(
        Client.company_id == company.id,
        Invoice.is_paid == False
    ).first()

    # Handle case where there are no invoices
    if aging_summary_query:
        aging_summary = {
            'current': float(aging_summary_query[0] or 0),
            '1_30': float(aging_summary_query[1] or 0),
            '31_60': float(aging_summary_query[2] or 0),
            '61_90': float(aging_summary_query[3] or 0),
            '90_plus': float(aging_summary_query[4] or 0),
            'total': float(aging_summary_query[5] or 0)
        }
    else:
        aging_summary = {
            'current': 0.0,
            '1_30': 0.0,
            '31_60': 0.0,
            '61_90': 0.0,
            '90_plus': 0.0,
            'total': 0.0
        }

    # Get unique clients for filter dropdown (limit for performance)
    clients = db.session.query(Client).filter_by(company_id=company.id).order_by(Client.name).limit(100).all()

    # Get collectors for filter dropdown
    collectors = []
    try:
        collectors = db.session.query(User).join(Client, User.id == Client.collector_id).filter(
            Client.company_id == company.id
        ).distinct().limit(100).all()
    except Exception:
        # Fallback if query fails
        collectors = []

    # Calculate totals for template based on aging_summary
    total_outstanding = aging_summary.get('total', 0)
    total_current = aging_summary.get('current', 0)
    total_0_30 = aging_summary.get('1_30', 0)
    total_31_60 = aging_summary.get('31_60', 0)
    total_61_90 = aging_summary.get('61_90', 0)
    total_90_plus = aging_summary.get('90_plus', 0)

    # Calculate percentages
    percentage_current = round((total_current / total_outstanding * 100) if total_outstanding > 0 else 0, 2)
    percentage_0_30 = round((total_0_30 / total_outstanding * 100) if total_outstanding > 0 else 0, 2)
    percentage_31_60 = round((total_31_60 / total_outstanding * 100) if total_outstanding > 0 else 0, 2)
    percentage_61_90 = round((total_61_90 / total_outstanding * 100) if total_outstanding > 0 else 0, 2)
    percentage_90_plus = round((total_90_plus / total_outstanding * 100) if total_outstanding > 0 else 0, 2)

    # Check if project feature is enabled
    from utils.project_helper import is_project_feature_enabled, get_project_label
    project_feature_enabled = is_project_feature_enabled(company)
    project_label = get_project_label(company) if project_feature_enabled else None

    return render_template('receivables/overview.html',
                         aging_summary=aging_summary,
                         clients=clients,
                         clients_data=clients_data,
                         clients_pagination=clients_pagination,
                         collectors=collectors,
                         client_filter=client_filter,
                         aging_filter=aging_filter,
                         amount_filter=amount_filter,
                         search=search,
                         collector_filter=collector_filter,
                         sort_by=sort_by,
                         sort_order=sort_order,
                         company=company,
                         total_outstanding=total_outstanding,
                         total_current=total_current,
                         total_0_30=total_0_30,
                         total_31_60=total_31_60,
                         total_61_90=total_61_90,
                         total_90_plus=total_90_plus,
                         percentage_current=percentage_current,
                         percentage_0_30=percentage_0_30,
                         percentage_31_60=percentage_31_60,
                         percentage_61_90=percentage_61_90,
                         percentage_90_plus=percentage_90_plus,
                         project_feature_enabled=project_feature_enabled,
                         project_label=project_label)


@receivable_bp.route('/api/load')
@limiter.exempt
@login_required
def api_load_receivables():
    """API endpoint for lazy loading receivables data with pagination"""
    from app import db
    from models import Client, Invoice, Company, User
    from utils import get_local_today
    from sqlalchemy import func, case, and_, or_, cast, Date

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 403

    # Get filter parameters
    search = request.args.get('search', '').strip()
    collector_filter = request.args.get('collector_id', '').strip()
    sort_by = request.args.get('sort_by', 'overdue_amount')
    sort_order = request.args.get('sort_order', 'desc')
    page = request.args.get('page', 1, type=int)
    per_page = 75

    today = get_local_today()

    # Calculate aging buckets in SQL (same as overview())
    calc_date_field = Invoice.invoice_date if company.aging_calculation_method == 'invoice_date' else Invoice.due_date
    days_old = cast(today, Date) - calc_date_field
    is_overdue = Invoice.due_date < today

    current_bucket = case((is_overdue == False, Invoice.amount), else_=0)
    days_30_bucket = case((and_(is_overdue == True, days_old <= 30), Invoice.amount), else_=0)
    days_60_bucket = case((and_(is_overdue == True, days_old > 30, days_old <= 60), Invoice.amount), else_=0)
    days_90_bucket = case((and_(is_overdue == True, days_old > 60, days_old <= 90), Invoice.amount), else_=0)
    over_90_bucket = case((and_(is_overdue == True, days_old > 90), Invoice.amount), else_=0)

    # Check if client has any invoices with project_name
    has_projects = func.bool_or(Invoice.project_name.isnot(None)).label('has_projects')

    # Build aggregated query
    clients_query = db.session.query(
        Client,
        func.sum(current_bucket).label('current'),
        func.sum(days_30_bucket).label('days_30'),
        func.sum(days_60_bucket).label('days_60'),
        func.sum(days_90_bucket).label('days_90'),
        func.sum(over_90_bucket).label('over_90'),
        func.sum(Invoice.amount).label('total_outstanding'),
        has_projects
    ).join(
        Invoice, Client.id == Invoice.client_id
    ).outerjoin(
        User, Client.collector_id == User.id
    ).filter(
        Client.company_id == company.id,
        Invoice.is_paid == False
    ).group_by(Client.id)

    # Apply filters
    if search:
        clients_query = clients_query.filter(or_(
            Client.name.ilike(f'%{search}%'),
            Client.code_client.ilike(f'%{search}%')
        ))

    # Validate and store collector filter for reuse in count_query
    validated_collector_id = None
    collector_is_unassigned = False
    if collector_filter:
        if collector_filter == 'unassigned':
            collector_is_unassigned = True
            clients_query = clients_query.filter(Client.collector_id.is_(None))
        else:
            try:
                collector_id = int(collector_filter)
                from models import UserCompany
                is_valid_collector = UserCompany.query.filter_by(
                    user_id=collector_id,
                    company_id=company.id,
                    is_active=True
                ).first() is not None
                if is_valid_collector:
                    validated_collector_id = collector_id
                    clients_query = clients_query.filter(Client.collector_id == collector_id)
            except (ValueError, TypeError):
                pass

    # Apply sorting
    sort_column_map = {
        'name': Client.name,
        'total': func.sum(Invoice.amount),
        'current': func.sum(current_bucket),
        '30_days': func.sum(days_30_bucket),
        '60_days': func.sum(days_60_bucket),
        '90_days': func.sum(days_90_bucket),
        'over_90_days': func.sum(over_90_bucket),
        'overdue_amount': func.sum(days_30_bucket) + func.sum(days_60_bucket) + func.sum(days_90_bucket) + func.sum(over_90_bucket)
    }

    use_sql_pagination = sort_by != 'collector'

    if use_sql_pagination:
        if sort_by in sort_column_map:
            if sort_order == 'desc':
                clients_query = clients_query.order_by(sort_column_map[sort_by].desc())
            else:
                clients_query = clients_query.order_by(sort_column_map[sort_by].asc())
        else:
            clients_query = clients_query.order_by(sort_column_map['overdue_amount'].desc())

        # Fast count
        count_query = db.session.query(func.count(func.distinct(Client.id))).join(
            Invoice, Client.id == Invoice.client_id
        ).filter(
            Client.company_id == company.id,
            Invoice.is_paid == False
        )

        if search:
            count_query = count_query.filter(or_(
                Client.name.ilike(f'%{search}%'),
                Client.code_client.ilike(f'%{search}%')
            ))
        if collector_is_unassigned:
            count_query = count_query.filter(Client.collector_id.is_(None))
        elif validated_collector_id:
            count_query = count_query.filter(Client.collector_id == validated_collector_id)

        total_clients = count_query.scalar()

        # Apply SQL pagination
        offset = (page - 1) * per_page
        clients_results = clients_query.limit(per_page).offset(offset).all()
    else:
        # Collector sort: load all and sort in Python
        all_results = clients_query.all()

        # Sort by collector name in Python
        if sort_by == 'collector':
            all_results.sort(
                key=lambda x: (x[0].collector.full_name if x[0].collector else ''),
                reverse=(sort_order == 'desc')
            )

        total_clients = len(all_results)

        # Manual pagination
        start = (page - 1) * per_page
        end = start + per_page
        clients_results = all_results[start:end]

    # Format results as JSON
    receivables_data = []
    for result in clients_results:
        client_obj = result[0]
        receivables_data.append({
            'id': client_obj.id,
            'name': client_obj.name,
            'code_client': client_obj.code_client,
            'collector': {
                'id': client_obj.collector.id if client_obj.collector else None,
                'full_name': client_obj.collector.full_name if client_obj.collector else None
            } if client_obj.collector else None,
            'aged_balances': {
                'current': float(result[1] or 0),
                'days_30': float(result[2] or 0),
                'days_60': float(result[3] or 0),
                'days_90': float(result[4] or 0),
                'over_90': float(result[5] or 0)
            },
            'total_outstanding': float(result[6] or 0),
            'has_projects': bool(result[7] or False)
        })

    # Calculate pagination info
    total_pages = math.ceil(total_clients / per_page) if total_clients > 0 else 1

    return jsonify({
        'receivables': receivables_data,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total_clients,
            'pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages
        }
    })


@receivable_bp.route('/export/excel')
@login_required
@limiter.limit("10 per minute")
def export_excel():
    """Export ALL accounts receivable to Excel"""
    from app import db
    from models import Client, Invoice
    from utils import get_local_today
    import xlsxwriter

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe', 'lecteur']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('receivable.overview'))

    try:
        today = get_local_today()

        # Get ALL clients with outstanding invoices (no pagination)
        clients_with_outstanding = db.session.query(Client).join(Invoice).filter(
            Client.company_id == company.id,
            Invoice.is_paid == False
        ).distinct().all()

        # Calculate aging for each client
        clients_data = []
        for client in clients_with_outstanding:
            unpaid_invoices = [inv for inv in client.invoices if not inv.is_paid]

            aged_balances = {
                'current': 0,
                '30_days': 0,
                '60_days': 0,
                '90_days': 0,
                'over_90_days': 0
            }

            total_outstanding = 0

            for invoice in unpaid_invoices:
                amount = float(invoice.amount) if invoice.amount else 0
                total_outstanding += amount

                # Use company's aging calculation method
                date_field = invoice.invoice_date if company.aging_calculation_method == 'invoice_date' else invoice.due_date

                if date_field:
                    days_overdue = (today - date_field).days

                    if days_overdue < 0:
                        aged_balances['current'] += amount
                    elif days_overdue <= 30:
                        aged_balances['30_days'] += amount
                    elif days_overdue <= 60:
                        aged_balances['60_days'] += amount
                    elif days_overdue <= 90:
                        aged_balances['90_days'] += amount
                    else:
                        aged_balances['over_90_days'] += amount

            clients_data.append({
                'client': client,
                'aged_balances': aged_balances,
                'total_outstanding': total_outstanding
            })

        # Sort by client name
        clients_data.sort(key=lambda x: x['client'].name.lower())

        # Create Excel file
        excel_buffer = io.BytesIO()
        workbook = xlsxwriter.Workbook(excel_buffer)
        worksheet = workbook.add_worksheet('Comptes à recevoir')

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

        # Define formats
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#4472C4',
            'font_color': 'white',
            'align': 'center'
        })

        currency_format = workbook.add_format({
            'num_format': currency_num_format,
            'align': 'right'
        })

        total_format = workbook.add_format({
            'bold': True,
            'num_format': currency_num_format,
            'align': 'right',
            'top': 1
        })

        # Headers
        headers = ['Client', 'Code', 'Collecteur', 'Courant', '0-30 jours', '31-60 jours', '61-90 jours', '90+ jours', 'Total']
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)

        # Data rows
        row = 1
        total_current = 0
        total_0_30 = 0
        total_31_60 = 0
        total_61_90 = 0
        total_90_plus = 0
        total_outstanding = 0

        for data in clients_data:
            client = data['client']
            aged = data['aged_balances']

            worksheet.write(row, 0, client.name)
            worksheet.write(row, 1, client.code_client or '')
            worksheet.write(row, 2, client.collector.full_name if client.collector else 'Non assigné')
            worksheet.write(row, 3, aged['current'], currency_format)
            worksheet.write(row, 4, aged['30_days'], currency_format)
            worksheet.write(row, 5, aged['60_days'], currency_format)
            worksheet.write(row, 6, aged['90_days'], currency_format)
            worksheet.write(row, 7, aged['over_90_days'], currency_format)
            worksheet.write(row, 8, data['total_outstanding'], currency_format)

            # Accumulate totals
            total_current += aged['current']
            total_0_30 += aged['30_days']
            total_31_60 += aged['60_days']
            total_61_90 += aged['90_days']
            total_90_plus += aged['over_90_days']
            total_outstanding += data['total_outstanding']

            row += 1

        # Total row
        worksheet.write(row, 0, 'TOTAL', total_format)
        worksheet.write(row, 1, '', total_format)
        worksheet.write(row, 2, '', total_format)
        worksheet.write(row, 3, total_current, total_format)
        worksheet.write(row, 4, total_0_30, total_format)
        worksheet.write(row, 5, total_31_60, total_format)
        worksheet.write(row, 6, total_61_90, total_format)
        worksheet.write(row, 7, total_90_plus, total_format)
        worksheet.write(row, 8, total_outstanding, total_format)

        # Adjust column widths
        worksheet.set_column('A:A', 30)  # Client
        worksheet.set_column('B:B', 15)  # Code
        worksheet.set_column('C:C', 20)  # Collecteur
        worksheet.set_column('D:I', 15)  # Montants

        workbook.close()
        excel_content = excel_buffer.getvalue()

        # Create response
        from flask import make_response
        response = make_response(excel_content)
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers['Content-Disposition'] = f'attachment; filename=comptes_a_recevoir_{today.strftime("%Y%m%d")}.xlsx'

        current_app.logger.info(f"Excel export generated: {len(clients_data)} clients")
        return response

    except Exception as e:
        current_app.logger.error(f"Error generating Excel export: {str(e)}")
        flash('Erreur lors de la génération du fichier Excel.', 'error')
        return redirect(url_for('receivable.overview'))


@receivable_bp.route('/client/<int:client_id>/statement')
@login_required
def client_statement(client_id):
    """Generate and download client statement"""
    from app import db
    from models import Client, Invoice
    from utils import generate_statement_pdf_reportlab

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get client
    client = db.session.query(Client).filter_by(id=client_id, company_id=company.id).first()
    if not client:
        flash('Client non trouvé.', 'error')
        return redirect(url_for('receivable.overview'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('receivable.overview'))

    # Get date range
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    # Get view filter to handle parent-children relationships
    view_filter = request.args.get('view_filter', 'parent_only')
    include_children = view_filter == 'parent_children' and client.is_parent

    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        else:
            start_date = datetime.now().date() - timedelta(days=90)

        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        else:
            end_date = datetime.now().date()
    except ValueError:
        flash('Format de date invalide.', 'error')
        return redirect(url_for('receivable.overview'))

    # Get ALL unpaid invoices - CRITICAL FIX: Include all outstanding invoices regardless of date
    # Statement should show ALL unpaid invoices, not just those in a date range
    if include_children and client.is_parent:
        # Get invoices from parent and all children
        # Get child clients using the relationship
        child_clients = db.session.query(Client).filter_by(parent_client_id=client.id).all()
        client_ids = [client.id] + [child.id for child in child_clients]
        invoices = db.session.query(Invoice).filter(
            Invoice.client_id.in_(client_ids),
            Invoice.is_paid == False  # Only unpaid invoices
        ).order_by(Invoice.invoice_date.asc(), Invoice.invoice_number.asc()).all()

        # Add client_name and client_code attributes for parent+children PDF detection
        for inv in invoices:
            inv.client_name = inv.client.name if inv.client else client.name
            inv.client_code = inv.client.code_client if inv.client else client.code_client
    else:
        # Get invoices only for this specific client
        invoices = db.session.query(Invoice).filter(
            Invoice.client_id == client_id,
            Invoice.is_paid == False  # Only unpaid invoices
        ).order_by(Invoice.invoice_date.asc(), Invoice.invoice_number.asc()).all()

    # Generate PDF - CRITICAL FIX: Handle parent-children aged balances
    try:
        # Calculer les balances pour le PDF selon le filtre
        if include_children and client.is_parent:
            # Calculate consolidated aged balances for parent + children
            aged_balances = {
                'current': 0,
                '30_days': 0,
                '60_days': 0,
                '90_days': 0,
                'over_90_days': 0
            }
            # Add balances from parent and all children
            # Get child clients using the relationship
            child_clients = db.session.query(Client).filter_by(parent_client_id=client.id).all()
            clients_to_analyze = [client] + child_clients
            for c in clients_to_analyze:
                client_balances = c.get_aged_balances(company.aging_calculation_method or 'invoice_date')
                for key in aged_balances:
                    aged_balances[key] += client_balances.get(key, 0)
        else:
            # Use only parent client's balances
            aged_balances = client.get_aged_balances(company.aging_calculation_method or 'invoice_date')

        pdf_data = generate_statement_pdf_reportlab(client, invoices, company, aged_balances, client.language or 'fr')

        # CRITICAL FIX: Créer réponse avec en-têtes uniques pour éviter ERR_RESPONSE_HEADERS_MULTIPLE_CONTENT_DISPOSITION
        from flask import Response
        filename = f'releve_{client.name.replace(" ", "_")}_{start_date}_{end_date}.pdf'

        # Handle different types of PDF data
        if pdf_data is not None and hasattr(pdf_data, 'getvalue'):
            pdf_content = pdf_data.getvalue()
        elif isinstance(pdf_data, bytes):
            pdf_content = pdf_data
        else:
            pdf_content = str(pdf_data or '').encode('utf-8')

        return Response(
            pdf_content,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )

    except Exception as e:
        current_app.logger.error(f"Error generating statement PDF: {str(e)}")
        flash('Erreur lors de la génération du relevé.', 'error')
        return redirect(url_for('receivable.overview'))


@receivable_bp.route('/api/client/<int:client_id>/projects_breakdown')
@limiter.exempt
@login_required
def client_projects_breakdown(client_id):
    """AJAX endpoint: Get aging breakdown by project for a specific client"""
    from app import db
    from models import Client
    from utils.project_helper import is_project_feature_enabled, get_project_label
    from views.client_views import get_aged_balances_by_project_sql

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'No company selected'}), 400

    # Check if project feature is enabled
    if not is_project_feature_enabled(company):
        return jsonify({'error': 'Project feature not enabled'}), 403

    # Get client and verify it belongs to the company (SECURITY: company isolation)
    client = db.session.query(Client).filter_by(id=client_id, company_id=company.id).first()
    if not client:
        return jsonify({'error': 'Client not found'}), 404

    # Use centralized helper function to get project balances
    # SECURITY: Pass company_id to enforce tenant isolation
    breakdown = get_aged_balances_by_project_sql(
        [client_id],
        company.id,
        company.aging_calculation_method
    )

    # Get project label
    project_label = get_project_label(company)

    return jsonify({
        'project_label': project_label,
        'breakdown': breakdown
    })