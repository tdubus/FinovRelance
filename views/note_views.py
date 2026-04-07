"""
Note management views for the Flask application
Centralized notes and emails management with dynamic filtering
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from datetime import datetime, date
import os
import io
import xlsxwriter
from app import db, limiter
from models import CommunicationNote, Client, User, ClientContact
from forms import NoteForm, EmailNoteForm
from sqlalchemy import or_, and_, func
from utils.note_grouping import get_conversation_counts_and_data, group_notes_by_conversation
from utils.audit_service import log_action, AuditActions, EntityTypes

# Create blueprint
note_bp = Blueprint('note', __name__, url_prefix='/notes')


@note_bp.route('/')
@login_required
def list_notes():
    """List all notes and emails for the selected company with pagination and filters"""
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
    search = request.args.get('search', '').strip()
    collector_id = request.args.get('collector_id', type=int)
    note_type = request.args.get('note_type', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Build query with joins for efficient loading
    query = db.session.query(CommunicationNote).join(
        Client, CommunicationNote.client_id == Client.id
    ).join(
        User, CommunicationNote.user_id == User.id
    ).filter(
        CommunicationNote.company_id == company.id
    )

    # Apply search filter (search on client name and code)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Client.name.ilike(search_filter),
                Client.code_client.ilike(search_filter)
            )
        )

    # Apply collector filter with company validation
    if collector_id:
        from models import UserCompany
        is_valid_collector = UserCompany.query.filter_by(
            user_id=collector_id,
            company_id=company.id,
            is_active=True
        ).first() is not None
        if is_valid_collector:
            query = query.filter(CommunicationNote.user_id == collector_id)

    # Apply note type filter
    if note_type:
        query = query.filter(CommunicationNote.note_type == note_type)

    # Order by date descending (most recent first)
    query = query.order_by(CommunicationNote.created_at.desc())

    # Execute pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Pre-calculate conversation counts and get newest/oldest for each conversation
    from utils.note_grouping import get_conversation_counts_and_data, group_notes_by_conversation
    conv_ids = [n.conversation_id for n in pagination.items if n.conversation_id]
    conversation_counts, conversation_data = get_conversation_counts_and_data(
        db.session, conv_ids, company.id)

    # Group notes by conversation - newest in parent, oldest as reference
    note_groups = group_notes_by_conversation(pagination.items, conversation_counts, conversation_data)

    # Get all collectors (users) for filter dropdown
    collectors = db.session.query(User).join(
        CommunicationNote, CommunicationNote.user_id == User.id
    ).filter(
        CommunicationNote.company_id == company.id
    ).distinct().order_by(User.first_name, User.last_name).all()

    from datetime import date
    return render_template('notes/list.html',
                         note_groups=note_groups,
                         pagination=pagination,
                         search=search,
                         collector_id=collector_id,
                         note_type=note_type,
                         collectors=collectors,
                         company=company,
                         user_role=user_role,
                         today=date.today().isoformat())


@note_bp.route('/api/search')
@limiter.exempt
@login_required
def api_search_notes():
    """API endpoint for dynamic search with AJAX - returns HTML partial"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)

    # Get parameters
    search = request.args.get('search', '').strip()
    collector_id = request.args.get('collector_id', type=int)
    note_type = request.args.get('note_type', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Build query
    query = db.session.query(CommunicationNote).join(
        Client, CommunicationNote.client_id == Client.id
    ).join(
        User, CommunicationNote.user_id == User.id
    ).filter(
        CommunicationNote.company_id == company.id
    )

    # Apply filters
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Client.name.ilike(search_filter),
                Client.code_client.ilike(search_filter)
            )
        )

    if collector_id:
        from models import UserCompany
        is_valid_collector = UserCompany.query.filter_by(
            user_id=collector_id,
            company_id=company.id,
            is_active=True
        ).first() is not None
        if is_valid_collector:
            query = query.filter(CommunicationNote.user_id == collector_id)

    if note_type:
        query = query.filter(CommunicationNote.note_type == note_type)

    # Order and paginate
    query = query.order_by(CommunicationNote.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Pre-calculate conversation counts and get newest/oldest for each conversation
    conv_ids = [n.conversation_id for n in pagination.items if n.conversation_id]
    conversation_counts, conversation_data = get_conversation_counts_and_data(
        db.session, conv_ids, company.id)

    # Group notes by conversation - newest in parent, oldest as reference
    note_groups = group_notes_by_conversation(pagination.items, conversation_counts, conversation_data)

    # Render HTML partial
    html = render_template('notes/_notes_table_body.html',
                          note_groups=note_groups,
                          company=company,
                          user_role=user_role)

    return jsonify({
        'html': html,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    })


@note_bp.route('/api/conversation_children')
@limiter.exempt
@login_required
def api_conversation_children():
    """
    Fonction vérifiée par MDF le 30/01/2026.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """API endpoint to load conversation children dynamically for Notes page

    Le parent affiché est le PLUS RÉCENT. Les enfants incluent tous les autres
    messages, y compris l'ORIGINAL (le plus ancien) avec un badge distinctif.

    Inclut aussi les messages liés via parent_note_id (transferts avec sujet modifié)
    qui ont un conversationId différent mais sont logiquement liés.

    IMPORTANT: Si le parent_note_id pointe vers une note avec un conversation_id
    différent (cas d'un transfert), on suit la chaîne parent_note_id pour trouver
    la conversation racine et récupérer tous les messages.
    """
    import logging
    logger = logging.getLogger(__name__)

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    conversation_id = request.args.get('conversation_id', '').strip()
    parent_note_id = request.args.get('parent_note_id', type=int)

    logger.info(f"api_conversation_children: conv={conversation_id[:30] if conversation_id else 'None'}..., parent_note_id={parent_note_id}")

    if not conversation_id:
        return jsonify({'error': 'conversation_id requis'}), 400

    user_role = current_user.get_role_in_company(company.id)

    # Étape 0: Si le parent affiché a un parent_note_id, suivre la chaîne pour trouver
    # la conversation racine (cas d'un transfert avec sujet modifié)
    root_conversation_id = conversation_id
    all_conversation_ids = {conversation_id}

    if parent_note_id:
        parent_note = db.session.query(CommunicationNote).filter(
            CommunicationNote.id == parent_note_id,
            CommunicationNote.company_id == company.id
        ).first()

        if parent_note and parent_note.parent_note_id:
            # Suivre la chaîne parent_note_id pour trouver la racine
            current_note = parent_note
            visited = {parent_note.id}
            max_depth = 10  # Protection contre boucle infinie
            depth = 0

            while current_note.parent_note_id and depth < max_depth:
                depth += 1
                ancestor = db.session.query(CommunicationNote).filter(
                    CommunicationNote.id == current_note.parent_note_id,
                    CommunicationNote.company_id == company.id
                ).first()

                if not ancestor or ancestor.id in visited:
                    break

                visited.add(ancestor.id)
                if ancestor.conversation_id:
                    all_conversation_ids.add(ancestor.conversation_id)
                    root_conversation_id = ancestor.conversation_id
                current_note = ancestor

            logger.info(f"api_conversation_children: Followed chain, root_conv={root_conversation_id[:30]}..., all_convs={len(all_conversation_ids)}")

    # Étape 1: Récupérer tous les messages de TOUTES les conversations liées
    base_messages = db.session.query(CommunicationNote).join(
        Client, CommunicationNote.client_id == Client.id
    ).join(
        User, CommunicationNote.user_id == User.id
    ).filter(
        CommunicationNote.company_id == company.id,
        CommunicationNote.conversation_id.in_(all_conversation_ids),
        CommunicationNote.note_type == 'email'
    ).all()

    # Collecter les IDs des messages de base pour chercher les notes liées
    base_ids = {n.id for n in base_messages}
    logger.info(f"api_conversation_children: base_ids={base_ids}")

    # Étape 2: Récupérer les notes liées via parent_note_id (transferts avec sujet modifié)
    # Ces notes ont un conversationId différent mais sont logiquement liées
    linked_messages = []
    if base_ids:
        linked_messages = db.session.query(CommunicationNote).join(
            Client, CommunicationNote.client_id == Client.id
        ).join(
            User, CommunicationNote.user_id == User.id
        ).filter(
            CommunicationNote.company_id == company.id,
            CommunicationNote.parent_note_id.in_(base_ids),
            CommunicationNote.note_type == 'email',
            ~CommunicationNote.id.in_(base_ids)  # Éviter les doublons
        ).all()
        logger.info(f"api_conversation_children: linked_messages={[n.id for n in linked_messages]}")

    # Combiner et trier du plus récent au plus ancien
    all_messages = base_messages + linked_messages
    all_messages.sort(key=lambda n: n.created_at, reverse=True)

    # Identifier l'ID du message original (le plus ancien de la conversation racine)
    original_id = None
    root_messages = [n for n in base_messages if n.conversation_id == root_conversation_id]
    if root_messages:
        root_sorted = sorted(root_messages, key=lambda n: n.created_at)
        original_id = root_sorted[0].id
    elif base_messages:
        base_sorted = sorted(base_messages, key=lambda n: n.created_at)
        original_id = base_sorted[0].id

    # Exclure le parent (le plus récent, qui est affiché en ligne principale)
    children = [n for n in all_messages if n.id != parent_note_id]

    html = render_template('notes/_conversation_children.html',
                          children=children,
                          conversation_id=root_conversation_id,
                          original_id=original_id,
                          company=company,
                          user_role=user_role)

    return jsonify({
        'html': html,
        'count': len(children),
        'original_id': original_id
    })


@note_bp.route('/api/clients')
@limiter.exempt
@login_required
def api_search_clients():
    """API endpoint to search clients dynamically"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    search = request.args.get('q', '').strip()

    query = Client.query.filter_by(company_id=company.id)

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Client.name.ilike(search_filter),
                Client.code_client.ilike(search_filter)
            )
        )

    clients = query.order_by(Client.code_client).limit(20).all()

    results = [{
        'id': client.id,
        'code': client.code_client,
        'name': client.name,
        'display': f"{client.code_client} - {client.name}"
    } for client in clients]

    return jsonify({'clients': results})


