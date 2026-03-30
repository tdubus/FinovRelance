"""
Gestion des factures - Routes pour suppression individuelle et multiple
"""

from flask import Blueprint, request, redirect, url_for, flash, send_file, jsonify, current_app
from flask_login import login_required, current_user
from urllib.parse import urlparse
from io import BytesIO
from datetime import datetime, timedelta
from app import db
from models import Invoice, AccountingConnection
from utils.audit_service import log_action, AuditActions, EntityTypes


def safe_redirect(referrer, fallback_url):
    """Safely redirect to referrer only if it's from the same host"""
    if referrer:
        parsed = urlparse(referrer)
        # Only allow relative URLs (no netloc) or same-host URLs
        if not parsed.netloc or parsed.netloc == request.host:
            return redirect(referrer)
    return redirect(fallback_url)

invoice_bp = Blueprint('invoice', __name__, url_prefix='/invoices')


@invoice_bp.route('/delete/<int:invoice_id>', methods=['POST'])
@login_required
def delete_invoice(invoice_id):
    """Supprime une facture individuelle"""
    try:
        # 1. Vérifier l'entreprise active
        company = current_user.get_selected_company()
        if not company:
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('auth.logout'))

        # 2. Vérifier les permissions (admin/super_admin seulement)
        user_role = current_user.get_role_in_company(company.id)
        if user_role not in ['super_admin', 'admin']:
            flash('Accès refusé. Seuls les administrateurs peuvent supprimer des factures.', 'error')
            return safe_redirect(request.referrer, url_for('client.list_clients'))

        # 3. Vérifier que la facture appartient à l'entreprise
        invoice = Invoice.query.filter_by(
            id=invoice_id,
            company_id=company.id
        ).first()

        if not invoice:
            flash('Facture introuvable ou accès non autorisé.', 'error')
            return safe_redirect(request.referrer, url_for('client.list_clients'))

        # Garder l'ID client et numéro pour redirection et confirmation
        client_id = invoice.client_id
        invoice_number = invoice.invoice_number

        # 4. Suppression de la facture
        db.session.delete(invoice)
        db.session.commit()

        log_action(
            AuditActions.INVOICE_DELETED,
            entity_type=EntityTypes.INVOICE,
            entity_id=invoice_id,
            entity_name=invoice_number
        )

        flash(f'Facture {invoice_number} supprimée avec succès.', 'success')
        return redirect(url_for('client.detail_client', id=client_id))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting invoice {invoice_id}: {e}")
        flash('Une erreur est survenue lors de la suppression. Veuillez reessayer.', 'error')
        return safe_redirect(request.referrer, url_for('client.list_clients'))


@invoice_bp.route('/delete-multiple', methods=['POST'])
@login_required
def delete_multiple_invoices():
    """Supprime plusieurs factures via checkboxes"""
    try:
        # 1. Vérifier l'entreprise active
        company = current_user.get_selected_company()
        if not company:
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('auth.logout'))

        # 2. Vérifier les permissions (admin/super_admin seulement)
        user_role = current_user.get_role_in_company(company.id)
        if user_role not in ['super_admin', 'admin']:
            flash('Accès refusé. Seuls les administrateurs peuvent supprimer des factures.', 'error')
            return safe_redirect(request.referrer, url_for('client.list_clients'))

        # 3. Récupérer les IDs depuis le formulaire
        invoice_ids = request.form.getlist('invoice_ids')
        if not invoice_ids:
            flash('Aucune facture sélectionnée.', 'warning')
            return safe_redirect(request.referrer, url_for('client.list_clients'))

        # 4. Vérifier et récupérer les factures valides
        invoices = Invoice.query.filter(
            Invoice.id.in_(invoice_ids),
            Invoice.company_id == company.id
        ).all()

        if not invoices:
            flash('Aucune facture valide trouvée.', 'error')
            return safe_redirect(request.referrer, url_for('client.list_clients'))

        # Garder l'ID du premier client pour redirection
        client_id = invoices[0].client_id
        count = len(invoices)

        # 5. Suppression en bulk
        for invoice in invoices:
            db.session.delete(invoice)

        db.session.commit()

        flash(f'{count} facture(s) supprimée(s) avec succès.', 'success')
        return redirect(url_for('client.detail_client', id=client_id))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting multiple invoices: {e}")
        flash('Une erreur est survenue lors de la suppression. Veuillez reessayer.', 'error')
        return safe_redirect(request.referrer, url_for('client.list_clients'))


