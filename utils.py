"FICHIER NETTOYÉ LE 2025-12-31"
from datetime import datetime
from flask import current_app, abort
import logging
import re as _re


def split_client_emails(raw):
    """Split a stored multi-email string (separated by ; or ,) into individual addresses."""
    if not raw:
        return []
    return [a.strip() for a in _re.split(r'[;,]', raw.strip().rstrip(';,')) if a.strip()]


def company_has_original_amount(company_id):
    """
    Helper partagé pour vérifier si une entreprise possède au moins une facture avec original_amount.

    Utilisé pour l'affichage conditionnel de la colonne "Montant Original" dans :
    - Templates UI (_invoice_table.html)
    - Exports Excel
    - États de compte PDF
    - Endpoints AJAX

    Args:
        company_id: L'ID de l'entreprise

    Returns:
        bool: True si au moins une facture a original_amount NOT NULL, False sinon
    """
    from app import db
    from models import Invoice

    return db.session.query(db.exists().where(
        db.and_(Invoice.company_id == company_id,
                Invoice.original_amount.isnot(None)))).scalar()


def safe_get_by_id(model_class, object_id, company_id, error_message=None):
    """
    SÉCURITÉ MULTI-TENANT : Récupère un objet par ID en vérifiant l'appartenance à company_id

    Args:
        model_class: La classe du modèle (ex: Client, Invoice, etc.)
        object_id: L'ID de l'objet à récupérer
        company_id: L'ID de l'entreprise pour vérification
        error_message: Message d'erreur personnalisé (optionnel)

    Returns:
        L'objet si trouvé et appartient à company_id

    Raises:
        404: Si l'objet n'existe pas
        403: Si l'objet n'appartient pas à l'entreprise
    """
    if not object_id:
        abort(404, error_message or "Objet non trouvé")

    obj = model_class.query.get(object_id)

    if not obj:
        abort(404, error_message or f"{model_class.__name__} non trouvé")

    # Vérifier l'appartenance à l'entreprise (si le modèle a company_id)
    if hasattr(obj, 'company_id'):
        if obj.company_id != company_id:
            current_app.logger.warning(
                f"SÉCURITÉ: Tentative d'accès cross-company refusée - "
                f"Model: {model_class.__name__}, ID: {object_id}, "
                f"Company demandée: {company_id}, Company réelle: {obj.company_id}"
            )
            abort(
                403,
                "Accès refusé - Cet objet n'appartient pas à votre entreprise")

    return obj


def safe_get_by_id_thread(model_class, object_id, company_id):
    """
    SÉCURITÉ MULTI-TENANT : Version THREAD-SAFE de safe_get_by_id

    Usage: Threads de background (ThreadPoolExecutor) SANS contexte Flask request
    Ne lève PAS d'exceptions HTTP (abort) car pas de contexte request
    Utilise logging standard (pas current_app.logger qui nécessite un contexte)

    Args:
        model_class: La classe du modèle
        object_id: L'ID de l'objet à récupérer
        company_id: L'ID de l'entreprise pour vérification

    Returns:
        L'objet si trouvé ET appartient à company_id
        None si objet non trouvé OU n'appartient pas à company_id
    """
    logger = logging.getLogger(__name__)

    if not object_id:
        logger.warning(
            f"SÉCURITÉ THREAD: object_id vide pour {model_class.__name__}")
        return None

    obj = model_class.query.get(object_id)

    if not obj:
        logger.warning(
            f"SÉCURITÉ THREAD: {model_class.__name__} ID {object_id} non trouvé"
        )
        return None

    # Vérifier ownership SEULEMENT pour les modèles avec company_id
    if hasattr(obj, 'company_id'):
        if obj.company_id != company_id:
            logger.error(
                f"🚨 SÉCURITÉ THREAD VIOLATION: {model_class.__name__} ID {object_id} - "
                f"Company demandée: {company_id}, Company réelle: {obj.company_id}"
            )
            return None

    return obj


# REFONTE STRIPE 2.0 - Fonction _get_stripe_items_safely simplifiée pour compatibilité V2
def _get_stripe_items_safely(subscription):
    """Accès sécurisé aux items d'une subscription Stripe - Support dict webhook + objet Stripe"""
    try:
        # CAS 1: Dictionnaire JSON (depuis webhook)
        if isinstance(subscription, dict):
            return subscription.get('items', {}).get('data', [])

        # CAS 2: Objet Stripe (depuis API call)
        elif hasattr(subscription,
                     'items') and not callable(subscription.items):
            items = subscription.items
            if hasattr(items, 'data'):
                return items.data
            elif hasattr(items, '__iter__') and not isinstance(items, str):
                return items
            else:
                return []
        else:
            return []
    except Exception as e:
        from flask import current_app
        current_app.logger.error(f"Erreur _get_stripe_items_safely: {str(e)}")
        return []


def get_item_price_id(item):
    """Helper pour extraire price_id de façon uniforme (dict webhook vs objet Stripe)"""
    try:
        # CAS 1: Dictionnaire JSON (webhook)
        if isinstance(item, dict):
            return item.get('price',
                            {}).get('id') if item.get('price') else None

        # CAS 2: Objet Stripe (API call)
        elif hasattr(item, 'price') and item.price:
            return item.price.id if hasattr(item.price, 'id') else None

        return None
    except Exception as e:
        from flask import current_app
        current_app.logger.error(f"Erreur get_item_price_id: {str(e)}")
        return None