@note_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_note():
    """Create a new note"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('note.list_notes'))

    form = NoteForm()

    # For AJAX requests from client detail (hidden input), validate client_id manually
    # to avoid SelectField choices limit issue. For GET (notes list dropdown), populate choices.
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        submitted_client_id = request.form.get('client_id', type=int)
        if submitted_client_id:
            client = Client.query.filter_by(id=submitted_client_id, company_id=company.id).first()
            if client:
                form.client_id.choices = [(client.id, client.name)]
            else:
                return jsonify({'success': False, 'error': 'Client introuvable.'}), 400
        else:
            return jsonify({'success': False, 'error': 'Veuillez sélectionner un client.'}), 400
    else:
        clients = Client.query.filter_by(company_id=company.id).order_by(Client.code_client).limit(500).all()
        form.client_id.choices = [(0, '-- Sélectionner un client --')] + [
            (c.id, f"{c.code_client} - {c.name}") for c in clients
        ]

    if form.validate_on_submit():
        try:
            note = CommunicationNote(
                client_id=form.client_id.data,
                user_id=current_user.id,
                company_id=company.id,
                note_type=form.note_type.data,
                note_text=form.note_text.data,
                note_date=form.note_date.data,
                reminder_date=form.reminder_date.data if form.reminder_date.data else None
            )

            db.session.add(note)
            db.session.commit()

            log_action(AuditActions.NOTE_CREATED, entity_type=EntityTypes.CLIENT,
                      entity_id=form.client_id.data, details={'note_type': form.note_type.data})

            flash('Note créée avec succès.', 'success')

            # Return JSON for AJAX request
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'message': 'Note créée avec succès'})

            return redirect(url_for('note.list_notes'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating note: {str(e)}")
            flash('Erreur lors de la création de la note.', 'error')

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Erreur lors de la création de la note'}), 500

    # Return errors for AJAX
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'errors': form.errors}), 400

    # Note creation is done via AJAX modals — no standalone form page
    if request.method == 'GET':
        return redirect(url_for('note.list_notes'))

    return render_template('notes/list.html', company=company)


@note_bp.route('/<int:note_id>', methods=['GET'])
@login_required
def get_note(note_id):
    """Get note details in JSON format for AJAX requests"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Verify note belongs to company
    note = CommunicationNote.query.filter_by(id=note_id, company_id=company.id).first()
    if not note:
        return jsonify({'error': 'Note introuvable'}), 404

    # Check permissions - all roles can view
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe', 'lecteur']:
        return jsonify({'error': 'Accès refusé'}), 403

    # Format data for JSON response
    from utils import format_local_datetime
    from markupsafe import Markup
    import json

    # Prepare attachments - normalize from JSON if needed
    attachments_list = []
    if note.attachments:
        if isinstance(note.attachments, list):
            attachments_list = note.attachments
        elif isinstance(note.attachments, str):
            try:
                attachments_list = json.loads(note.attachments)
            except (json.JSONDecodeError, TypeError):
                attachments_list = []

    # Check if this is a real email (has email_subject or email_body)
    is_real_email = bool(note.email_subject or note.email_body)

    # Permissions: can edit only if not read-only AND (creator OR admin)
    # Employees can only edit their own notes
    can_edit = (
        not current_user.is_read_only() and
        (note.user_id == current_user.id or user_role in ['super_admin', 'admin'])
    )

    response_data = {
        'id': note.id,
        'client_id': note.client_id,
        'client_code': note.client.code_client,
        'client_name': note.client.name,
        'client_display': f"{note.client.code_client} - {note.client.name}",
        'note_type': note.note_type,
        'note_text': note.note_text or '',
        'note_date': note.note_date.isoformat() if note.note_date else '',
        'reminder_date': note.reminder_date.strftime('%Y-%m-%d') if note.reminder_date else None,
        'created_at': format_local_datetime(note.created_at, '%Y-%m-%d %H:%M', company.timezone),
        'author': f"{note.user.first_name} {note.user.last_name}",
        'is_real_email': is_real_email,
        # Email-specific fields (always include for compatibility)
        'email_from': note.email_from,
        'email_to': note.email_to,
        'email_cc': None,  # CC not stored in database, included for UI compatibility
        'email_subject': note.email_subject,
        'email_body': note.email_body,
        'email_body_html': note.email_body if note.email_body else None,
        'attachments': attachments_list,
        # Editing permissions
        'can_edit': can_edit
    }

    return jsonify(response_data)