@invoice_bp.route('/<int:invoice_id>/download-pdf', methods=['GET'])
@login_required
def download_invoice_pdf(invoice_id):
    """Télécharge le PDF d'une facture depuis QuickBooks, Xero, Odoo ou Business Central"""
    invoice = None
    try:
        # Vérifier l'entreprise active
        company = current_user.get_selected_company()
        if not company:
            flash('Aucune entreprise sélectionnée.', 'error')
            return redirect(url_for('auth.logout'))

        # Vérifier les permissions (admin/super_admin seulement)
        user_role = current_user.get_role_in_company(company.id)
        if user_role not in ['super_admin', 'admin']:
            flash('Accès refusé. Seuls les administrateurs peuvent télécharger des PDF de factures.', 'error')
            return redirect(url_for('client.list_clients'))

        # Récupérer la facture
        invoice = Invoice.query.filter_by(
            id=invoice_id,
            company_id=company.id
        ).first()

        if not invoice:
            flash('Facture introuvable.', 'error')
            return redirect(url_for('client.list_clients'))

        # Récupérer la connexion comptable active
        connection = AccountingConnection.query.filter_by(
            company_id=company.id,
            is_active=True
        ).first()

        if not connection:
            flash('Aucune connexion comptable active.', 'error')
            return redirect(url_for('client.detail_client', id=invoice.client_id))

        # Télécharger le PDF selon le type de système
        pdf_content = None

        if connection.system_type == 'quickbooks':
            # QuickBooks : Vérifier que l'ID externe existe
            if not invoice.invoice_id_external:
                flash('Cette facture n\'a pas été synchronisée avec QuickBooks.', 'warning')
                return redirect(url_for('client.detail_client', id=invoice.client_id))

            from quickbooks_connector import QuickBooksConnector
            connector = QuickBooksConnector(connection.id, company.id)
            pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)

        elif connection.system_type == 'xero':
            # Xero : Vérifier que l'ID externe existe
            if not invoice.invoice_id_external:
                flash('Cette facture n\'a pas été synchronisée avec Xero.', 'warning')
                return redirect(url_for('client.detail_client', id=invoice.client_id))

            from xero_connector import XeroConnector
            connector = XeroConnector(connection.id, company.id)
            pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)

        elif connection.system_type == 'odoo':
            # Odoo : Vérifier que l'ID externe existe
            if not invoice.invoice_id_external:
                flash('Cette facture n\'a pas été synchronisée avec Odoo.', 'warning')
                return redirect(url_for('client.detail_client', id=invoice.client_id))

            from odoo_connector import OdooConnector
            connector = OdooConnector(connection.id, company.id)
            pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)

        elif connection.system_type == 'business_central':
            # Business Central : utilise invoice_number (invoice_id_external est NULL pour BC)
            if not invoice.invoice_number:
                flash('Cette facture n\'a pas de numéro de document.', 'warning')
                return redirect(url_for('client.detail_client', id=invoice.client_id))

            from business_central_connector import BusinessCentralConnector
            connector = BusinessCentralConnector(connection.id)
            pdf_content = connector.download_invoice_pdf(invoice.invoice_number)

        else:
            flash(f'Système comptable "{connection.system_type}" non supporté pour le téléchargement de PDF.', 'error')
            return redirect(url_for('client.detail_client', id=invoice.client_id))

        # Envoyer le PDF au navigateur
        pdf_file = BytesIO(pdf_content)
        pdf_file.seek(0)

        filename = f"facture_{invoice.invoice_number}.pdf"

        return send_file(
            pdf_file,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename
        )

    except ValueError as e:
        current_app.logger.error(f"Value error downloading invoice PDF {invoice_id}: {e}")
        flash('Une erreur est survenue lors du telechargement du PDF. Veuillez reessayer.', 'error')
        if invoice:
            return redirect(url_for('client.detail_client', id=invoice.client_id))
        return redirect(url_for('client.list_clients'))

    except Exception as e:
        current_app.logger.error(f"Error downloading invoice PDF {invoice_id}: {e}")
        flash('Une erreur est survenue lors du telechargement du PDF. Veuillez reessayer.', 'error')
        if invoice:
            return redirect(url_for('client.detail_client', id=invoice.client_id))
        return redirect(url_for('client.list_clients'))


