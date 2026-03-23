"""
Views for email template management and sending
Extracted from views.py monolith - Phase 6 Refactoring
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user
from datetime import datetime, date
import os
from app import limiter
from utils.audit_service import log_action, log_email_action, AuditActions, EntityTypes

# Create email blueprint
email_bp = Blueprint('email', __name__, url_prefix='/emails')


def get_user_friendly_error_message(error, provider=None):
    """
    Convert technical error messages into user-friendly messages.
    Technical details are still logged on the server side.

    Args:
        error: Exception object or error string
        provider: Optional string indicating email provider ('outlook', 'gmail', None)

    Returns:
        str: User-friendly error message in French
    """
    error_str = str(error).lower()

    # Check for authentication/token errors (401, token issues)
    if any(keyword in error_str for keyword in ['401', 'access token', 'refresh token', 'unauthorized', 'token refresh failed']):
        if provider == 'gmail' or 'gmail' in error_str:
            return "Votre session Gmail a expiré. Veuillez vous reconnecter."
        return "Votre session Outlook a expiré. Veuillez vous reconnecter."

    # Check for timeout errors
    if 'timeout' in error_str or 'timed out' in error_str:
        return "Le serveur ne répond pas. Veuillez réessayer dans quelques instants."

    # Check for connection errors
    if any(keyword in error_str for keyword in ['connection', 'connexion', 'network', 'réseau']):
        return "Problème de connexion. Vérifiez votre connexion Internet et réessayez."

    # Check for Gmail-specific errors (be specific with SMTP context)
    if ('gmail' in error_str and 'smtp' in error_str) or ('gmail' in error_str and provider == 'gmail'):
        return "Erreur d'envoi Gmail. Vérifiez votre mot de passe d'application."

    # Check for Outlook/Microsoft Graph errors
    if any(keyword in error_str for keyword in ['graph', 'microsoft', 'outlook']) or provider == 'outlook':
        return "Erreur d'envoi Outlook. Vérifiez votre configuration email."

    # Check for generic SMTP errors (when not specifically Gmail or Outlook)
    if 'smtp' in error_str and provider != 'gmail':
        return "Erreur d'envoi email. Vérifiez votre configuration serveur."

    # Check for permission/configuration errors
    if any(keyword in error_str for keyword in ['not configured', 'no refresh token', 'no access token', 'non configuré']):
        return "Configuration email manquante. Veuillez configurer votre compte email."

    # Check for attachment errors
    if 'attachment' in error_str or 'fichier' in error_str or 'pièce jointe' in error_str:
        return "Erreur avec les pièces jointes. Vérifiez les fichiers sélectionnés."

    # Generic error message for all other cases
    return "Une erreur est survenue lors de l'envoi du courriel. Veuillez réessayer."

@email_bp.route('/templates')
@login_required
def templates():
    """List email templates"""
    from app import db
    from models import EmailTemplate, User
    from sqlalchemy.orm import joinedload

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions - NORMALISATION FRANÇAISE
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get templates with filtering based on user role and sharing settings
    # Eager load creator relationship to avoid N+1 queries
    if user_role in ['super_admin', 'admin']:
        # Admins voient tous les templates
        templates = db.session.query(EmailTemplate).options(
            joinedload(EmailTemplate.creator)
        ).filter_by(
            company_id=company.id
        ).order_by(EmailTemplate.name).all()
    else:
        # Employés voient seulement templates partagés et leurs propres templates
        templates = db.session.query(EmailTemplate).options(
            joinedload(EmailTemplate.creator)
        ).filter(
            EmailTemplate.company_id == company.id,
            db.or_(
                EmailTemplate.is_shared == True,  # Templates partagés
                EmailTemplate.created_by == current_user.id  # Templates personnels
            )
        ).order_by(EmailTemplate.name).all()

    return render_template('emails/templates.html', templates=templates, company=company)


@email_bp.route('/templates/new', methods=['GET', 'POST'])
@login_required
def new_template():
    """Create new email template"""
    from app import db
    from models import EmailTemplate
    from forms import EmailTemplateForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions - Admins et employés peuvent créer des templates
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        flash('Accès refusé. Seuls les administrateurs et employés peuvent créer des templates.', 'error')
        return redirect(url_for('email.templates'))

    # Check if this is a duplication request
    duplicate_id = request.args.get('duplicate', type=int)
    original_template = None
    form_data = {}

    if duplicate_id:
        # Get original template for duplication
        original_template = db.session.query(EmailTemplate).filter_by(
            id=duplicate_id,
            company_id=company.id
        ).first()

        if original_template:
            # Check if user can access this template
            can_view = (
                user_role in ['super_admin', 'admin'] or  # Admins voient tout
                (original_template.created_by == current_user.id) or  # Créateur peut dupliquer
                (original_template.is_shared)  # Template partagé visible
            )

            if can_view and request.method == 'GET':
                form_data = {
                    'name': f"Copie de {original_template.name}",
                    'subject': original_template.subject,
                    'content': original_template.content,
                    'is_active': True,
                    'is_shared': False,
                    'is_editable_by_team': False
                }
                current_app.logger.info(f"Duplication template {duplicate_id} - {original_template.name}")
        else:
            flash('Template à dupliquer non trouvé.', 'error')

    # Create form with potential pre-population
    if form_data:
        form = EmailTemplateForm(data=form_data, user_role=user_role)
    else:
        form = EmailTemplateForm(user_role=user_role)

    if form.validate_on_submit():
        template = EmailTemplate(
            name=form.name.data,
            subject=form.subject.data,
            content=form.content.data,  # Corrigé : utilise content (nom correct dans DB)
            company_id=company.id,
            created_by=current_user.id,
            is_active=form.is_active.data,
            is_shared=form.is_shared.data if hasattr(form, 'is_shared') and form.is_shared is not None else False,
            is_editable_by_team=form.is_editable_by_team.data if hasattr(form, 'is_editable_by_team') and form.is_editable_by_team is not None else False
        )

        # Add reference to original template if this is a duplication
        if original_template:
            template.original_template_id = original_template.id

        try:
            db.session.add(template)
            db.session.commit()

            action = AuditActions.EMAIL_TEMPLATE_CREATED
            log_email_action(
                action,
                template_id=template.id,
                template_name=template.name
            )

            if original_template:
                flash('Template dupliqué avec succès.', 'success')
            else:
                flash('Template créé avec succès.', 'success')
            return redirect(url_for('email.templates'))
        except Exception as e:
            db.session.rollback()
            flash('Erreur lors de la création du template.', 'error')

    # Set appropriate title
    title = 'Dupliquer template' if original_template else 'Nouveau template'

    return render_template('emails/template_form.html',
                         form=form,
                         company=company,
                         title=title,
                         original_template=original_template)


@email_bp.route('/templates/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_template(id):
    """Edit email template"""
    from app import db
    from models import EmailTemplate
    from forms import EmailTemplateForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    template = db.session.query(EmailTemplate).filter_by(id=id, company_id=company.id).first()
    if not template:
        flash('Template non trouvé.', 'error')
        return redirect(url_for('email.templates'))

    # Check permissions - plus flexible pour les templates partagés
    user_role = current_user.get_role_in_company(company.id)
    can_edit = (
        user_role in ['super_admin', 'admin'] or  # Admins peuvent toujours modifier
        (template.created_by == current_user.id) or  # Créateur peut modifier
        (template.is_shared and template.is_editable_by_team)  # Template partagé modifiable par équipe
    )

    if not can_edit:
        flash('Accès refusé. Vous n\'avez pas les permissions pour modifier ce template.', 'error')
        return redirect(url_for('email.templates'))

    form = EmailTemplateForm(obj=template, user_role=user_role)

    if form.validate_on_submit():
        template.name = form.name.data
        template.subject = form.subject.data
        template.content = form.content.data  # Corrigé : content au lieu de body
        template.is_active = form.is_active.data
        if hasattr(form, 'is_shared') and form.is_shared is not None:
            template.is_shared = form.is_shared.data
        if hasattr(form, 'is_editable_by_team') and form.is_editable_by_team is not None:
            template.is_editable_by_team = form.is_editable_by_team.data
        template.updated_at = datetime.utcnow()

        try:
            db.session.commit()
            log_email_action(
                AuditActions.EMAIL_TEMPLATE_UPDATED,
                template_id=template.id,
                template_name=template.name
            )
            flash('Template mis à jour avec succès.', 'success')
            return redirect(url_for('email.templates'))
        except Exception as e:
            db.session.rollback()
            flash('Erreur lors de la mise à jour du template.', 'error')

    return render_template('emails/template_form.html', form=form, template=template, company=company, title='Modifier template')


@email_bp.route('/templates/<int:id>/delete', methods=['POST'])
@login_required
def delete_template(id):
    """Delete email template"""
    from app import db
    from models import EmailTemplate

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    template = db.session.query(EmailTemplate).filter_by(id=id, company_id=company.id).first()
    if not template:
        flash('Template non trouvé.', 'error')
        return redirect(url_for('email.templates'))

    # Check permissions using model method
    if not template.can_delete(current_user):
        flash('Accès refusé. Seul l\'auteur ou un super administrateur peut supprimer ce template.', 'error')
        return redirect(url_for('email.templates'))

    try:
        template_name = template.name
        template_id = template.id
        db.session.delete(template)
        db.session.commit()
        log_email_action(
            AuditActions.EMAIL_TEMPLATE_DELETED,
            template_id=template_id,
            template_name=template_name
        )
        flash('Template supprimé avec succès.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Erreur lors de la suppression du template.', 'error')

    return redirect(url_for('email.templates'))


@email_bp.route('/templates/<int:id>/duplicate', methods=['GET', 'POST'])
@login_required
def duplicate_template(id):
    """Duplicate an email template"""
    from app import db
    from models import EmailTemplate
    from forms import EmailTemplateForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Get original template
    original_template = db.session.query(EmailTemplate).filter_by(id=id, company_id=company.id).first()
    if not original_template:
        flash('Template non trouvé.', 'error')
        return redirect(url_for('email.templates'))

    # Check permissions - anyone who can see the template can duplicate it
    user_role = current_user.get_role_in_company(company.id)
    can_view = (
        user_role in ['super_admin', 'admin'] or  # Admins voient tout
        (original_template.created_by == current_user.id) or  # Créateur peut dupliquer
        (original_template.is_shared)  # Template partagé visible
    )

    if not can_view:
        flash('Accès refusé.', 'error')
        return redirect(url_for('email.templates'))

    # Prepare form data for pre-population
    form_data = {}
    if request.method == 'GET':
        form_data = {
            'name': f"Copie de {original_template.name}",
            'subject': original_template.subject,
            'content': original_template.content,
            'is_active': True,
        }

        # Add sharing fields only if user has admin rights
        if user_role in ['super_admin', 'admin']:
            form_data['is_shared'] = False
            form_data['is_editable_by_team'] = False

        current_app.logger.info(f"Template duplication - Original: {original_template.name}, Pre-populating with: {form_data}")

    # Create form with pre-populated data
    if form_data:
        form = EmailTemplateForm(data=form_data, user_role=user_role)
    else:
        form = EmailTemplateForm(user_role=user_role)

    if form.validate_on_submit():
        # Create new template
        new_template = EmailTemplate(
            name=form.name.data,
            subject=form.subject.data,
            content=form.content.data,
            company_id=company.id,
            created_by=current_user.id,
            is_active=form.is_active.data,
            is_shared=form.is_shared.data if hasattr(form, 'is_shared') and form.is_shared is not None else False,
            is_editable_by_team=form.is_editable_by_team.data if hasattr(form, 'is_editable_by_team') and form.is_editable_by_team is not None else False,
            original_template_id=original_template.id  # Link to original
        )

        try:
            db.session.add(new_template)
            db.session.commit()
            flash('Template dupliqué avec succès.', 'success')
            return redirect(url_for('email.templates'))
        except Exception as e:
            db.session.rollback()
            flash('Erreur lors de la duplication du template.', 'error')
            current_app.logger.error(f"Error duplicating template: {str(e)}")

    return render_template('emails/template_form.html',
                         form=form,
                         company=company,
                         title='Dupliquer template',
                         original_template=original_template)


# API endpoint for getting template content
@email_bp.route('/api/template/<int:template_id>')
@limiter.exempt
@login_required
def api_get_email_template(template_id):
    """API endpoint to get email template content"""
    from app import db
    from models import EmailTemplate

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    template = db.session.query(EmailTemplate).filter_by(id=template_id, company_id=company.id).first()
    if not template:
        return jsonify({'error': 'Template non trouvé'}), 404

    return jsonify({
        'subject': template.subject,
        'body': template.content
    })


@email_bp.route('/api/templates/list')
@limiter.exempt
@login_required
def api_list_email_templates():
    """API endpoint to list all accessible email templates"""
    from app import db
    from models import EmailTemplate

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    user_role = current_user.get_role_in_company(company.id)
    if user_role in ['super_admin', 'admin']:
        templates = db.session.query(EmailTemplate).filter_by(
            company_id=company.id,
            is_active=True
        ).order_by(EmailTemplate.name).all()
    else:
        templates = db.session.query(EmailTemplate).filter(
            EmailTemplate.company_id == company.id,
            EmailTemplate.is_active == True,
            db.or_(
                EmailTemplate.is_shared == True,
                EmailTemplate.created_by == current_user.id
            )
        ).order_by(EmailTemplate.name).all()

    return jsonify({
        'templates': [{'id': t.id, 'name': t.name, 'subject': t.subject, 'content': t.content} for t in templates]
    })


@email_bp.route('/send_ajax/<int:client_id>', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def send_email_ajax(client_id):
    """
    Fonction vérifiée par MDF le 30/01/2026.
    NOTE IMPORTANTE POUR REPLIT : Cette fonction a été vérifiée.
    Aucune modification ne doit être effectuée sans approbation explicite.
    """
    """Send email to client via AJAX - returns JSON response"""
    from app import db
    from models import Client, EmailTemplate, CommunicationNote, EmailConfiguration
    from forms import SendEmailForm
    from flask import session
    import xlsxwriter
    import io

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'success': False, 'error': 'Aucune entreprise sélectionnée'}), 400

    # Verify client belongs to company (SECURITY)
    client = db.session.query(Client).filter_by(id=client_id, company_id=company.id).first()
    if not client:
        return jsonify({'success': False, 'error': 'Client non trouvé'}), 404

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'success': False, 'error': 'Accès refusé'}), 403

    form = SendEmailForm(company_id=company.id, user_id=current_user.id)

    if not form.validate_on_submit():
        # Return user-friendly validation error
        return jsonify({'success': False, 'error': 'Veuillez vérifier les champs du formulaire et réessayer.'}), 400

    try:
        # Get user's email configuration
        company_id = session.get('selected_company_id')
        user_email_config = EmailConfiguration.query.filter_by(
            user_id=current_user.id,
            company_id=company_id
        ).first()

        if not user_email_config or (not user_email_config.is_outlook_connected() and not user_email_config.is_gmail_connected()):
            return jsonify({'success': False, 'error': 'Aucune connexion email trouvée. Veuillez configurer Outlook ou Gmail.'}), 400

        # Refresh token if needed
        if user_email_config.is_outlook_connected() and user_email_config.needs_token_refresh():
            try:
                from email_fallback import refresh_user_oauth_token
                refresh_user_oauth_token(user_email_config)
            except Exception as e:
                current_app.logger.error(f"Token refresh failed: {str(e)}")
                friendly_error = get_user_friendly_error_message(e, provider='outlook')
                return jsonify({'success': False, 'error': friendly_error}), 400

        # Replace variables in subject and content
        subject = form.subject.data or ''
        content = form.content.data or ''

        variables = {
            '{client_name}': client.name or '',
            '{client_code}': client.code_client or '',
            '{client_email}': client.email or '',
            '{client_phone}': client.phone or '',
            '{client_payment_terms}': client.payment_terms or '',
            '{company_name}': company.name or '',
            '{company_email}': company.email or '',
            '{user_name}': f"{current_user.first_name} {current_user.last_name}".strip(),
            '{today_date}': datetime.now().strftime('%d/%m/%Y')
        }

        try:
            from utils import format_currency
            total_outstanding = sum(float(invoice.amount) for invoice in client.invoices if not invoice.is_paid)
            currency_code = company.currency if company and company.currency else 'CAD'
            variables['{client_total_outstanding}'] = format_currency(total_outstanding, currency_code)
        except Exception:
            from utils import format_currency
            currency_code = company.currency if company and company.currency else 'CAD'
            variables['{client_total_outstanding}'] = format_currency(0, currency_code)

        for var, value in variables.items():
            subject = subject.replace(var, str(value))
            content = content.replace(var, str(value))

        # Add signature
        if user_email_config.email_signature:
            content = content + "\n\n" + user_email_config.email_signature if content.strip() else user_email_config.email_signature

        # Collect invoices (parent + children if requested)
        include_children = form.include_children.data if hasattr(form, 'include_children') else False
        all_invoices = list(client.invoices)
        if include_children and client.is_parent:
            for child_client in client.child_clients:
                all_invoices.extend(child_client.invoices)

        # Prepare attachments
        attachments = []

        if form.attach_pdf.data:
            try:
                from utils import generate_statement_pdf_reportlab
                from models import Company
                import datetime as dt
                company_obj = Company.query.get(company_id)
                # Filter unpaid invoices and sort chronologically (oldest to newest)
                unpaid_invoices = [inv for inv in all_invoices if not inv.is_paid]
                # Sort with type normalization to handle both date and datetime objects
                def get_invoice_sort_key(inv):
                    # Normalize invoice_date to date object for consistent comparison
                    if inv.invoice_date:
                        inv_date = inv.invoice_date.date() if isinstance(inv.invoice_date, dt.datetime) else inv.invoice_date
                    else:
                        inv_date = dt.date(1900, 1, 1)
                    return (inv_date, str(inv.invoice_number or ''))
                unpaid_invoices.sort(key=get_invoice_sort_key)
                aged_balances = client.get_aged_balances(company_obj.aging_calculation_method) if hasattr(client, 'get_aged_balances') else {}
                report_language = form.attachment_language.data or 'fr'
                pdf_buffer = generate_statement_pdf_reportlab(client, unpaid_invoices, company_obj, aged_balances, report_language)

                if pdf_buffer:
                    pdf_filename = f'statement_{client.code_client}.pdf' if report_language == 'en' else f'releve_{client.code_client}.pdf'
                    attachments.append({
                        'filename': pdf_filename,
                        'content': pdf_buffer.getvalue(),
                        'content_type': 'application/pdf'
                    })
            except Exception as e:
                current_app.logger.error(f"Error generating PDF: {str(e)}")

        if form.attach_excel.data:
            try:
                excel_buffer = io.BytesIO()
                workbook = xlsxwriter.Workbook(excel_buffer)
                worksheet = workbook.add_worksheet('Factures')

                report_language = form.attachment_language.data or 'fr'
                headers = ['Invoice #', 'Date', 'Amount', 'Status', 'Due Date'] if report_language == 'en' else ['N° Facture', 'Date', 'Montant', 'Statut', 'Date d\'échéance']
                paid_status = 'Paid' if report_language == 'en' else 'Payée'
                unpaid_status = 'Unpaid' if report_language == 'en' else 'Impayée'

                header_format = workbook.add_format({'bold': True, 'bg_color': '#4472C4', 'font_color': 'white'})
                for col, header in enumerate(headers):
                    worksheet.write(0, col, header, header_format)

                for row, invoice in enumerate(all_invoices, start=1):
                    worksheet.write(row, 0, invoice.invoice_number or '')
                    worksheet.write(row, 1, invoice.invoice_date.strftime('%Y-%m-%d') if invoice.invoice_date else '')
                    worksheet.write(row, 2, float(invoice.amount) if invoice.amount else 0)
                    worksheet.write(row, 3, paid_status if invoice.is_paid else unpaid_status)
                    worksheet.write(row, 4, invoice.due_date.strftime('%Y-%m-%d') if invoice.due_date else '')

                workbook.close()
                excel_filename = f'invoices_{client.code_client}.xlsx' if report_language == 'en' else f'factures_{client.code_client}.xlsx'
                attachments.append({
                    'filename': excel_filename,
                    'content': excel_buffer.getvalue(),
                    'content_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                })
            except Exception as e:
                current_app.logger.error(f"Error generating Excel: {str(e)}")

        # Process external files
        external_files = request.files.getlist('external_files')
        total_size = 0
        MAX_SIZE = 20 * 1024 * 1024  # 20 MB

        for file in external_files:
            if file and file.filename:
                allowed_extensions = {'.pdf', '.xlsx', '.csv', '.doc', '.docx'}
                file_ext = os.path.splitext(file.filename)[1].lower()

                if file_ext in allowed_extensions:
                    file.seek(0)
                    file_content = file.read()
                    total_size += len(file_content)

                    if total_size > MAX_SIZE:
                        return jsonify({'success': False, 'error': 'Les pièces jointes sont trop volumineuses. Veuillez réduire la taille des fichiers.'}), 400

                    if file_content:
                        content_types = {
                            '.pdf': 'application/pdf',
                            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            '.csv': 'text/csv',
                            '.doc': 'application/msword',
                            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                        }
                        attachments.append({
                            'filename': file.filename,
                            'content': file_content,
                            'content_type': content_types.get(file_ext, 'application/octet-stream')
                        })

        # Copies de factures depuis le cache PDF temporaire
        invoice_pdf_ids = request.form.getlist('invoice_pdf_ids')
        cached_pdf_invoice_ids = []
        if invoice_pdf_ids:
            from datetime import datetime as _dt
            now = _dt.utcnow()
            for raw_id in invoice_pdf_ids:
                try:
                    inv_id = int(raw_id)
                except (ValueError, TypeError):
                    continue
                cache_key = (current_user.id, inv_id)
                entry = current_app.pdf_temp_cache.get(cache_key)
                if entry and entry['expires'] > now:
                    # SÉCURITÉ : vérifier que le PDF appartient à l'entreprise active
                    if entry.get('company_id') != company.id:
                        current_app.logger.warning(
                            f'send_email_ajax: TENTATIVE D\'ACCÈS INTER-ENTREPRISE '
                            f'user={current_user.id} invoice_id={inv_id} '
                            f'cache_company={entry.get("company_id")} current_company={company.id}'
                        )
                        continue
                    attachments.append({
                        'filename': entry['filename'],
                        'content': entry['bytes'],
                        'content_type': 'application/pdf'
                    })
                    cached_pdf_invoice_ids.append(inv_id)
                else:
                    current_app.logger.warning(
                        f'send_email_ajax: cache PDF absent ou expiré pour invoice_id={raw_id}'
                    )

        import re
        email_regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

        raw_to = (form.to_emails.data or '').replace(',', ';')
        to_emails = [email.strip() for email in raw_to.split(';') if email.strip()]

        raw_cc = (form.cc_emails.data or '').replace(',', ';')
        cc_emails = [email.strip() for email in raw_cc.split(';') if email.strip()]

        invalid_to = [e for e in to_emails if not email_regex.match(e)]
        invalid_cc = [e for e in cc_emails if not email_regex.match(e)]
        if invalid_to or invalid_cc:
            all_invalid = invalid_to + invalid_cc
            return jsonify({'success': False, 'error': f"Format de courriel invalide : {', '.join(all_invalid)}"}), 400

        if len(to_emails) > 10:
            return jsonify({'success': False, 'error': 'Maximum 10 destinataires autorisés dans le champ À'}), 400
        if len(cc_emails) > 10:
            return jsonify({'success': False, 'error': 'Maximum 10 destinataires autorisés dans le champ CC'}), 400

        # Send email
        success = False
        provider_used = None
        email_message_id = None
        email_conversation_id = None

        # Get email options (NEW)
        high_importance = form.high_importance.data if hasattr(form, 'high_importance') else False
        read_receipt = form.read_receipt.data if hasattr(form, 'read_receipt') else False
        delivery_receipt = form.delivery_receipt.data if hasattr(form, 'delivery_receipt') else False

        if user_email_config.is_outlook_connected():
            provider_used = 'outlook'
            try:
                from microsoft_oauth import MicrosoftOAuthConnector
                oauth_connector = MicrosoftOAuthConnector()

                result = oauth_connector.send_email(
                    access_token=user_email_config.outlook_oauth_access_token,
                    to_emails=to_emails,
                    subject=subject,
                    body=content,
                    cc_list=cc_emails if cc_emails else None,
                    attachments=attachments if attachments else None,
                    return_message_ids=True,
                    high_importance=high_importance,
                    read_receipt=read_receipt,
                    delivery_receipt=delivery_receipt
                )

                if isinstance(result, dict):
                    success = result.get('success', False)
                    # Handle soft failures (no exception raised but success=False)
                    if not success:
                        error_msg = result.get('error', 'Outlook send failed')
                        current_app.logger.error(f"Outlook send soft failure: {error_msg}")
                        friendly_error = get_user_friendly_error_message(error_msg, provider='outlook')
                        return jsonify({'success': False, 'error': friendly_error}), 500
                    email_message_id = result.get('message_id')
                    email_conversation_id = result.get('conversation_id')
                else:
                    success = result
            except Exception as e:
                current_app.logger.error(f"Outlook send failed: {str(e)}")
                friendly_error = get_user_friendly_error_message(e, provider='outlook')
                return jsonify({'success': False, 'error': friendly_error}), 500

        elif user_email_config.is_gmail_connected():
            provider_used = 'gmail'
            try:
                from gmail_smtp import GmailSMTPConnector
                gmail_connector = GmailSMTPConnector()

                result = gmail_connector.send_email(
                    gmail_email=user_email_config.gmail_email,
                    app_password=user_email_config.gmail_smtp_app_password,
                    to_emails=to_emails,
                    subject=subject,
                    body=content,
                    cc_list=cc_emails if cc_emails else None,
                    attachments=attachments if attachments else None,
                    return_message_ids=True,
                    high_importance=high_importance,
                    read_receipt=read_receipt,
                    delivery_receipt=delivery_receipt
                )

                if isinstance(result, dict):
                    success = result.get('success', False)
                    # Handle soft failures (no exception raised but success=False)
                    if not success:
                        error_msg = result.get('error', 'Gmail send failed')
                        current_app.logger.error(f"Gmail send soft failure: {error_msg}")
                        friendly_error = get_user_friendly_error_message(error_msg, provider='gmail')
                        return jsonify({'success': False, 'error': friendly_error}), 500
                    email_message_id = result.get('message_id')
                else:
                    success = result
            except Exception as e:
                current_app.logger.error(f"Gmail send failed: {str(e)}")
                friendly_error = get_user_friendly_error_message(e, provider='gmail')
                return jsonify({'success': False, 'error': friendly_error}), 500
        else:
            return jsonify({'success': False, 'error': 'Aucune connexion email configurée'}), 400

        if success:
            # Create communication note
            attachments_data = None
            if attachments:
                attachments_data = []
                for att in attachments:
                    content_data = att.get('content', b'')
                    size_bytes = len(content_data) if isinstance(content_data, (bytes, bytearray)) else 0
                    attachments_data.append({'filename': att['filename'], 'size': size_bytes})

            sender_email = user_email_config.gmail_email if provider_used == 'gmail' else user_email_config.outlook_email
            if not sender_email:
                sender_email = current_user.email

            # Parse reminder date if provided
            reminder_date_value = None
            reminder_date_str = request.form.get('reminder_date')
            if reminder_date_str and reminder_date_str.strip():
                try:
                    # Parse date string (format: YYYY-MM-DD from HTML5 date input)
                    parsed_date = datetime.strptime(reminder_date_str, '%Y-%m-%d')

                    # Validate date is not in the past
                    if parsed_date.date() < date.today():
                        return jsonify({'success': False, 'error': 'La date de rappel ne peut pas être dans le passé.'}), 400

                    reminder_date_value = parsed_date
                except ValueError:
                    current_app.logger.warning(f"Invalid reminder_date format: {reminder_date_str}")
                    return jsonify({'success': False, 'error': 'Format de date invalide pour le rappel.'}), 400

            note = CommunicationNote(
                client_id=client.id,
                user_id=current_user.id,
                company_id=company_id,
                note_type='email',
                note_text=f'Email envoyé par {sender_email}',
                email_from=sender_email,
                email_to=", ".join(to_emails),
                email_subject=subject,
                email_body=content,
                attachments=attachments_data,
                outlook_message_id=email_message_id if provider_used == 'outlook' else None,
                gmail_message_id=email_message_id if provider_used == 'gmail' else None,
                conversation_id=email_conversation_id if provider_used == 'outlook' else None,
                email_direction='sent',
                is_conversation_active=True,
                reminder_date=reminder_date_value
            )

            db.session.add(note)
            db.session.commit()

            log_email_action(
                AuditActions.EMAIL_SENT,
                recipient=", ".join(to_emails) if to_emails else None,
                subject=subject,
                details={
                    'client_id': client.id,
                    'client_name': client.name,
                    'provider': provider_used
                }
            )

            # Vider le cache PDF temporaire pour les factures envoyées
            for inv_id in cached_pdf_invoice_ids:
                current_app.pdf_temp_cache.pop((current_user.id, inv_id), None)

            return jsonify({'success': True, 'message': 'Courriel envoyé avec succès'})
        else:
            # Email sending failed but didn't throw exception
            current_app.logger.error(f"Email send failed for provider {provider_used}")
            friendly_error = get_user_friendly_error_message("Email send failed", provider=provider_used)
            return jsonify({'success': False, 'error': friendly_error}), 500

    except Exception as e:
        db.session.rollback()
        # Log technical error details for debugging
        current_app.logger.error(f"Error in send_email_ajax: {str(e)}")
        # Return user-friendly error message to client
        # Use provider_used if it was set, otherwise try to infer
        provider_hint = None
        if 'provider_used' in locals() and provider_used:
            provider_hint = provider_used
        elif 'user_email_config' in locals():
            if user_email_config.is_outlook_connected():
                provider_hint = 'outlook'
            elif user_email_config.is_gmail_connected():
                provider_hint = 'gmail'
        friendly_error = get_user_friendly_error_message(e, provider=provider_hint)
        return jsonify({'success': False, 'error': friendly_error}), 500


@email_bp.route('/api/sync/client/<int:client_id>', methods=['POST'])
@limiter.exempt
@login_required
def sync_client_emails(client_id):
    """
    Synchronise les réponses/transferts des conversations email d'un client.
    Récupère les nouveaux messages depuis Outlook et les ajoute comme notes.
    """
    from app import db
    from models import Client, CommunicationNote, EmailConfiguration

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'success': False, 'error': 'Aucune entreprise sélectionnée'}), 400

    client = db.session.query(Client).filter_by(id=client_id, company_id=company.id).first()
    if not client:
        return jsonify({'success': False, 'error': 'Client non trouvé'}), 404

    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'success': False, 'error': 'Accès refusé'}), 403

    email_config = EmailConfiguration.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()

    if not email_config or not email_config.is_outlook_connected():
        return jsonify({
            'success': False,
            'error': 'Connexion Outlook requise pour synchroniser les courriels.'
        }), 400

    try:
        from outlook_email_sync import get_sync_service_for_user

        sync_service, user_email = get_sync_service_for_user(current_user.id, company.id)

        if not sync_service:
            return jsonify({
                'success': False,
                'error': 'Impossible d\'initialiser le service de synchronisation. Veuillez reconnecter Outlook.'
            }), 400

        conversations = db.session.query(
            CommunicationNote.conversation_id
        ).filter(
            CommunicationNote.client_id == client_id,
            CommunicationNote.company_id == company.id,
            CommunicationNote.conversation_id.isnot(None),
            CommunicationNote.note_type == 'email'
        ).distinct().all()

        total_stats = {'new_notes': 0, 'skipped': 0, 'errors': 0, 'conversations': 0, 'replies_discovered': 0, 'replies_synced': 0}
        debug_info = []

        for (conv_id,) in conversations:
            try:
                stats = sync_service.sync_conversation_to_notes(
                    conversation_id=conv_id,
                    client_id=client_id,
                    company_id=company.id,
                    user_id=current_user.id,
                    user_email=user_email
                )

                total_stats['new_notes'] += stats['new_notes']
                total_stats['skipped'] += stats['skipped']
                total_stats['errors'] += stats['errors']
                total_stats['conversations'] += 1

                debug_info.append({
                    'conversation_id': conv_id,
                    'messages_found': stats.get('messages_found', 0),
                    'new_notes': stats['new_notes'],
                    'graph_url': stats.get('graph_url', 'N/A'),
                    'method_used': stats.get('method_used', 'unknown'),
                    'error': stats.get('error_detail')
                })

            except Exception as e:
                current_app.logger.error(f"Erreur sync conversation {conv_id[:20] if conv_id else 'N/A'}: {str(e)}")
                total_stats['errors'] += 1
                debug_info.append({
                    'conversation_id': conv_id,
                    'error': 'Erreur de synchronisation'
                })

        discovery_warning = None
        try:
            discovery_result = sync_service.sync_replies_with_changed_subject(
                client_id=client_id,
                company_id=company.id,
                user_id=current_user.id,
                user_email=user_email
            )

            replies_discovered = discovery_result.get('discovered', 0)
            replies_synced = discovery_result.get('synced', 0)

            total_stats['replies_discovered'] += replies_discovered
            total_stats['replies_synced'] += replies_synced
            total_stats['new_notes'] += replies_synced

            if replies_synced > 0:
                current_app.logger.info(
                    f"Découvert {replies_synced} réponses avec sujet modifié pour client {client_id}"
                )
                debug_info.append({
                    'phase': 'discovery',
                    'replies_discovered': replies_discovered,
                    'replies_synced': replies_synced
                })

        except Exception as discovery_error:
            current_app.logger.warning(f"Erreur découverte réponses: {str(discovery_error)}")
            discovery_warning = str(discovery_error)

        if total_stats['new_notes'] > 0:
            message = f"{total_stats['new_notes']} nouveau(x) message(s) synchronisé(s)"
            if total_stats.get('replies_synced', 0) > 0:
                message += f" (dont {total_stats['replies_synced']} avec sujet modifié)"
        elif total_stats['conversations'] > 0:
            message = "Aucun nouveau message trouvé"
        else:
            message = "Aucune conversation à synchroniser pour ce client"

        response = {
            'success': True,
            'message': message,
            'synced_count': total_stats['new_notes'],
            'conversations_processed': total_stats['conversations'],
            'stats': total_stats,
            'debug': debug_info
        }

        if discovery_warning:
            response['discovery_warning'] = discovery_warning

        return jsonify(response)

    except Exception as e:
        current_app.logger.error(f"Erreur sync client {client_id}: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Une erreur est survenue lors de la synchronisation.',
            'debug_error': str(e)
        }), 500


@email_bp.route('/api/sync/conversation/<path:conversation_id>', methods=['POST'])
@limiter.exempt
@login_required
def sync_single_conversation(conversation_id):
    """
    Synchronise une conversation spécifique.
    """
    from app import db
    from models import CommunicationNote, EmailConfiguration

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'success': False, 'error': 'Aucune entreprise sélectionnée'}), 400

    original_note = db.session.query(CommunicationNote).filter(
        CommunicationNote.conversation_id == conversation_id,
        CommunicationNote.company_id == company.id
    ).first()

    if not original_note:
        return jsonify({'success': False, 'error': 'Conversation non trouvée'}), 404

    email_config = EmailConfiguration.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()

    if not email_config or not email_config.is_outlook_connected():
        return jsonify({
            'success': False,
            'error': 'Connexion Outlook requise pour synchroniser les courriels.'
        }), 400

    try:
        from outlook_email_sync import get_sync_service_for_user

        sync_service, user_email = get_sync_service_for_user(current_user.id, company.id)

        if not sync_service:
            return jsonify({
                'success': False,
                'error': 'Impossible d\'initialiser le service de synchronisation.'
            }), 400

        stats = sync_service.sync_conversation_to_notes(
            conversation_id=conversation_id,
            client_id=original_note.client_id,
            company_id=company.id,
            user_id=current_user.id,
            user_email=user_email
        )

        if stats['new_notes'] > 0:
            message = f"{stats['new_notes']} nouveau(x) message(s) trouvé(s)"
        else:
            message = "Aucun nouveau message dans cette conversation"

        return jsonify({
            'success': True,
            'message': message,
            'stats': stats
        })

    except Exception as e:
        current_app.logger.error(f"Erreur sync conversation: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Une erreur est survenue lors de la synchronisation.'
        }), 500