@note_bp.route('/<int:note_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_note(note_id):
    """Edit an existing note"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Verify note belongs to company
    note = CommunicationNote.query.filter_by(id=note_id, company_id=company.id).first()
    if not note:
        flash('Note introuvable.', 'error')
        return redirect(url_for('note.list_notes'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('note.list_notes'))

    # Only note creator can edit (unless admin)
    if note.user_id != current_user.id and user_role not in ['super_admin', 'admin']:
        flash('Vous ne pouvez modifier que vos propres notes.', 'error')
        return redirect(url_for('note.list_notes'))

    # If this is an email note with email_subject or email_body, use special email template
    if note.note_type == 'email' and (note.email_subject or note.email_body):
        from forms import EmailDetailForm

        form = EmailDetailForm()

        # Pre-fill form on GET
        if request.method == 'GET':
            form.additional_note.data = note.note_text
            form.reminder_date.data = note.reminder_date

        if form.validate_on_submit():
            try:
                note.note_text = form.additional_note.data
                note.reminder_date = form.reminder_date.data if form.reminder_date.data else None
                note.updated_by = current_user.id
                note.updated_at = datetime.utcnow()

                db.session.commit()

                # Return JSON for AJAX
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': True, 'message': 'Note modifiée avec succès'})

                flash('Note modifiée avec succès.', 'success')
                return redirect(url_for('note.list_notes'))

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error updating email note: {str(e)}")
                flash('Erreur lors de la modification de la note.', 'error')

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': False, 'error': 'Erreur lors de la modification'}), 500

        # Return errors for AJAX
        if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            current_app.logger.error(f"Email note edit validation failed for note {note_id}: {form.errors}, form data: {dict(request.form)}")
            return jsonify({'success': False, 'errors': form.errors}), 400

        return render_template('notes/email_detail.html', form=form, note=note, company=company)

    # Regular note editing
    form = NoteForm(obj=note)

    # For AJAX requests, validate client_id manually to avoid SelectField choices limit issue
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        submitted_client_id = request.form.get('client_id', type=int)
        if submitted_client_id:
            client_obj = Client.query.filter_by(id=submitted_client_id, company_id=company.id).first()
            if client_obj:
                form.client_id.choices = [(client_obj.id, client_obj.name)]
            else:
                return jsonify({'success': False, 'error': 'Client introuvable.'}), 400
        else:
            form.client_id.choices = [(note.client_id, '')]
    else:
        clients = Client.query.filter_by(company_id=company.id).order_by(Client.code_client).limit(500).all()
        form.client_id.choices = [(c.id, f"{c.code_client} - {c.name}") for c in clients]

    if form.validate_on_submit():
        try:
            note.client_id = form.client_id.data
            note.note_type = form.note_type.data
            note.note_text = form.note_text.data
            note.note_date = form.note_date.data
            note.reminder_date = form.reminder_date.data if form.reminder_date.data else None
            note.updated_by = current_user.id
            note.updated_at = datetime.utcnow()

            db.session.commit()

            # Return JSON for AJAX
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'message': 'Note modifiée avec succès'})

            flash('Note modifiée avec succès.', 'success')
            return redirect(url_for('note.list_notes'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating note: {str(e)}")
            flash('Erreur lors de la modification de la note.', 'error')

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Erreur lors de la modification'}), 500

    # Return errors for AJAX
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        current_app.logger.error(f"Note edit validation failed for note {note_id}: {form.errors}, form data: {dict(request.form)}")
        return jsonify({'success': False, 'errors': form.errors}), 400

    return render_template('notes/note_form.html', form=form, company=company, title='Modifier note', note=note)


@note_bp.route('/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_note(note_id):
    """Delete a note"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Verify note belongs to company
    note = CommunicationNote.query.filter_by(id=note_id, company_id=company.id).first()
    if not note:
        return jsonify({'error': 'Note introuvable'}), 404

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'error': 'Accès refusé'}), 403

    # Only creator or admin can delete
    if note.user_id != current_user.id and user_role not in ['super_admin', 'admin']:
        return jsonify({'error': 'Vous ne pouvez supprimer que vos propres notes'}), 403

    try:
        client_id = note.client_id
        db.session.delete(note)
        db.session.commit()

        log_action(AuditActions.NOTE_DELETED, entity_type=EntityTypes.CLIENT,
                  entity_id=client_id, entity_name=str(note_id))

        flash('Note supprimée avec succès.', 'success')
        return jsonify({'success': True, 'message': 'Note supprimée avec succès'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting note: {str(e)}")
        return jsonify({'error': 'Erreur lors de la suppression de la note'}), 500


@note_bp.route('/email/new', methods=['GET', 'POST'])
@login_required
def new_email():
    """Create and send a new email"""
    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('note.list_notes'))

    form = EmailNoteForm()

    # For AJAX requests, validate client_id manually to avoid SelectField choices limit issue
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        submitted_client_id = request.form.get('client_id', type=int)
        if submitted_client_id:
            client_obj = Client.query.filter_by(id=submitted_client_id, company_id=company.id).first()
            if client_obj:
                form.client_id.choices = [(client_obj.id, client_obj.name)]
            else:
                return jsonify({'success': False, 'error': 'Client introuvable.'}), 400
        else:
            return jsonify({'success': False, 'error': 'Veuillez sélectionner un client.'}), 400
    else:
        clients = Client.query.filter_by(company_id=company.id).order_by(Client.code_client).limit(500).all()
        form.client_id.choices = [(0, '-- Sélectionner un client --')] + [
            (c.id, f"{c.code_client} - {c.name}") for c in clients
        ]

    if form.validate_on_submit():
        try:
            # Create email note
            note = CommunicationNote(
                client_id=form.client_id.data,
                user_id=current_user.id,
                company_id=company.id,
                note_type='email',
                note_text=f"Courriel envoyé: {form.email_subject.data}",
                note_date=date.today(),
                email_from=form.email_from.data,
                email_to=form.email_to.data,
                email_subject=form.email_subject.data,
                email_body=form.email_body.data
            )

            db.session.add(note)
            db.session.commit()

            flash('Courriel créé avec succès.', 'success')

            # Return JSON for AJAX
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'message': 'Courriel créé avec succès'})

            return redirect(url_for('note.list_notes'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating email: {str(e)}")
            flash('Erreur lors de la création du courriel.', 'error')

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Erreur lors de la création'}), 500

    # Return errors for AJAX
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'errors': form.errors}), 400

    return render_template('notes/email_form.html', form=form, company=company, title='Nouveau courriel')