# ---------------------------------------------------------------------------
# T002 — Autocomplete de recherche de factures par numéro
# ---------------------------------------------------------------------------

@invoice_bp.route('/api/search', methods=['GET'])
@login_required
def search_invoices():
    """Autocomplete : retourne les factures dont le numéro commence par `q` pour un client donné."""
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'error': 'Accès refusé'}), 403

    client_id = request.args.get('client_id', type=int)
    q = request.args.get('q', '').strip()
    include_children = request.args.get('include_children', '0') == '1'

    if not client_id:
        return jsonify({'error': 'client_id requis'}), 400

    from models import Client
    client = Client.query.filter_by(id=client_id, company_id=company.id).first()
    if not client:
        return jsonify({'error': 'Client introuvable'}), 404

    # Build list of client IDs to search
    client_ids = [client_id]
    client_name_map = {client_id: client.name}
    if include_children and client.is_parent:
        for child in client.child_clients:
            if child.company_id == company.id:
                client_ids.append(child.id)
                client_name_map[child.id] = child.name

    query = Invoice.query.filter(
        Invoice.client_id.in_(client_ids),
        Invoice.company_id == company.id
    )
    if q:
        query = query.filter(Invoice.invoice_number.ilike(f'{q}%'))

    limit = 10 if q else 30
    invoices = query.order_by(Invoice.invoice_number.asc()).limit(limit).all()

    return jsonify([
        {
            'id': inv.id,
            'invoice_number': inv.invoice_number or '',
            'amount': float(inv.amount) if inv.amount else 0,
            'is_paid': bool(inv.is_paid),
            'client_name': client_name_map.get(inv.client_id, '') if len(client_ids) > 1 else ''
        }
        for inv in invoices
    ])


# ---------------------------------------------------------------------------
# Helper interne : télécharger le PDF d'une facture et le retourner en bytes
# ---------------------------------------------------------------------------

def _fetch_invoice_pdf_bytes(invoice, connection):
    """
    Télécharge le PDF de la facture depuis le connecteur actif.
    Retourne (bytes, filename) ou lève une exception.
    """
    pdf_content = None
    system_type = connection.system_type

    if system_type == 'quickbooks':
        if not invoice.invoice_id_external:
            raise ValueError('Facture non synchronisée avec QuickBooks.')
        from quickbooks_connector import QuickBooksConnector
        connector = QuickBooksConnector(connection.id, invoice.company_id)
        pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)

    elif system_type == 'xero':
        if not invoice.invoice_id_external:
            raise ValueError('Facture non synchronisée avec Xero.')
        from xero_connector import XeroConnector
        connector = XeroConnector(connection.id, invoice.company_id)
        pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)

    elif system_type == 'odoo':
        if not invoice.invoice_id_external:
            raise ValueError('Facture non synchronisée avec Odoo.')
        from odoo_connector import OdooConnector
        connector = OdooConnector(connection.id, invoice.company_id)
        pdf_content = connector.download_invoice_pdf(invoice.invoice_id_external)

    elif system_type == 'business_central':
        if not invoice.invoice_number:
            raise ValueError('Aucun numéro de document pour cette facture BC.')
        from business_central_connector import BusinessCentralConnector
        connector = BusinessCentralConnector(connection.id)
        pdf_content = connector.download_invoice_pdf(invoice.invoice_number)

    else:
        raise ValueError(f'Système "{system_type}" non supporté pour le téléchargement de PDF.')

    if not pdf_content:
        raise ValueError('Le connecteur a retourné un contenu vide.')

    filename = f"facture_{invoice.invoice_number or invoice.id}.pdf"
    return pdf_content, filename