def get_item_quantity(item):
    """Helper pour extraire quantity de façon uniforme (dict webhook vs objet Stripe)"""
    try:
        # CAS 1: Dictionnaire JSON (webhook)
        if isinstance(item, dict):
            return item.get('quantity', 1)

        # CAS 2: Objet Stripe (API call)
        elif hasattr(item, 'quantity'):
            return item.quantity

        return 1
    except Exception as e:
        from flask import current_app
        current_app.logger.error(f"Erreur get_item_quantity: {str(e)}")
        return 1


# Translations for PDF/Excel reports
TRANSLATIONS = {
    'fr': {
        'statement_title': 'Liste des factures impayées',
        'aging_summary': 'Résumé',
        'invoice_date': 'Date facture',
        'due_date': 'Date échéance',
        'invoice_number': 'Numéro facture',
        'original_amount': 'Montant Original',
        'amount': 'Montant',
        'days_overdue': 'Jours en retard',
        'company': 'Entreprise',
        'current': 'Courante',
        'days_0_30': '0-30 jours',
        'days_31_60': '31-60 jours',
        'days_61_90': '61-90 jours',
        'days_over_90': '90+ jours',
        'total': 'Total',
        'client_code': 'Code client',
        'client_name': 'Nom du client',
        'balance': 'Solde',
        'aging_calculation': 'Calcul d\'âge basé sur',
        'invoice_date_method': 'la date de facture',
        'due_date_method': 'la date d\'échéance',
        'account_statement': 'Relevé de compte',
        'parent_and_subsidiaries': 'Compte parent et succursales',
        'client': 'Client',
        'code': 'Code',
        'export_date': 'Date d\'export',
        'includes_children': 'Inclut {} comptes enfants',
        'communications': 'COMMUNICATIONS',
        'invoices': 'FACTURES',
        'client_code': 'Code client',
        'type': 'Type',
        'user': 'Utilisateur',
        'subject_title': 'Sujet/Titre',
        'note': 'Note',
        'reminder': 'Rappel',
        'invoice_num': 'N° Facture',
        'invoice_date': 'Date facture',
        'due_date': 'Date échéance',
        'days': 'Jours',
        'status': 'Statut',
        'total': 'TOTAL',
        'paid': 'Payé',
        'unpaid': 'Impayé',
        'date': 'Date',
        'project': 'Projet'
    },
    'en': {
        'statement_title': 'Outstanding Invoices List',
        'aging_summary': 'Summary',
        'invoice_date': 'Invoice Date',
        'due_date': 'Due Date',
        'invoice_number': 'Invoice Number',
        'original_amount': 'Original Amount',
        'amount': 'Amount',
        'days_overdue': 'Days Overdue',
        'company': 'Company',
        'current': 'Current',
        'days_0_30': '0-30 days',
        'days_31_60': '31-60 days',
        'days_61_90': '61-90 days',
        'days_over_90': '90+ days',
        'total': 'Total',
        'client_code': 'Client Code',
        'client_name': 'Client Name',
        'balance': 'Balance',
        'aging_calculation': 'Aging calculation based on',
        'invoice_date_method': 'invoice date',
        'due_date_method': 'due date',
        'account_statement': 'Account Statement',
        'parent_and_subsidiaries': 'Parent and Subsidiaries',
        'client': 'Client',
        'code': 'Code',
        'export_date': 'Export date',
        'includes_children': 'Includes {} child accounts',
        'communications': 'COMMUNICATIONS',
        'invoices': 'INVOICES',
        'client_code': 'Client Code',
        'type': 'Type',
        'user': 'User',
        'subject_title': 'Subject/Title',
        'note': 'Note',
        'reminder': 'Reminder',
        'invoice_num': 'Invoice #',
        'invoice_date': 'Invoice Date',
        'due_date': 'Due Date',
        'days': 'Days',
        'status': 'Status',
        'total': 'TOTAL',
        'paid': 'Paid',
        'unpaid': 'Unpaid',
        'date': 'Date',
        'project': 'Project'
    }
}


def get_translation(key, language='fr'):
    """Get translation for a key in specified language"""
    return TRANSLATIONS.get(language, TRANSLATIONS['fr']).get(key, key)


# NETTOYAGE: Fonctions timezone/formatting supprimées (maintenant dans utils/__init__.py)
# get_user_timezone, convert_utc_to_local, convert_local_to_utc, get_local_now
# format_local_datetime, format_local_date, check_feature_access, format_currency
# clean_note_text, replace_email_variables
# Importées via: from utils import fonction_name


def get_local_today(timezone_str=None):
    """Get today's date in local timezone"""
    from utils import get_local_now
    return get_local_now(timezone_str).date()


# FONCTION SUPPRIMÉE - utils.send_email() n'est plus utilisée
# Remplacée par MicrosoftOAuthConnector.send_email() dans email_views.py
# AUDIT COMPLET : Aucun code mort restant pour envoi d'emails


def send_password_reset_email(to_email, user_name, reset_url):
    """Send password reset email with fallback mechanisms"""

    # Try Microsoft Graph API first
    try:
        return _send_password_reset_via_graph(to_email, user_name, reset_url)
    except Exception as graph_error:
        current_app.logger.warning(
            f"Microsoft Graph API failed for password reset: {str(graph_error)}"
        )

        # Try SMTP fallback
        try:
            # from email_fallback import send_password_reset_via_smtp  # Function doesn't exist
            raise Exception("SMTP fallback not available")
        except Exception as smtp_error:
            current_app.logger.error(
                f"SMTP fallback failed for password reset: {str(smtp_error)}")

            # All methods failed
            raise Exception(
                f"All email methods failed for password reset. Graph: {str(graph_error)}, SMTP: {str(smtp_error)}"
            )