@note_bp.route('/export/excel')
@login_required
@limiter.limit("10 per minute")
def export_excel():
    """Export notes to Excel with applied filters"""
    from flask import make_response
    from utils import format_local_datetime

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe', 'lecteur']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('note.list_notes'))

    try:
        # Get filter parameters (same as list_notes)
        search = request.args.get('search', '').strip()
        collector_id = request.args.get('collector_id', type=int)
        note_type = request.args.get('note_type', '').strip()

        # Build query with joins + contains_eager pour que yield_per puisse streamer
        # sans declencher de lazy-load N+1 sur note.client et note.user
        from sqlalchemy.orm import contains_eager
        query = db.session.query(CommunicationNote).join(
            Client, CommunicationNote.client_id == Client.id
        ).join(
            User, CommunicationNote.user_id == User.id
        ).options(
            contains_eager(CommunicationNote.client),
            contains_eager(CommunicationNote.user)
        ).filter(
            CommunicationNote.company_id == company.id
        )

        # Apply search filter (search on client name and code)
        if search:
            search_filter = f"%{search}%"
            query = query.filter(
                or_(
                    Client.name.ilike(search_filter),
                    Client.code_client.ilike(search_filter)
                )
            )

        # Apply collector filter with company validation
        if collector_id:
            from models import UserCompany
            is_valid_collector = UserCompany.query.filter_by(
                user_id=collector_id,
                company_id=company.id,
                is_active=True
            ).first() is not None
            if is_valid_collector:
                query = query.filter(CommunicationNote.user_id == collector_id)

        # Apply note type filter
        if note_type:
            query = query.filter(CommunicationNote.note_type == note_type)

        # Order by date descending (most recent first)
        # Limite de securite (50k) + streaming par batch de 500 pour eviter OOM
        MAX_EXPORT_NOTES = 50000
        notes = query.order_by(CommunicationNote.created_at.desc()).limit(MAX_EXPORT_NOTES).yield_per(500)

        # Create Excel file
        excel_buffer = io.BytesIO()
        workbook = xlsxwriter.Workbook(excel_buffer)
        worksheet = workbook.add_worksheet('Notes')

        # Define formats
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#4472C4',
            'font_color': 'white',
            'align': 'center',
            'valign': 'vcenter',
            'border': 1
        })

        cell_format = workbook.add_format({
            'align': 'left',
            'valign': 'top',
            'text_wrap': True,
            'border': 1
        })

        date_format = workbook.add_format({
            'align': 'center',
            'valign': 'top',
            'border': 1
        })

        # Set column widths
        worksheet.set_column(0, 0, 15)  # Code client
        worksheet.set_column(1, 1, 30)  # Nom client
        worksheet.set_column(2, 2, 12)  # Type
        worksheet.set_column(3, 3, 20)  # Auteur
        worksheet.set_column(4, 4, 60)  # Contenu
        worksheet.set_column(5, 5, 18)  # Date

        # Headers
        headers = ['Code Client', 'Nom Client', 'Type', 'Auteur', 'Contenu', 'Date/Heure']
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)

        # Data rows
        row = 1
        for note in notes:
            # Code client
            worksheet.write(row, 0, note.client.code_client or '', cell_format)

            # Nom client
            worksheet.write(row, 1, note.client.name or '', cell_format)

            # Type
            worksheet.write(row, 2, get_note_type_display(note.note_type), cell_format)

            # Auteur
            author_name = f"{note.user.first_name} {note.user.last_name}"
            worksheet.write(row, 3, author_name, cell_format)

            # Contenu - Pour les courriels, ne pas afficher le contenu du courriel
            if note.note_type == 'email':
                # Pour les courriels, afficher simplement "Courriel envoyé" ou le sujet
                if note.email_subject:
                    content = f"Objet: {note.email_subject}"
                else:
                    content = note.note_text or "Courriel"
            else:
                # Pour les autres types, afficher le contenu de la note
                content = note.note_text or ''
            worksheet.write(row, 4, content, cell_format)

            # Date/Heure - Basé sur le fuseau horaire de l'entreprise
            date_str = format_local_datetime(note.created_at, '%Y-%m-%d %H:%M', company.timezone)
            worksheet.write(row, 5, date_str, date_format)

            row += 1

        # Add autofilter (row > 1 signifie qu'au moins une note a ete ecrite)
        if row > 1:
            worksheet.autofilter(0, 0, row - 1, 5)

        workbook.close()

        # Prepare response
        excel_buffer.seek(0)

        # Generate filename with current date
        from datetime import datetime as dt
        filename = f"notes_export_{dt.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        response = make_response(excel_buffer.getvalue())
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'

        return response

    except Exception as e:
        current_app.logger.error(f"Error exporting notes to Excel: {str(e)}")
        flash('Erreur lors de l\'exportation Excel.', 'error')
        return redirect(url_for('note.list_notes'))