def _purge_expired_pdf_cache():
    """Supprime les entrées expirées du cache PDF temporaire."""
    cache = current_app.pdf_temp_cache
    now = datetime.utcnow()
    expired_keys = [k for k, v in cache.items() if v['expires'] < now]
    for k in expired_keys:
        del cache[k]


# ---------------------------------------------------------------------------
# T003 — Pré-téléchargement PDF vers le cache serveur
# ---------------------------------------------------------------------------

@invoice_bp.route('/api/prefetch-pdfs', methods=['POST'])
@login_required
def prefetch_invoice_pdfs():
    """
    Télécharge les PDF des factures demandées et les stocke dans le cache mémoire
    (current_app.pdf_temp_cache) avec un TTL de 30 minutes.
    Body JSON : {invoice_ids: [1, 2, 3]}
    Retourne : {results: {invoice_id: {success, filename, error}}}
    """
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'error': 'Accès refusé'}), 403

    data = request.get_json(silent=True) or {}
    invoice_ids = data.get('invoice_ids', [])
    if not invoice_ids:
        return jsonify({'error': 'invoice_ids requis'}), 400

    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        is_active=True
    ).first()
    if not connection:
        return jsonify({'error': 'Aucune connexion comptable active.'}), 400

    _purge_expired_pdf_cache()

    results = {}
    ttl = timedelta(minutes=30)

    for inv_id in invoice_ids:
        try:
            inv_id = int(inv_id)
        except (TypeError, ValueError):
            results[str(inv_id)] = {'success': False, 'filename': None, 'error': 'ID invalide'}
            continue

        invoice = Invoice.query.filter_by(id=inv_id, company_id=company.id).first()
        if not invoice:
            results[str(inv_id)] = {'success': False, 'filename': None, 'error': 'Facture introuvable'}
            continue

        try:
            pdf_bytes, filename = _fetch_invoice_pdf_bytes(invoice, connection)
            cache_key = (current_user.id, inv_id)
            current_app.pdf_temp_cache[cache_key] = {
                'bytes': pdf_bytes,
                'filename': filename,
                'company_id': company.id,
                'expires': datetime.utcnow() + ttl
            }
            results[str(inv_id)] = {'success': True, 'filename': filename, 'error': None}
        except Exception as exc:
            current_app.logger.warning(f'prefetch_invoice_pdfs: invoice {inv_id} → {exc}')
            results[str(inv_id)] = {'success': False, 'filename': None, 'error': str(exc)}

    return jsonify({'results': results})


# ---------------------------------------------------------------------------
# T006 — Micro-endpoint : info comptable d'un client (pour la modale notes)
# ---------------------------------------------------------------------------

@invoice_bp.route('/api/client/<int:client_id>/accounting-info', methods=['GET'])
@login_required
def client_accounting_info(client_id):
    """
    Retourne si le client a une connexion comptable active et son type.
    Utilisé par la modale notes pour afficher/masquer la sous-section "Copies de factures".
    """
    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin', 'employe']:
        return jsonify({'error': 'Accès refusé'}), 403

    from models import Client
    client = Client.query.filter_by(id=client_id, company_id=company.id).first()
    if not client:
        return jsonify({'error': 'Client introuvable'}), 404

    connection = AccountingConnection.query.filter_by(
        company_id=company.id,
        is_active=True
    ).first()

    supported_systems = ['quickbooks', 'xero', 'odoo', 'business_central']
    has_connection = (
        connection is not None
        and connection.system_type in supported_systems
    )

    return jsonify({
        'has_accounting_connection': has_connection,
        'accounting_system_type': connection.system_type if connection else None
    })