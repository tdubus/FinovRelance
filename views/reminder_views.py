"""
Views for reminder management
Extracted from views.py monolith - Phase 6 Refactoring
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from app import limiter
from utils.audit_service import log_action, AuditActions, EntityTypes

# Create reminder blueprint
reminder_bp = Blueprint('reminder', __name__, url_prefix='/reminders')

# Note: CommunicationNote model doesn't exist - using CommunicationNote as proxy for reminders

@reminder_bp.route('/')
@login_required
def list_reminders():
    """List all reminders for company"""
    from app import db
    from models import CommunicationNote, Client

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
    status_filter = request.args.get('filter', 'all')  # Utilise 'filter' pour correspondre au template
    client_filter = request.args.get('client')

    # Base query for reminders - CORRECTION: Ne sélectionner que les notes avec reminder_date
    # Cela évite d'afficher les notes d'email automatiques comme des rappels
    query = db.session.query(CommunicationNote).join(Client).filter(
        Client.company_id == company.id,
        CommunicationNote.reminder_date.isnot(None)  # Seulement les vraies notes avec rappel
    )

    # Apply client filter first
    if client_filter:
        try:
            client_id = int(client_filter)
            query = query.filter(CommunicationNote.client_id == client_id)
        except (ValueError, TypeError):
            pass

    # Filter by user - tous les utilisateurs voient uniquement leurs propres rappels
    query = query.filter(CommunicationNote.user_id == current_user.id)

    # Get all reminders first, then filter by status using model methods for consistency
    all_reminders = query.order_by(CommunicationNote.created_at.desc()).all()

    # Apply status filter using model methods to ensure timezone consistency
    if status_filter == 'completed':
        reminders = [r for r in all_reminders if r.is_reminder_completed]
    elif status_filter == 'pending':
        reminders = [r for r in all_reminders if not r.is_reminder_completed]
    elif status_filter == 'overdue':
        reminders = [r for r in all_reminders if r.is_reminder_overdue()]
    elif status_filter == 'today':
        reminders = [r for r in all_reminders if r.is_reminder_today()]
    elif status_filter == 'upcoming':
        reminders = [r for r in all_reminders if r.is_reminder_upcoming()]
    else:
        reminders = all_reminders

    # Get clients for filter dropdown
    clients = db.session.query(Client).filter_by(company_id=company.id).order_by(Client.name).all()

    # Calculate counts for badges using all_reminders (before status filtering) for correct counts
    # CORRECTION: Utiliser les méthodes du modèle pour cohérence avec l'affichage et la logique timezone
    overdue_count = len([r for r in all_reminders if r.is_reminder_overdue()])
    today_count = len([r for r in all_reminders if r.is_reminder_today()])
    upcoming_count = len([r for r in all_reminders if r.is_reminder_upcoming()])
    completed_count = len([r for r in all_reminders if r.is_reminder_completed])
    total_count = len(all_reminders)

    return render_template('reminders/list.html',
                         reminders=reminders,
                         clients=clients,
                         status_filter=status_filter,
                         client_filter=client_filter,
                         company=company,
                         overdue_count=overdue_count,
                         today_count=today_count,
                         upcoming_count=upcoming_count,
                         completed_count=completed_count,
                         total_count=total_count,
                         filter_type=status_filter)


@reminder_bp.route('/api/load')
@limiter.exempt
@login_required
def api_load_reminders():
    """API endpoint to load reminders dynamically with search and pagination"""
    from app import db
    from models import CommunicationNote, Client, User
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload
    from utils import format_local_date

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Get filter parameters
    search = request.args.get('search', '').strip()
    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = 75

    # Base query for reminders with eager loading
    query = db.session.query(CommunicationNote).join(Client).options(
        joinedload(CommunicationNote.client),
        joinedload(CommunicationNote.user)
    ).filter(
        Client.company_id == company.id,
        CommunicationNote.reminder_date.isnot(None),
        CommunicationNote.user_id == current_user.id
    )

    # Apply search filter (by client name or client code)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Client.name.ilike(search_filter),
                Client.code_client.ilike(search_filter)
            )
        )

    # Get today's date for status filtering (using local timezone)
    from utils import get_local_today
    from datetime import date as date_type
    try:
        today = get_local_today()
    except Exception:
        today = date_type.today()

    # Build query for count calculations (counts query - no status filter yet)
    count_query = query

    # Apply status filter at SQL level for performance
    if status_filter == 'completed':
        query = query.filter(CommunicationNote.is_reminder_completed == True)
    elif status_filter == 'pending':
        query = query.filter(CommunicationNote.is_reminder_completed == False)
    elif status_filter == 'overdue':
        query = query.filter(
            CommunicationNote.is_reminder_completed == False,
            db.func.date(CommunicationNote.reminder_date) < today
        )
    elif status_filter == 'today':
        query = query.filter(
            CommunicationNote.is_reminder_completed == False,
            db.func.date(CommunicationNote.reminder_date) == today
        )
    elif status_filter == 'upcoming':
        query = query.filter(
            CommunicationNote.is_reminder_completed == False,
            db.func.date(CommunicationNote.reminder_date) > today
        )
    # else: 'all' - no additional filter

    # Sort by reminder_date ascending (oldest first) at SQL level
    query = query.order_by(CommunicationNote.reminder_date.asc())

    # Calculate total count for current filter (using SQL count, not loading all records)
    total = query.count()

    # Apply pagination at SQL level using offset and limit
    pages = (total + per_page - 1) // per_page if total > 0 else 1
    paginated_reminders = query.offset((page - 1) * per_page).limit(per_page).all()

    # Calculate counts for badges - each count uses a completely independent query to avoid mutation
    # Helper function to build base count query
    def build_count_query():
        q = db.session.query(CommunicationNote).join(Client).filter(
            Client.company_id == company.id,
            CommunicationNote.reminder_date.isnot(None),
            CommunicationNote.user_id == current_user.id
        )
        if search:
            q = q.filter(
                or_(
                    Client.name.ilike(f"%{search}%"),
                    Client.code_client.ilike(f"%{search}%")
                )
            )
        return q

    # Each count is completely independent
    overdue_count = build_count_query().filter(
        CommunicationNote.is_reminder_completed == False,
        db.func.date(CommunicationNote.reminder_date) < today
    ).count()

    today_count = build_count_query().filter(
        CommunicationNote.is_reminder_completed == False,
        db.func.date(CommunicationNote.reminder_date) == today
    ).count()

    upcoming_count = build_count_query().filter(
        CommunicationNote.is_reminder_completed == False,
        db.func.date(CommunicationNote.reminder_date) > today
    ).count()

    completed_count = build_count_query().filter(
        CommunicationNote.is_reminder_completed == True
    ).count()

    total_count = build_count_query().count()

    # Build JSON response
    reminders_data = []
    for reminder in paginated_reminders:
        # Determine status - using SQL-based logic to avoid re-executing Python helper methods
        # Since we already filtered by status in SQL, we can derive it more efficiently
        reminder_date_obj = reminder.reminder_date.date() if isinstance(reminder.reminder_date, datetime) else reminder.reminder_date

        if reminder.is_reminder_completed:
            status = 'completed'
            badge_class = 'bg-success'
            badge_text = 'Terminé'
            border_class = 'border-success'
        elif reminder_date_obj < today:
            status = 'overdue'
            badge_class = 'bg-danger'
            badge_text = 'En retard'
            border_class = 'border-danger'
        elif reminder_date_obj == today:
            status = 'today'
            badge_class = 'bg-warning'
            badge_text = "Aujourd'hui"
            border_class = 'border-warning'
        else:
            status = 'upcoming'
            badge_class = 'bg-info'
            badge_text = 'À venir'
            border_class = 'border-info'

        # Get note type icon
        if reminder.note_type == 'call':
            type_icon = 'phone'
        elif reminder.note_type == 'email':
            type_icon = 'mail'
        elif reminder.note_type == 'meeting':
            type_icon = 'user'
        else:
            type_icon = 'edit-3'

        reminders_data.append({
            'id': reminder.id,
            'client_id': reminder.client.id,
            'client_name': reminder.client.name,
            'client_code': reminder.client.code_client,
            'note_text': reminder.note_text,
            'note_type': reminder.note_type,
            'type_icon': type_icon,
            'reminder_date': reminder.reminder_date.strftime('%d/%m/%Y') if reminder.reminder_date else '',
            'created_at': format_local_date(reminder.created_at, '%d/%m/%Y') if reminder.created_at else '',
            'user_name': reminder.user.full_name if reminder.user else 'N/A',
            'is_reminder_completed': reminder.is_reminder_completed,
            'status': status,
            'badge_class': badge_class,
            'badge_text': badge_text,
            'border_class': border_class
        })

    return jsonify({
        'reminders': reminders_data,
        'pagination': {
            'page': page,
            'pages': pages,
            'total': total,
            'per_page': per_page,
            'has_prev': page > 1,
            'has_next': page < pages,
            'prev_num': page - 1 if page > 1 else None,
            'next_num': page + 1 if page < pages else None
        },
        'counts': {
            'overdue': overdue_count,
            'today': today_count,
            'upcoming': upcoming_count,
            'completed': completed_count,
            'total': total_count
        }
    })


@reminder_bp.route('/<int:reminder_id>/complete', methods=['POST'])
@reminder_bp.route('/complete/<int:reminder_id>', methods=['POST'])
@login_required
def complete_reminder(reminder_id):
    """Mark reminder as completed - new path order for AJAX, old path for backward compatibility"""
    from app import db
    from models import CommunicationNote, Client

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get reminder
    reminder = db.session.query(CommunicationNote).join(Client).filter(
        CommunicationNote.id == reminder_id,
        Client.company_id == company.id
    ).first()

    if not reminder:
        flash('Rappel non trouvé.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Check permissions - seul le propriétaire du rappel peut le compléter
    if reminder.user_id != current_user.id:
        flash('Accès refusé. Vous ne pouvez compléter que vos propres rappels.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Mark as completed
    reminder.is_reminder_completed = True

    # Check if AJAX request (JSON or has CSRF token)
    is_ajax = request.headers.get('X-CSRFToken') or 'application/json' in request.headers.get('Content-Type', '')

    try:
        db.session.commit()
        log_action(AuditActions.REMINDER_COMPLETED, entity_type=EntityTypes.CLIENT,
                  entity_id=reminder.client_id, entity_name=str(reminder_id))
        flash('Rappel marqué comme terminé.', 'success')
        if is_ajax:
            return jsonify({
                'success': True,
                'message': 'Rappel marqué comme terminé.',
                'status': 'completed'
            })
    except Exception as e:
        db.session.rollback()
        flash('Erreur lors de la mise à jour du rappel.', 'error')
        if is_ajax:
            return jsonify({'success': False, 'message': 'Erreur lors de la mise à jour du rappel.'})

    return redirect(url_for('reminder.list_reminders'))


@reminder_bp.route('/<int:reminder_id>/delete', methods=['POST'])
@reminder_bp.route('/delete/<int:reminder_id>', methods=['POST'])
@login_required
def delete_reminder(reminder_id):
    """Delete reminder - new path order for AJAX, old path for backward compatibility"""
    from app import db
    from models import CommunicationNote, Client

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get reminder
    reminder = db.session.query(CommunicationNote).join(Client).filter(
        CommunicationNote.id == reminder_id,
        Client.company_id == company.id
    ).first()

    if not reminder:
        flash('Rappel non trouvé.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Check permissions - seul le propriétaire du rappel peut le supprimer
    if reminder.user_id != current_user.id:
        flash('Accès refusé. Vous ne pouvez supprimer que vos propres rappels.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Check if AJAX request (JSON or has CSRF token)
    is_ajax = request.headers.get('X-CSRFToken') or 'application/json' in request.headers.get('Content-Type', '')

    try:
        client_id = reminder.client_id
        db.session.delete(reminder)
        db.session.commit()
        log_action(AuditActions.REMINDER_DELETED, entity_type=EntityTypes.CLIENT,
                  entity_id=client_id, entity_name=str(reminder_id))
        flash('Rappel supprimé avec succès.', 'success')
        if is_ajax:
            return jsonify({'success': True, 'message': 'Rappel supprimé avec succès.'})
    except Exception as e:
        db.session.rollback()
        flash('Erreur lors de la suppression du rappel.', 'error')
        if is_ajax:
            return jsonify({'success': False, 'message': 'Erreur lors de la suppression du rappel.'})

    return redirect(url_for('reminder.list_reminders'))


@reminder_bp.route('/reactivate/<int:reminder_id>', methods=['POST'])
@login_required
def reactivate_reminder(reminder_id):
    """Reactivate completed reminder"""
    from app import db
    from models import CommunicationNote, Client

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get reminder
    reminder = db.session.query(CommunicationNote).join(Client).filter(
        CommunicationNote.id == reminder_id,
        Client.company_id == company.id
    ).first()

    if not reminder:
        flash('Rappel non trouvé.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Check permissions - seul le propriétaire du rappel peut le réactiver
    if reminder.user_id != current_user.id:
        flash('Accès refusé. Vous ne pouvez réactiver que vos propres rappels.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Reactivate reminder
    reminder.is_reminder_completed = False

    # Check if AJAX request (JSON or has CSRF token)
    is_ajax = request.headers.get('X-CSRFToken') or 'application/json' in request.headers.get('Content-Type', '')

    try:
        db.session.commit()
        log_action(AuditActions.REMINDER_REACTIVATED, entity_type=EntityTypes.CLIENT,
                  entity_id=reminder.client_id, entity_name=str(reminder_id))

        # Calculer le statut du rappel pour la réponse AJAX
        status = 'upcoming'
        if reminder.is_reminder_overdue():
            status = 'overdue'
        elif reminder.is_reminder_today():
            status = 'today'

        flash('Rappel réactivé avec succès.', 'success')
        if is_ajax:
            return jsonify({
                'success': True,
                'message': 'Rappel réactivé avec succès.',
                'status': status
            })
    except Exception as e:
        db.session.rollback()
        flash('Erreur lors de la réactivation du rappel.', 'error')
        if is_ajax:
            return jsonify({'success': False, 'message': 'Erreur lors de la réactivation du rappel.'})

    return redirect(url_for('reminder.list_reminders'))


# API endpoint for getting reminder details
@reminder_bp.route('/api/<int:reminder_id>')
@limiter.exempt
@login_required
def api_get_reminder(reminder_id):
    """API endpoint to get reminder details"""
    from app import db
    from models import CommunicationNote, Client

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Get reminder
    reminder = db.session.query(CommunicationNote).join(Client).filter(
        CommunicationNote.id == reminder_id,
        Client.company_id == company.id
    ).first()

    if not reminder:
        return jsonify({'success': False, 'error': 'Rappel non trouvé'}), 404

    # Check permissions - seul le propriétaire du rappel peut y accéder
    if reminder.user_id != current_user.id:
        return jsonify({'success': False, 'error': 'Accès refusé'}), 403

    # Get client and user info via proper queries to avoid relationship issues
    client = db.session.query(Client).filter_by(id=reminder.client_id).first()
    from models import User
    user = db.session.query(User).filter_by(id=reminder.user_id).first()
    from utils import format_local_date

    # Check if this is a real email (has email_subject or email_body)
    is_real_email = bool(reminder.email_subject or reminder.email_body)

    response_data = {
        'success': True,
        'id': reminder.id,
        'note_text': reminder.note_text,
        'note_type': reminder.note_type,
        'reminder_date': reminder.reminder_date.date().isoformat() if reminder.reminder_date else None,
        'is_reminder_completed': reminder.is_reminder_completed,
        'client_id': client.id if client else None,
        'client_name': client.name if client else 'N/A',
        'client_code': client.code_client if client else 'N/A',
        'client_display': f"{client.code_client} - {client.name}" if client else 'N/A',
        'is_urgent': reminder.is_urgent,
        'user_name': user.full_name if user else 'N/A',
        'author': user.full_name if user else 'N/A',
        'created_at': format_local_date(reminder.created_at, '%d/%m/%Y à %H:%M') if reminder.created_at else None,
        'updated_at': reminder.updated_at.isoformat() if reminder.updated_at else None,
        'note_date': reminder.note_date.isoformat() if reminder.note_date else None,
        'is_real_email': is_real_email,
    }

    # Add email fields if it's a real email
    if is_real_email:
        response_data['email_from'] = reminder.email_from
        response_data['email_to'] = reminder.email_to
        response_data['email_subject'] = reminder.email_subject
        response_data['email_body'] = reminder.email_body
        response_data['email_body_html'] = reminder.email_body
        response_data['attachments'] = reminder.attachments if reminder.attachments else []

    return jsonify(response_data)


@reminder_bp.route('/<int:reminder_id>/edit', methods=['POST'])
@login_required
def edit_reminder(reminder_id):
    """Edit reminder from the reminders list page"""
    from app import db
    from models import CommunicationNote, Client
    from datetime import datetime

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get reminder
    reminder = db.session.query(CommunicationNote).join(Client).filter(
        CommunicationNote.id == reminder_id,
        Client.company_id == company.id
    ).first()

    if not reminder:
        flash('Rappel non trouvé.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    # Check permissions - seul le propriétaire du rappel peut le modifier
    if reminder.user_id != current_user.id:
        flash('Accès refusé. Vous ne pouvez modifier que vos propres rappels.', 'error')
        return redirect(url_for('reminder.list_reminders'))

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.headers.get('X-CSRFToken')

    try:
        # Update note text
        note_text = request.form.get('note_text')
        if note_text:
            reminder.note_text = note_text.strip()

        # Update reminder date
        reminder_date_str = request.form.get('reminder_date')
        if reminder_date_str:
            try:
                # Parse the date string (expecting YYYY-MM-DD format from date input)
                reminder.reminder_date = datetime.strptime(reminder_date_str, '%Y-%m-%d').date()
            except ValueError:
                # If parsing fails, try to handle datetime format
                try:
                    parsed_datetime = datetime.strptime(reminder_date_str, '%Y-%m-%dT%H:%M')
                    reminder.reminder_date = parsed_datetime.date()
                except ValueError:
                    if is_ajax:
                        return jsonify({'success': False, 'error': 'Format de date invalide'}), 400
                    flash('Format de date invalide.', 'error')
                    return redirect(url_for('reminder.list_reminders'))
        else:
            reminder.reminder_date = None

        # Update modification timestamp
        reminder.updated_at = datetime.utcnow()

        db.session.commit()

        if is_ajax:
            return jsonify({'success': True, 'message': 'Rappel modifié avec succès'})

        flash('Rappel modifié avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur modification rappel: {str(e)}")
        if is_ajax:
            return jsonify({'success': False, 'error': 'Erreur lors de la modification du rappel.'}), 500
        flash('Erreur lors de la modification du rappel.', 'error')

    return redirect(url_for('reminder.list_reminders'))