def get_note_type_display(note_type):
    """Get display text for note type"""
    types = {
        'general': 'Note',
        'call': 'Appel',
        'email': 'Courriel',
        'meeting': 'Rencontre'
    }
    return types.get(note_type, note_type)


@note_bp.route('/api/email-templates')
@limiter.exempt
@login_required
def get_email_templates():
    """Get available email templates for the company (API endpoint)"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    from models import EmailTemplate
    user_role = current_user.get_role_in_company(company.id)

    if user_role in ['super_admin', 'admin']:
        # Admins see all templates
        templates = EmailTemplate.query.filter_by(
            company_id=company.id,
            is_active=True
        ).order_by(EmailTemplate.name).all()
    else:
        # Employees see only shared templates and their own
        templates = EmailTemplate.query.filter(
            EmailTemplate.company_id == company.id,
            EmailTemplate.is_active == True,
            db.or_(
                EmailTemplate.is_shared == True,
                EmailTemplate.created_by == current_user.id
            )
        ).order_by(EmailTemplate.name).all()

    return jsonify({
        'templates': [{
            'id': t.id,
            'name': t.name,
            'subject': t.subject,
            'content': t.content
        } for t in templates]
    })


@note_bp.route('/api/client-contacts/<int:client_id>')
@limiter.exempt
@login_required
def get_client_contacts(client_id):
    """Get available contacts for a client (API endpoint)"""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Verify client belongs to company
    client = Client.query.filter_by(id=client_id, company_id=company.id).first()
    if not client:
        return jsonify({'error': 'Client introuvable'}), 404

    # Build available contacts list (same logic as email_views.py)
    available_contacts = []

    # Always include main client contact(s) first
    if client.email:
        from utils import split_client_emails
        client_emails = split_client_emails(client.email)
        for addr in client_emails:
            available_contacts.append({
                'email': addr,
                'full_name': f"{client.name} (Contact principal)",
                'language': client.language or 'fr',
                'client_id': client.id
            })

    # Add secondary contacts
    try:
        from models import ClientContact
        contacts = ClientContact.query.filter_by(client_id=client.id).all()
        for contact in contacts:
            if contact.email and contact.email != client.email:
                available_contacts.append({
                    'email': contact.email,
                    'full_name': f"{contact.first_name} {contact.last_name}".strip(),
                    'language': contact.language or 'fr',
                    'client_id': client.id
                })
    except Exception as e:
        current_app.logger.error(f"Error loading contacts: {str(e)}")

    # Calculate total outstanding for client
    total_outstanding = 0
    try:
        from models import Invoice
        invoices = Invoice.query.filter_by(client_id=client.id, is_paid=False).all()
        total_outstanding = sum(float(inv.amount or 0) for inv in invoices)
    except Exception as e:
        current_app.logger.error(f"Error calculating outstanding balance: {str(e)}")

    # Format currency with company settings
    from utils import format_currency
    company_currency = client.company.currency if client.company else 'CAD'
    total_outstanding_formatted = format_currency(total_outstanding, company_currency)

    # Check if client has children (using the built-in property)
    has_children = client.is_parent

    return jsonify({
        'contacts': available_contacts,
        'client': {
            'id': client.id,
            'name': client.name,
            'code': client.code_client,
            'email': client.email,
            'phone': client.phone,
            'payment_terms': client.payment_terms,
            'total_outstanding': total_outstanding_formatted,
            'has_children': has_children
        }
    })


@note_bp.route('/email/send', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def send_email_from_notes():
    """
    Fonction vérifiée par MDF le 30/01/2026.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Send email from notes tab - FULL IMPLEMENTATION with attachments"""
    from models import EmailConfiguration

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'error': 'Accès refusé'}), 403

    try:
        client_id = request.form.get('client_id')
        if not client_id:
            return jsonify({'error': 'Client non spécifié'}), 400

        client = Client.query.filter_by(id=client_id, company_id=company.id).first()
        if not client:
            return jsonify({'error': 'Client introuvable'}), 404

        # Get user email configuration
        user_email_config = EmailConfiguration.query.filter_by(
            user_id=current_user.id,
            company_id=company.id
        ).first()

        if not user_email_config or (not user_email_config.is_outlook_connected() and not user_email_config.is_gmail_connected()):
            return jsonify({'error': 'Aucune connexion email configurée. Configurez Outlook ou Gmail.'}), 400

        # Refresh Outlook token if needed
        if user_email_config.is_outlook_connected() and user_email_config.needs_token_refresh():
            try:
                from email_fallback import refresh_user_oauth_token
                refresh_user_oauth_token(user_email_config)
            except Exception as e:
                current_app.logger.error(f"Failed to refresh Outlook token: {str(e)}")
                return jsonify({'error': 'Erreur de rafraîchissement du token Outlook'}), 400

        # Get form data
        subject = request.form.get('subject', '')
        content = request.form.get('content', '')
        to_emails_str = request.form.get('to_emails', '')
        cc_emails_str = request.form.get('cc_emails', '')
        attachment_language = request.form.get('attachment_language', 'fr')
        attach_pdf = request.form.get('attach_pdf') == 'on'
        attach_excel = request.form.get('attach_excel') == 'on'
        include_children = request.form.get('include_children') == 'on'

        # Replace variables
        variables = {
            '{client_name}': client.name or '',
            '{client_code}': client.code_client or '',
            '{client_email}': client.email or '',
            '{client_phone}': client.phone or '',
            '{client_payment_terms}': client.payment_terms or '',
            '{company_name}': company.name or '',
            '{user_name}': f"{current_user.first_name} {current_user.last_name}".strip(),
            '{today_date}': datetime.now().strftime('%d/%m/%Y')
        }

        # Calculate total outstanding
        try:
            from models import Invoice
            from utils import format_currency
            total_outstanding = sum(float(invoice.amount) for invoice in client.invoices if not invoice.is_paid)
            company_currency = company.currency if company else 'CAD'
            variables['{client_total_outstanding}'] = format_currency(total_outstanding, company_currency)
        except:
            variables['{client_total_outstanding}'] = format_currency(0, company.currency if company else 'CAD')

        # Replace in subject and content
        for var, value in variables.items():
            subject = subject.replace(var, str(value))
            content = content.replace(var, str(value))

        # Add signature
        if user_email_config.email_signature:
            content = content + "\n\n" + user_email_config.email_signature

        # Prepare attachments
        attachments = []

        # Collect invoices (parent + children if requested)
        invoices_for_attachments = list(client.invoices)

        if include_children and client.is_parent:
            current_app.logger.info(f"Including children invoices for client {client.name}")
            for child_client in client.child_clients:
                if child_client.company_id != company.id:
                    current_app.logger.warning(f"SECURITE: Enfant {child_client.id} ignoré - company_id différent")
                    continue
                current_app.logger.info(f"Adding invoices from child: {child_client.name}")
                invoices_for_attachments.extend(child_client.invoices)

        # PDF attachment
        if attach_pdf:
            try:
                from utils import generate_statement_pdf_reportlab
                import datetime as dt
                # Filter unpaid invoices and sort chronologically (oldest to newest)
                unpaid_invoices = [inv for inv in invoices_for_attachments if not inv.is_paid]
                # Sort with type normalization to handle both date and datetime objects
                def get_invoice_sort_key(inv):
                    # Normalize invoice_date to date object for consistent comparison
                    if inv.invoice_date:
                        inv_date = inv.invoice_date.date() if isinstance(inv.invoice_date, dt.datetime) else inv.invoice_date
                    else:
                        inv_date = dt.date(1900, 1, 1)
                    return (inv_date, str(inv.invoice_number or ''))
                unpaid_invoices.sort(key=get_invoice_sort_key)
                aged_balances = client.get_aged_balances(company.aging_calculation_method) if hasattr(client, 'get_aged_balances') else {}
                pdf_buffer = generate_statement_pdf_reportlab(client, unpaid_invoices, company, aged_balances, attachment_language)

                if pdf_buffer:
                    pdf_filename = 'statement_' + client.code_client + '.pdf' if attachment_language == 'en' else 'releve_' + client.code_client + '.pdf'
                    attachments.append({
                        'filename': pdf_filename,
                        'content': pdf_buffer.getvalue(),
                        'content_type': 'application/pdf'
                    })
                    current_app.logger.info(f"PDF generated with {len(unpaid_invoices)} invoices (include_children={include_children})")
            except Exception as e:
                current_app.logger.error(f"Error generating PDF: {str(e)}")

        # Excel attachment
        if attach_excel:
            try:
                excel_buffer = io.BytesIO()
                workbook = xlsxwriter.Workbook(excel_buffer)
                worksheet = workbook.add_worksheet('Factures')

                headers = ['Invoice #', 'Date', 'Amount', 'Status', 'Due Date'] if attachment_language == 'en' else ['N° Facture', 'Date', 'Montant', 'Statut', "Date d'échéance"]
                paid_status = 'Paid' if attachment_language == 'en' else 'Payée'
                unpaid_status = 'Unpaid' if attachment_language == 'en' else 'Impayée'

                header_format = workbook.add_format({'bold': True, 'bg_color': '#4472C4', 'font_color': 'white'})
                for col, header in enumerate(headers):
                    worksheet.write(0, col, header, header_format)

                row = 1
                for invoice in invoices_for_attachments:
                    worksheet.write(row, 0, invoice.invoice_number or '')
                    worksheet.write(row, 1, invoice.invoice_date.strftime('%Y-%m-%d') if invoice.invoice_date else '')
                    worksheet.write(row, 2, float(invoice.amount) if invoice.amount else 0)
                    worksheet.write(row, 3, paid_status if invoice.is_paid else unpaid_status)
                    worksheet.write(row, 4, invoice.due_date.strftime('%Y-%m-%d') if invoice.due_date else '')
                    row += 1

                workbook.close()
                excel_filename = 'invoices_' + client.code_client + '.xlsx' if attachment_language == 'en' else 'factures_' + client.code_client + '.xlsx'
                attachments.append({
                    'filename': excel_filename,
                    'content': excel_buffer.getvalue(),
                    'content_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                })
                current_app.logger.info(f"Excel generated with {len(invoices_for_attachments)} invoices (include_children={include_children})")
            except Exception as e:
                current_app.logger.error(f"Error generating Excel: {str(e)}")

        # External files
        external_files = request.files.getlist('external_files')
        if external_files:
            total_size = 0
            for file in external_files:
                if file and file.filename:
                    filename = file.filename
                    allowed_extensions = {'.pdf', '.xlsx', '.csv', '.doc', '.docx'}
                    file_ext = os.path.splitext(filename)[1].lower()

                    if file_ext in allowed_extensions:
                        file.seek(0)
                        file_content = file.read()
                        total_size += len(file_content)

                        if total_size > 20 * 1024 * 1024:
                            return jsonify({'error': 'Taille totale des fichiers > 20 MB'}), 400

                        if file_content:
                            content_types = {
                                '.pdf': 'application/pdf',
                                '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                '.csv': 'text/csv',
                                '.doc': 'application/msword',
                                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                            }
                            attachments.append({
                                'filename': filename,
                                'content': file_content,
                                'content_type': content_types.get(file_ext, 'application/octet-stream')
                            })

        # Copies de factures depuis le cache PDF temporaire (Redis partage entre workers)
        from utils.pdf_temp_cache import get_pdf
        invoice_pdf_ids_raw = request.form.getlist('invoice_pdf_ids')
        cached_pdf_invoice_ids = []
        missing_pdf_ids = []
        if invoice_pdf_ids_raw:
            for raw_id in invoice_pdf_ids_raw:
                try:
                    inv_id = int(raw_id)
                except (ValueError, TypeError):
                    missing_pdf_ids.append(str(raw_id))
                    continue
                entry = get_pdf(current_user.id, inv_id)
                if entry:
                    # SECURITE : verifier que le PDF appartient a l'entreprise active
                    if entry.get('company_id') != company.id:
                        current_app.logger.warning(
                            f'send_email_from_notes: TENTATIVE D\'ACCES INTER-ENTREPRISE '
                            f'user={current_user.id} invoice_id={inv_id} '
                            f'cache_company={entry.get("company_id")} current_company={company.id}'
                        )
                        missing_pdf_ids.append(str(inv_id))
                        continue
                    attachments.append({
                        'filename': entry['filename'],
                        'content': entry['bytes'],
                        'content_type': 'application/pdf'
                    })
                    cached_pdf_invoice_ids.append(inv_id)
                else:
                    current_app.logger.warning(
                        f'send_email_from_notes: cache PDF absent ou expire pour invoice_id={raw_id}'
                    )
                    missing_pdf_ids.append(str(raw_id))

        # BLOCAGE : si certaines PDF demandees sont absentes du cache, refuser l'envoi
        # plutot que d'envoyer le courriel sans pieces jointes (perte silencieuse)
        if missing_pdf_ids:
            return jsonify({
                'error': (
                    f"Certaines copies de factures ne sont plus disponibles "
                    f"(cache expire ou non telecharge). Veuillez recliquer sur "
                    f"\"Telecharger les copies de factures\" puis renvoyer le courriel. "
                    f"Factures concernees : {', '.join(missing_pdf_ids)}"
                )
            }), 400

        import re
        email_regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

        raw_to = to_emails_str.replace(',', ';')
        to_emails = [e.strip() for e in raw_to.split(';') if e.strip()]

        raw_cc = cc_emails_str.replace(',', ';')
        cc_emails = [e.strip() for e in raw_cc.split(';') if e.strip()]

        invalid_to = [e for e in to_emails if not email_regex.match(e)]
        invalid_cc = [e for e in cc_emails if not email_regex.match(e)]
        if invalid_to or invalid_cc:
            all_invalid = invalid_to + invalid_cc
            return jsonify({'error': f"Format de courriel invalide : {', '.join(all_invalid)}"}), 400

        if len(to_emails) > 10:
            return jsonify({'error': 'Maximum 10 destinataires autorisés dans le champ À'}), 400
        if len(cc_emails) > 10:
            return jsonify({'error': 'Maximum 10 destinataires autorisés dans le champ CC'}), 400

        # Send email
        success = False
        provider_used = None
        email_message_id = None
        email_conversation_id = None

        if user_email_config.is_outlook_connected():
            from microsoft_oauth import MicrosoftOAuthConnector
            oauth_connector = MicrosoftOAuthConnector()
            result = oauth_connector.send_email(
                access_token=user_email_config.outlook_oauth_access_token,
                to_emails=to_emails,
                subject=subject,
                body=content,
                cc_list=cc_emails if cc_emails else None,
                attachments=attachments if attachments else None,
                return_message_ids=True
            )

            # Extraire les résultats
            if isinstance(result, dict):
                success = result.get('success', False)
                email_message_id = result.get('message_id')
                email_conversation_id = result.get('conversation_id')
            else:
                success = result
            provider_used = 'outlook'

        elif user_email_config.is_gmail_connected():
            from gmail_smtp import GmailSMTPConnector
            primary_recipient = to_emails[0] if to_emails else None
            if not primary_recipient:
                return jsonify({'error': 'Aucun destinataire'}), 400

            gmail_connector = GmailSMTPConnector()
            result = gmail_connector.send_email(
                gmail_email=user_email_config.gmail_email,
                app_password=user_email_config.gmail_smtp_app_password,
                to_emails=[primary_recipient],
                subject=subject,
                body=content,
                attachments=attachments if attachments else None,
                return_message_ids=True
            )

            # Extraire les résultats
            if isinstance(result, dict):
                success = result.get('success', False)
                email_message_id = result.get('message_id')
            else:
                success = result
            provider_used = 'gmail'

        if success:
            # Get sender email based on provider
            if provider_used == 'gmail':
                sender_email = user_email_config.gmail_email if user_email_config and user_email_config.gmail_email else current_user.email
            else:  # outlook or default
                sender_email = user_email_config.outlook_email if user_email_config and user_email_config.outlook_email else current_user.email

            # Métadonnées des pièces jointes (noms + tailles uniquement, sans bytes)
            attachments_meta = None
            if attachments:
                attachments_meta = [
                    {
                        'filename': att['filename'],
                        'size': len(att.get('content', b'')) if isinstance(att.get('content'), (bytes, bytearray)) else 0
                    }
                    for att in attachments
                ]

            # Create note with email IDs
            note = CommunicationNote(
                client_id=client.id,
                user_id=current_user.id,
                company_id=company.id,
                note_type='email',
                note_text=f"Courriel envoyé: {subject}",
                note_date=date.today(),
                email_from=sender_email,
                email_to=to_emails_str,
                email_subject=subject,
                email_body=content,
                attachments=attachments_meta,
                outlook_message_id=email_message_id if provider_used == 'outlook' else None,
                gmail_message_id=email_message_id if provider_used == 'gmail' else None,
                conversation_id=email_conversation_id if provider_used == 'outlook' else None
            )
            db.session.add(note)
            db.session.commit()

            # Vider le cache PDF temporaire pour les factures envoyées
            from utils.pdf_temp_cache import delete_pdf
            for inv_id in cached_pdf_invoice_ids:
                delete_pdf(current_user.id, inv_id)

            return jsonify({'success': True, 'message': 'Email envoyé avec succès'})
        else:
            return jsonify({'error': "Échec d'envoi de l'email"}), 500

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error sending email from notes: {str(e)}")
        return jsonify({'error': 'Une erreur interne est survenue'}), 500