def _send_password_reset_via_graph(to_email, user_name, reset_url):
    """Send password reset email using Microsoft Graph API with auto-refresh"""
    import requests
    from models import SystemEmailConfiguration
    from email_fallback import refresh_system_oauth_token

    # Get the system email configuration for password reset
    email_config = SystemEmailConfiguration.query.filter_by(
        config_name='password_reset', is_active=True).first()

    if not email_config:
        # Fallback to any active system configuration
        email_config = SystemEmailConfiguration.query.filter_by(
            is_active=True).first()

    if not email_config or not email_config.outlook_oauth_access_token:
        raise Exception(
            "Configuration email système introuvable ou token OAuth manquant")

    # Prepare email content
    subject = "Réinitialisation de votre mot de passe - FinovRelance"

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px;">
                Réinitialisation de mot de passe
            </h2>

            <p>Bonjour {user_name},</p>

            <p>Vous avez demandé la réinitialisation de votre mot de passe pour votre compte FinovRelance.</p>

            <p>Pour réinitialiser votre mot de passe, cliquez sur le lien ci-dessous :</p>

            <div style="text-align: center; margin: 30px 0;">
                <a href="{reset_url}"
                   style="background-color: #3498db; color: white; padding: 12px 30px;
                          text-decoration: none; border-radius: 5px; display: inline-block;
                          font-weight: bold;">
                    Réinitialiser mon mot de passe
                </a>
            </div>

            <p><strong>Important :</strong> Ce lien est valide pendant 30 minutes seulement et ne peut être utilisé qu'une seule fois.</p>

            <p>Si vous n'avez pas demandé cette réinitialisation, ignorez simplement cet email. Votre mot de passe restera inchangé.</p>

            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">

            <p style="font-size: 12px; color: #666;">
                Cet email a été envoyé automatiquement par FinovRelance.<br>
                Pour toute question, contactez notre support.
            </p>
        </div>
    </body>
    </html>
    """

    # Prepare Microsoft Graph API request
    headers = {
        'Authorization': f'Bearer {email_config.outlook_oauth_access_token}',
        'Content-Type': 'application/json'
    }

    email_data = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html_content
            },
            "toRecipients": [{
                "emailAddress": {
                    "address": to_email
                }
            }]
        }
    }

    # Send email via Microsoft Graph API
    response = requests.post('https://graph.microsoft.com/v1.0/me/sendMail',
                             headers=headers,
                             json=email_data)

    if response.status_code == 202:
        current_app.logger.info(
            f"Password reset email sent successfully via Graph API to {to_email}"
        )
        return True
    else:
        # If token is expired, try to refresh it once
        if response.status_code == 401 and hasattr(
                email_config, 'outlook_oauth_refresh_token'
        ) and email_config.outlook_oauth_refresh_token:
            try:
                refresh_system_oauth_token(email_config)
                current_app.logger.info(
                    "System OAuth token refreshed after 401 error for password reset"
                )

                # Retry with new token
                headers[
                    'Authorization'] = f'Bearer {email_config.outlook_oauth_access_token}'
                response = requests.post(
                    'https://graph.microsoft.com/v1.0/me/sendMail',
                    headers=headers,
                    json=email_data)

                if response.status_code == 202:
                    current_app.logger.info(
                        f"Password reset email sent successfully after token refresh to {to_email}"
                    )
                    return True

            except Exception as refresh_error:
                current_app.logger.error(
                    f"Token refresh failed for password reset: {str(refresh_error)}"
                )

        error_msg = f"Failed to send password reset email: {response.status_code} - {response.text}"
        current_app.logger.error(error_msg)
        raise Exception(error_msg)


def prepare_logo_cache(company):
    """
    Prépare le logo de l'entreprise une seule fois pour réutilisation dans les PDFs.
    Retourne un dict avec le chemin du fichier temporaire et ses dimensions, ou None si pas de logo.

    OPTIMISATION CAMPAGNE: Sauvegarde le logo dans un fichier temporaire pour éviter
    le décodage Base64 + PIL à chaque PDF (gain de ~2s par PDF).

    IMPORTANT: Le fichier temporaire doit être supprimé après utilisation avec cleanup_logo_cache().
    """
    from reportlab.lib.units import inch
    from PIL import Image as PILImage
    from io import BytesIO
    import base64
    import re
    import tempfile
    import os

    if hasattr(company, 'logo_base64') and company.logo_base64:
        try:
            base64_match = re.search(r'base64,(.+)', company.logo_base64)
            if base64_match:
                base64_data = base64_match.group(1)
                logo_bytes = base64.b64decode(base64_data)

                logo_stream = BytesIO(logo_bytes)
                with PILImage.open(logo_stream) as pil_img:
                    original_width, original_height = pil_img.size
                    aspect_ratio = original_width / original_height

                    max_width_points = 3 * inch
                    max_height_points = 1.5 * inch

                    if (max_width_points / aspect_ratio) <= max_height_points:
                        logo_width = max_width_points
                        logo_height = logo_width / aspect_ratio
                    else:
                        logo_height = max_height_points
                        logo_width = logo_height * aspect_ratio

                    img_format = pil_img.format or 'PNG'

                suffix = '.png' if img_format.upper() == 'PNG' else '.jpg'
                fd, temp_path = tempfile.mkstemp(suffix=suffix,
                                                 prefix='logo_cache_')
                try:
                    os.write(fd, logo_bytes)
                finally:
                    os.close(fd)

                return {
                    'path': temp_path,
                    'width': logo_width,
                    'height': logo_height
                }
        except Exception as e:
            logging.error(f"Erreur préparation cache logo: {e}")
            return None
    return None


def cleanup_logo_cache(logo_cache):
    """Supprime le fichier temporaire du cache logo après utilisation."""
    import os
    if logo_cache and 'path' in logo_cache:
        try:
            if os.path.exists(logo_cache['path']):
                os.remove(logo_cache['path'])
        except Exception:
            pass


def generate_statement_pdf_reportlab(client,
                                     invoices,
                                     company,
                                     aging_balances,
                                     language='fr',
                                     logo_cache=None):
    """Generate PDF statement using ReportLab with improved logo and dynamic text colors

    Args:
        logo_cache: Optional dict with pre-decoded logo {'bytes': bytes, 'width': float, 'height': float}
                   Use prepare_logo_cache(company) to generate this once for batch operations.

    Layout improvements (2024):
        - Automatic landscape orientation when 8+ columns (parent+children + project + original_amount)
        - Lighter gray borders instead of black grid for premium look
        - Zebra rows (alternating backgrounds) for better readability
        - Right-aligned amounts, centered days overdue
        - Short columns use plain strings to prevent word wrapping
        - Reduced spacers to keep summary on same page when possible
    """
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor
    from io import BytesIO
    import os
    from PIL import Image as PILImage

    # Import format_currency from utils package (avoiding circular import)
    import sys
    from importlib import import_module
    utils_package = import_module('utils')
    _format_currency = utils_package.format_currency

    # Get company currency for formatting
    company_currency = company.currency if hasattr(company, 'currency') and company.currency else 'CAD'

    # Create a partial function that uses company currency
    def format_currency(amount):
        return _format_currency(amount, company_currency)

    # Check if company has any invoices with original_amount
    has_original_amount = company_has_original_amount(company.id)

    # Check if project feature is enabled
    from utils.project_helper import is_project_feature_enabled, get_project_label
    is_project_enabled = is_project_feature_enabled(company)
    project_label = get_project_label(company) if is_project_enabled else None

    # Check if this is a parent+children statement (pre-check for layout decisions)
    is_parent_children_precheck = len(invoices) > 0 and hasattr(
        invoices[0], 'client_name')

    # Calculate total column count for landscape decision
    # Base columns: parent+children adds Code column, plus Invoice#, InvDate, DueDate, Amount, DaysLate
    base_cols = 6 if is_parent_children_precheck else 5
    if is_project_enabled:
        base_cols += 1
    if has_original_amount:
        base_cols += 1

    # Use landscape when 8+ columns (widest scenario: parent+children + project + original_amount)
    use_landscape = base_cols >= 8

    def get_image_size_with_aspect_ratio(image_path,
                                         max_width_inches=3,
                                         max_height_inches=1.5):
        """Calculate optimal image size while preserving aspect ratio"""
        try:
            with PILImage.open(image_path) as img:
                original_width, original_height = img.size
                aspect_ratio = original_width / original_height

                # Calculate dimensions in inches
                max_width_points = max_width_inches * inch
                max_height_points = max_height_inches * inch

                # Determine best fit
                if (max_width_points / aspect_ratio) <= max_height_points:
                    # Width is the limiting factor
                    width = max_width_points
                    height = width / aspect_ratio
                else:
                    # Height is the limiting factor
                    height = max_height_points
                    width = height * aspect_ratio

                return width, height
        except Exception:
            # Fallback to default size if image cannot be processed
            return 2 * inch, 1 * inch

    def is_color_light(hex_color):
        """Determine if a color is light (needs dark text) or dark (needs light text)"""
        try:
            if hex_color.startswith('#'):
                hex_color = hex_color[1:]

            # Convert hex to RGB
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)

            # Calculate brightness using weighted average (similar to CSS luminance)
            brightness = (r * 0.299 + g * 0.587 + b * 0.114)

            # Return True if light (brightness > 128), False if dark
            return brightness > 128
        except Exception:
            # Default to dark background if parsing fails
            return False

    buffer = BytesIO()
    # Use landscape for wide tables, portrait otherwise
    page_size = landscape(letter) if use_landscape else letter
    # Reduce top margin for more compact layout
    doc = SimpleDocTemplate(buffer,
                            pagesize=page_size,
                            topMargin=0.6 * inch,
                            bottomMargin=0.6 * inch,
                            leftMargin=0.5 * inch,
                            rightMargin=0.5 * inch)
    styles = getSampleStyleSheet()

    # Calculate printable width based on orientation
    printable_width = 10 * inch if use_landscape else 7.5 * inch

    # Parse company colors with intelligent text color detection
    primary_color = HexColor(
        company.primary_color) if company.primary_color else colors.blue
    secondary_color = HexColor(
        company.secondary_color) if company.secondary_color else colors.grey

    # Determine text colors based on background brightness
    primary_bg_is_light = is_color_light(
        company.primary_color) if company.primary_color else False
    secondary_bg_is_light = is_color_light(
        company.secondary_color) if company.secondary_color else False

    # Set text colors: white for dark backgrounds, black for light backgrounds
    primary_text_color = colors.black if primary_bg_is_light else colors.white
    secondary_text_color = colors.black if secondary_bg_is_light else colors.white

    # Content list
    story = []

    # Logo (if exists) or company name with proper aspect ratio
    # OPTIMISATION: Utiliser le cache si fourni (évite de décoder Base64 à chaque PDF)
    logo_added = False

    if logo_cache and 'path' in logo_cache:
        try:
            logo = Image(logo_cache['path'],
                         width=logo_cache['width'],
                         height=logo_cache['height'])
            logo.hAlign = 'CENTER'
            story.append(logo)
            story.append(Spacer(1, 20))
            logo_added = True
        except Exception as e:
            current_app.logger.error(
                f"Erreur utilisation cache logo: {str(e)}")

    if not logo_added and hasattr(company,
                                  'logo_base64') and company.logo_base64:
        try:
            import base64
            import re

            # Extraire les données Base64 du data URI (format: data:image/png;base64,...)
            base64_match = re.search(r'base64,(.+)', company.logo_base64)
            if base64_match:
                base64_data = base64_match.group(1)
                logo_bytes = base64.b64decode(base64_data)

                # Créer un BytesIO pour l'image
                logo_stream = BytesIO(logo_bytes)

                # Calculer les dimensions optimales avec aspect ratio
                with PILImage.open(logo_stream) as pil_img:
                    original_width, original_height = pil_img.size
                    aspect_ratio = original_width / original_height

                    max_width_inches = 3
                    max_height_inches = 1.5
                    max_width_points = max_width_inches * inch
                    max_height_points = max_height_inches * inch

                    if (max_width_points / aspect_ratio) <= max_height_points:
                        logo_width = max_width_points
                        logo_height = logo_width / aspect_ratio
                    else:
                        logo_height = max_height_points
                        logo_width = logo_height * aspect_ratio

                # Reset stream position pour ReportLab
                logo_stream.seek(0)

                # Créer l'objet Image ReportLab depuis le stream
                logo = Image(logo_stream, width=logo_width, height=logo_height)
                logo.hAlign = 'CENTER'
                story.append(logo)
                story.append(Spacer(1, 20))
                logo_added = True
            else:
                # Fallback si format Base64 invalide
                company_name_style = ParagraphStyle(
                    'CompanyName',
                    parent=styles['Heading2'],
                    fontSize=16,
                    spaceAfter=20,
                    alignment=1  # Center
                )
                story.append(Paragraph(company.name, company_name_style))
                logo_added = True
        except Exception as e:
            # Fallback to company name if there's an error with Base64 logo
            current_app.logger.error(
                f"Erreur décodage logo Base64 pour PDF: {str(e)}")
            company_name_style = ParagraphStyle(
                'CompanyName',
                parent=styles['Heading2'],
                fontSize=16,
                spaceAfter=20,
                alignment=1  # Center
            )
            story.append(Paragraph(company.name, company_name_style))
            logo_added = True
    # FALLBACK TEMPORAIRE: Ancien système de fichiers (pour migration)
    elif hasattr(company, 'logo_path') and company.logo_path:
        try:
            logo_path = os.path.join('static/uploads/logos', company.logo_path)
            if os.path.exists(logo_path):
                # Calculate optimal size preserving aspect ratio
                logo_width, logo_height = get_image_size_with_aspect_ratio(
                    logo_path)
                logo = Image(logo_path, width=logo_width, height=logo_height)
                logo.hAlign = 'CENTER'
                story.append(logo)
                story.append(Spacer(1, 20))
            else:
                # Fallback to company name if logo file doesn't exist
                company_name_style = ParagraphStyle(
                    'CompanyName',
                    parent=styles['Heading2'],
                    fontSize=16,
                    spaceAfter=20,
                    alignment=1  # Center
                )
                story.append(Paragraph(company.name, company_name_style))
        except Exception:
            # Fallback to company name if there's an error with logo
            company_name_style = ParagraphStyle(
                'CompanyName',
                parent=styles['Heading2'],
                fontSize=16,
                spaceAfter=20,
                alignment=1  # Center
            )
            story.append(Paragraph(company.name, company_name_style))
    else:
        # No logo attribute or logo is empty - show company name
        company_name_style = ParagraphStyle(
            'CompanyName',
            parent=styles['Heading2'],
            fontSize=16,
            spaceAfter=20,
            alignment=1  # Center
        )
        story.append(Paragraph(company.name, company_name_style))

    # Header
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=30,
        alignment=1  # Center
    )

    # Check if this is a parent+children statement
    is_parent_children = len(invoices) > 0 and hasattr(invoices[0],
                                                       'client_name')

    # Title: just "Account Statement" / "Relevé de compte" without client name
    title_text = get_translation('account_statement', language)
    story.append(Paragraph(title_text, title_style))

    story.append(Spacer(1, 12))

    # Compact header: Company and Client info side by side using a table for better layout
    company_info = f"""<b>{company.name}</b><br/>
{company.address or ''}<br/>
{company.phone or ''}<br/>
{company.email or ''}"""

    client_info = f"""<b>{get_translation('client', language)}:</b> {client.name}<br/>
<b>{get_translation('code', language)}:</b> {client.code_client}<br/>
{client.address or ''}<br/>
{(client.email or '').split(';')[0].split(',')[0].strip()}"""

    # Create a two-column layout for company and client info
    info_table = Table([[
        Paragraph(company_info, styles['Normal']),
        Paragraph(client_info, styles['Normal'])
    ]],
                       colWidths=[printable_width / 2, printable_width / 2])
    info_table.setStyle(
        TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))
    story.append(info_table)
    story.append(Spacer(1, 15))

    # Invoices table
    if invoices:
        # Table title
        table_title = ParagraphStyle(
            'TableTitle',
            parent=styles['Heading3'],
            fontSize=14,
            spaceAfter=12,
            alignment=0,  # Left
            fontName='Helvetica-Bold'  # Remove italics
        )
        story.append(
            Paragraph(get_translation('statement_title', language),
                      table_title))

        # Check if this is a parent+children statement
        is_parent_children = len(invoices) > 0 and hasattr(
            invoices[0], 'client_name')

        # Headers based on statement type - UPDATED to include optional project and original_amount columns
        if is_parent_children:
            headers = [
                get_translation('code', language),
                get_translation('invoice_number', language).replace(' ', '\n')
            ]
            if is_project_enabled:
                # Use custom label or fallback to default translation
                project_header = (project_label or get_translation(
                    'project', language)).replace(' ', '\n')
                headers.append(project_header)
            headers.extend([
                get_translation('invoice_date', language).replace(' ', '\n'),
                get_translation('due_date', language).replace(' ', '\n')
            ])
            if has_original_amount:
                headers.append(
                    get_translation('original_amount',
                                    language).replace(' ', '\n'))
            headers.extend([
                get_translation('amount', language),
                get_translation('days_overdue', language).replace(' ', '\n')
            ])
            data = [headers]

            # Adjust column widths - use landscape (10") or portrait (7.5")
            if is_project_enabled and has_original_amount:
                # Code, Invoice#, Project, InvDate, DueDate, OrigAmt, Amount, DaysLate (8 cols) - LANDSCAPE
                col_widths = [
                    1.2 * inch, 1.15 * inch, 1.4 * inch, 1.1 * inch,
                    1.1 * inch, 1.15 * inch, 1.15 * inch, 0.75 * inch
                ]
            elif is_project_enabled:
                # Code, Invoice#, Project, InvDate, DueDate, Amount, DaysLate (7 cols = 7.5")
                col_widths = [
                    1.1 * inch, 1.05 * inch, 1.1 * inch, 0.95 * inch,
                    0.95 * inch, 1.1 * inch, 0.75 * inch
                ]
            elif has_original_amount:
                # Code, Invoice#, InvDate, DueDate, OrigAmt, Amount, DaysLate (7 cols = 7.5")
                col_widths = [
                    1.1 * inch, 1.05 * inch, 0.95 * inch, 0.95 * inch,
                    1.05 * inch, 1.05 * inch, 0.75 * inch
                ]
            else:
                # Code, Invoice#, InvDate, DueDate, Amount, DaysLate (6 cols = 7.5")
                col_widths = [
                    1.3 * inch, 1.2 * inch, 1.05 * inch, 1.05 * inch,
                    1.2 * inch, 0.7 * inch
                ]
        else:
            headers = [
                get_translation('invoice_number', language).replace(' ', '\n')
            ]
            if is_project_enabled:
                # Use custom label or fallback to default translation
                project_header = (project_label or get_translation(
                    'project', language)).replace(' ', '\n')
                headers.append(project_header)
            headers.extend([
                get_translation('invoice_date', language).replace(' ', '\n'),
                get_translation('due_date', language).replace(' ', '\n')
            ])
            if has_original_amount:
                headers.append(
                    get_translation('original_amount',
                                    language).replace(' ', '\n'))
            headers.extend([
                get_translation('amount', language),
                get_translation('days_overdue', language).replace(' ', '\n')
            ])
            data = [headers]

            # Adjust column widths to fill exactly 7.5" printable width (reduced margins)
            if is_project_enabled and has_original_amount:
                # Invoice#, Project, InvDate, DueDate, OrigAmt, Amount, DaysLate (7 cols = 7.5")
                col_widths = [
                    1.1 * inch, 1.15 * inch, 0.95 * inch, 0.95 * inch,
                    1.05 * inch, 1.05 * inch, 0.75 * inch
                ]
            elif is_project_enabled:
                # Invoice#, Project, InvDate, DueDate, Amount, DaysLate (6 cols = 7.5")
                col_widths = [
                    1.35 * inch, 1.35 * inch, 1.05 * inch, 1.05 * inch,
                    1.35 * inch, 0.75 * inch
                ]
            elif has_original_amount:
                # Invoice#, InvDate, DueDate, OrigAmt, Amount, DaysLate (6 cols = 7.5")
                col_widths = [
                    1.35 * inch, 1.05 * inch, 1.05 * inch, 1.15 * inch,
                    1.15 * inch, 0.75 * inch
                ]
            else:
                # Invoice#, InvDate, DueDate, Amount, DaysLate (5 cols = 7.5")
                col_widths = [
                    1.7 * inch, 1.2 * inch, 1.2 * inch, 1.7 * inch, 0.7 * inch
                ]

        # Style for project column only (may need wrapping for long project names)
        project_cell_style = ParagraphStyle(
            'ProjectCellStyle',
            parent=styles['Normal'],
            fontSize=8 if use_landscape else 9,
            wordWrap='CJK',
            alignment=0  # Left align for project names
        )

        for invoice in invoices:
            # Calculate days late using company's aging calculation method
            calc_date = invoice.invoice_date if company.aging_calculation_method == 'invoice_date' else invoice.due_date
            if calc_date:
                days_late = (datetime.now().date() - calc_date).days
                days_late_text = str(days_late) if days_late > 0 else "0"
            else:
                days_late_text = "N/A"

            # USE PLAIN STRINGS for short columns to prevent word wrapping
            # Only Project column uses Paragraph (may need wrapping for long names)
            if is_parent_children:
                client_code = getattr(invoice, 'client_code',
                                      client.code_client) or ''
                row = [client_code, invoice.invoice_number]
                # Add project column if enabled (uses Paragraph for natural wrapping)
                if is_project_enabled:
                    project_value = invoice.project_name if invoice.project_name else ''
                    row.append(Paragraph(project_value, project_cell_style))
                row.extend([
                    invoice.invoice_date.strftime('%Y-%m-%d'),
                    invoice.due_date.strftime('%Y-%m-%d')
                    if invoice.due_date else 'N/A'
                ])
                # Add original_amount column if company has it
                if has_original_amount:
                    original_amt_text = format_currency(
                        invoice.original_amount
                    ) if invoice.original_amount is not None else ''
                    row.append(original_amt_text)
                row.extend([format_currency(invoice.amount), days_late_text])
            else:
                row = [invoice.invoice_number]
                # Add project column if enabled (uses Paragraph for natural wrapping)
                if is_project_enabled:
                    project_value = invoice.project_name if invoice.project_name else ''
                    row.append(Paragraph(project_value, project_cell_style))
                row.extend([
                    invoice.invoice_date.strftime('%Y-%m-%d'),
                    invoice.due_date.strftime('%Y-%m-%d')
                    if invoice.due_date else 'N/A'
                ])
                # Add original_amount column if company has it
                if has_original_amount:
                    original_amt_text = format_currency(
                        invoice.original_amount
                    ) if invoice.original_amount is not None else ''
                    row.append(original_amt_text)
                row.extend([format_currency(invoice.amount), days_late_text])

            data.append(row)

        table = Table(data, colWidths=col_widths)

        # Determine column indices for amount columns (need right alignment)
        # Structure: [Code?], Invoice#, [Project?], InvDate, DueDate, [OrigAmt?], Amount, DaysLate
        num_cols = len(col_widths)
        amount_col = num_cols - 2  # Amount is second to last
        orig_amount_col = num_cols - 3 if has_original_amount else None
        days_col = num_cols - 1  # Days is last column

        # Build table style with premium look
        table_style_commands = [
            # Header row styling (uses company colors from Settings)
            ('BACKGROUND', (0, 0), (-1, 0), primary_color),
            ('TEXTCOLOR', (0, 0), (-1, 0), primary_text_color),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9 if use_landscape else 10),
            ('TOPPADDING', (0, 0), (-1, 0), 6),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 6),

            # All cells aligned LEFT (headers and data)
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),

            # Zebra rows - alternating light gray and white backgrounds
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [colors.white, HexColor('#F8F9FA')]),

            # Light gray borders instead of heavy black grid (premium look)
            ('LINEBELOW', (0, 0), (-1, 0), 1, HexColor('#CCCCCC')
             ),  # Header bottom border
            ('LINEBEFORE', (0, 0), (0, -1), 0.5,
             HexColor('#E0E0E0')),  # Left edge
            ('LINEAFTER', (-1, 0), (-1, -1), 0.5,
             HexColor('#E0E0E0')),  # Right edge
            ('LINEBELOW', (0, -1), (-1, -1), 0.5,
             HexColor('#E0E0E0')),  # Bottom edge
            ('INNERGRID', (0, 1), (-1, -1), 0.25,
             HexColor('#E8E8E8')),  # Subtle inner grid

            # Font size for data rows
            ('FONTSIZE', (0, 1), (-1, -1), 8 if use_landscape else 9),

            # Padding
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 1), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 3)
        ]

        table.setStyle(TableStyle(table_style_commands))

        story.append(table)
        story.append(Spacer(1,
                            15))  # Reduced spacer to keep summary on same page

    # Aging Summary Table (must not be split across pages)
    summary_title = ParagraphStyle(
        'SummaryTitle',
        parent=styles['Heading2'],
        fontSize=14,
        spaceAfter=10,
        alignment=0  # Left
    )

    summary_data = [[
        'Age Range' if language == 'en' else 'Tranche d\'âge',
        get_translation('amount', language)
    ]]
    summary_data.append([
        get_translation('current', language),
        format_currency(aging_balances['current'])
    ])
    summary_data.append([
        get_translation('days_0_30', language),
        format_currency(aging_balances['30_days'])
    ])
    summary_data.append([
        get_translation('days_31_60', language),
        format_currency(aging_balances['60_days'])
    ])
    summary_data.append([
        get_translation('days_61_90', language),
        format_currency(aging_balances['90_days'])
    ])
    summary_data.append([
        get_translation('days_over_90', language),
        format_currency(aging_balances['over_90_days'])
    ])

    # Calculate total
    total_balance = (aging_balances['current'] + aging_balances['30_days'] +
                     aging_balances['60_days'] + aging_balances['90_days'] +
                     aging_balances['over_90_days'])
    summary_data.append([
        get_translation('total', language).upper(),
        format_currency(total_balance)
    ])

    summary_table = Table(summary_data, colWidths=[2 * inch, 2 * inch])
    summary_table.setStyle(
        TableStyle([
            # Header row (uses secondary color from Settings)
            ('BACKGROUND', (0, 0), (-1, 0), secondary_color),
            ('TEXTCOLOR', (0, 0), (-1, 0), secondary_text_color),
            # All cells aligned LEFT (headers and data)
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            # Data rows with zebra
            ('ROWBACKGROUNDS', (0, 1), (-1, -2),
             [colors.white, HexColor('#F8F9FA')]),
            # Total row (uses secondary color from Settings)
            ('BACKGROUND', (0, -1), (-1, -1), secondary_color),
            ('TEXTCOLOR', (0, -1), (-1, -1), secondary_text_color),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            # Light gray borders for premium look
            ('LINEBELOW', (0, 0), (-1, 0), 0.75, HexColor('#CCCCCC')),
            ('LINEBEFORE', (0, 0), (0, -1), 0.5, HexColor('#E0E0E0')),
            ('LINEAFTER', (-1, 0), (-1, -1), 0.5, HexColor('#E0E0E0')),
            ('LINEBELOW', (0, -1), (-1, -1), 0.5, HexColor('#E0E0E0')),
            ('INNERGRID', (0, 1), (-1, -2), 0.25, HexColor('#E8E8E8')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 1), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 5)
        ]))

    # Keep summary together (don't split across pages)
    summary_section = [
        Paragraph(get_translation('aging_summary', language), summary_title),
        summary_table
    ]
    story.append(KeepTogether(summary_section))

    # Build PDF
    doc.build(story)
    buffer.seek(0)
    return buffer


def convert_signature_images_to_base64(html_content):
    """
    Convertit toutes les images locales dans un HTML de signature en Base64.

    Gère à la fois:
    - URLs relatives: /static/uploads/signatures/logo.png
    - URLs absolues locales: https://app.finov-relance.com/static/uploads/signatures/logo.png

    Args:
        html_content (str): HTML contenant potentiellement des <img src="/static/..."> ou <img src="https://.../static/...">

    Returns:
        str: HTML avec images converties en data URIs Base64

    Usage:
        signature_base64 = convert_signature_images_to_base64(signature_html)
    """
    import os
    import base64
    import imghdr
    from bs4 import BeautifulSoup
    from flask import current_app
    from urllib.parse import urlparse

    if not html_content:
        return html_content

    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        img_tags = soup.find_all('img')

        converted_count = 0

        for img in img_tags:
            src = img.get('src')
            if not src:
                continue

            file_path = None

            # CAS 1: URL relative (/static/...)
            if src.startswith('/static/'):
                file_path = src.lstrip('/')
                current_app.logger.debug(f"Image relative détectée: {src}")

            # CAS 2: URL absolue locale (https://app.finov-relance.com/static/...)
            elif src.startswith('http://') or src.startswith('https://'):
                parsed = urlparse(src)
                # Vérifier si c'est notre domaine
                if 'finov-relance.com' in parsed.netloc or 'localhost' in parsed.netloc:
                    # Extraire le chemin /static/...
                    if parsed.path.startswith('/static/'):
                        file_path = parsed.path.lstrip('/')
                        current_app.logger.debug(
                            f"Image absolue locale détectée: {src} → {file_path}"
                        )

            # Convertir l'image si un chemin local a été trouvé
            if file_path:

                if os.path.exists(file_path):
                    try:
                        # Lire le fichier image
                        with open(file_path, 'rb') as image_file:
                            image_data = image_file.read()

                        # Détecter le type MIME
                        image_type = imghdr.what(None, h=image_data)
                        if image_type:
                            mime_type = f"image/{image_type}"
                        else:
                            # Fallback sur l'extension
                            ext = os.path.splitext(file_path)[1].lower()
                            mime_map = {
                                '.png': 'image/png',
                                '.jpg': 'image/jpeg',
                                '.jpeg': 'image/jpeg',
                                '.gif': 'image/gif',
                                '.webp': 'image/webp',
                                '.svg': 'image/svg+xml'
                            }
                            mime_type = mime_map.get(ext, 'image/png')

                        # Encoder en Base64
                        base64_data = base64.b64encode(image_data).decode(
                            'utf-8')
                        data_uri = f"data:{mime_type};base64,{base64_data}"

                        # Remplacer le src
                        img['src'] = data_uri
                        converted_count += 1

                        current_app.logger.info(
                            f"Image signature convertie en Base64: {file_path} ({len(image_data)} bytes, {mime_type})"
                        )

                    except Exception as img_error:
                        current_app.logger.warning(
                            f"Impossible de convertir l'image {file_path}: {str(img_error)}"
                        )
                else:
                    current_app.logger.warning(
                        f"Image signature non trouvée: {file_path}")

        if converted_count > 0:
            current_app.logger.info(
                f"Conversion signature terminée: {converted_count} image(s) convertie(s) en Base64"
            )

        return str(soup)

    except Exception as e:
        current_app.logger.error(
            f"Erreur lors de la conversion des images en Base64: {str(e)}")
        # En cas d'erreur, retourner le HTML original
        return html_content
