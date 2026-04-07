from flask import Blueprint, render_template, make_response
from app import limiter, cache

marketing_bp = Blueprint(
    'marketing',
    __name__,
    template_folder='../marketing_site/templates',
    static_folder='../marketing_site/static',
    static_url_path='/marketing-static'
)

def _cached_page(template, **kwargs):
    """Render template with public cache headers for Cloudflare edge + browser."""
    resp = make_response(render_template(template, **kwargs))
    resp.headers['Cache-Control'] = 'public, max-age=3600, s-maxage=86400'
    return resp


@marketing_bp.route('/')
@cache.cached(timeout=3600)
def index():
    return _cached_page('index_v2.html', active_page='accueil')

@marketing_bp.route('/essai')
@cache.cached(timeout=3600)
def essai():
    return _cached_page('ads_v2.html')

@marketing_bp.route('/essai-fr')
@cache.cached(timeout=3600)
def essai_fr():
    return _cached_page('ads_fr.html')

@marketing_bp.route('/demo-iframe')
@cache.cached(timeout=3600)
def demo_iframe():
    return _cached_page('demo_iframe.html')

@marketing_bp.route('/fonctionnalites')
@cache.cached(timeout=3600)
def fonctionnalites():
    return _cached_page('fonctionnalites.html', active_page='fonctionnalites')

@marketing_bp.route('/tarifs')
@cache.cached(timeout=3600)
def tarifs():
    return _cached_page('tarifs.html', active_page='tarifs')

@marketing_bp.route('/cas-usage')
@cache.cached(timeout=3600)
def cas_usage():
    return _cached_page('cas-usage.html', active_page='cas-usage')

@marketing_bp.route('/contact', methods=['GET', 'POST'])
@limiter.limit("3 per hour", methods=["POST"])
def contact():
    from flask import request, flash, redirect, url_for, current_app, session
    from email_fallback import send_email_via_system_config
    import re
    import time
    from markupsafe import escape

    if request.method == 'POST':
        honeypot = request.form.get('website_url', '').strip()
        if honeypot:
            current_app.logger.warning(f"Spam blocked: honeypot field filled")
            flash('Merci pour votre message ! Nous vous répondrons sous 24 heures.', 'success')
            return redirect(url_for('marketing.contact'))

        form_token = request.form.get('form_loaded_at', '')
        session_token = session.pop('contact_form_token', None)
        if not session_token or form_token != session_token:
            current_app.logger.warning(f"Spam blocked: invalid or missing form token")
            flash('Merci pour votre message ! Nous vous répondrons sous 24 heures.', 'success')
            return redirect(url_for('marketing.contact'))

        try:
            load_time = float(form_token.split('_')[0])
            elapsed = time.time() - load_time
            if elapsed < 3:
                current_app.logger.warning(f"Spam blocked: form submitted too fast ({elapsed:.1f}s)")
                flash('Merci pour votre message ! Nous vous répondrons sous 24 heures.', 'success')
                return redirect(url_for('marketing.contact'))
        except (ValueError, TypeError, IndexError):
            pass

        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr) or 'unknown'
        if ',' in client_ip:
            client_ip = client_ip.split(',')[0].strip()

        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        company = request.form.get('company', '').strip()
        subject_type = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()

        if not name or not email or not subject_type or not message:
            flash('Veuillez remplir tous les champs obligatoires.', 'error')
            return redirect(url_for('marketing.contact'))

        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            flash('Veuillez entrer une adresse email valide.', 'error')
            return redirect(url_for('marketing.contact'))

        url_pattern = r'https?://[^\s<>\"\']+|www\.[^\s<>\"\']+|\.[a-z]{2,}/[^\s<>\"\']*'
        spam_detected = False
        spam_reason = ""

        for field_name, field_value in [('name', name), ('company', company), ('message', message)]:
            if re.search(url_pattern, field_value, re.IGNORECASE):
                spam_detected = True
                spam_reason = f"URL in {field_name} field"
                break

        suspicious_patterns = [
            r'(?i)credit\s+available',
            r'(?i)confirm\s+your\s+transfer',
            r'(?i)click\s+here\s+to\s+claim',
            r'(?i)you\s+have\s+won',
            r'(?i)congratulations.*winner',
            r'(?i)earn\s+\$?\d+.*per\s+(day|hour|week)',
            r'(?i)bitcoin.*profit',
            r'(?i)crypto.*invest',
            r'\$\d{1,3}(,\d{3})+',
        ]
        all_text = f"{name} {company} {message}"
        for pattern in suspicious_patterns:
            if re.search(pattern, all_text):
                spam_detected = True
                spam_reason = f"Suspicious pattern: {pattern}"
                break

        if len(message) < 10:
            spam_detected = True
            spam_reason = "Message too short"

        if len(name) > 100 or len(company) > 100 or len(message) > 5000:
            spam_detected = True
            spam_reason = "Field length exceeded"

        if subject_type not in ('commercial', 'technique', 'support', 'partenaire', 'autre'):
            spam_detected = True
            spam_reason = "Invalid subject type"

        if spam_detected:
            current_app.logger.warning(f"Spam blocked: {spam_reason} | email={email} | IP={client_ip}")
            flash('Merci pour votre message ! Nous vous répondrons sous 24 heures.', 'success')
            return redirect(url_for('marketing.contact'))

        safe_name = str(escape(name))
        safe_name = str(safe_name).replace('\r', '').replace('\n', '')
        safe_email = str(escape(email))
        safe_company = str(escape(company))
        safe_message = str(escape(message))

        subject_labels = {
            'commercial': 'Demande commerciale',
            'technique': 'Question technique',
            'support': 'Support',
            'partenaire': 'Programme partenaire',
            'autre': 'Autre'
        }

        subject_label = subject_labels.get(subject_type, 'Contact')
        email_subject = f"[Contact Web] {subject_label} - {safe_name}"

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background-color: #f8f9fa; padding: 30px; border-radius: 10px;">
                <h2 style="color: #8475EC; margin-bottom: 20px;">Nouveau message du formulaire de contact</h2>

                <div style="background-color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                    <p style="margin-bottom: 15px;"><strong>Nom :</strong> {safe_name}</p>
                    <p style="margin-bottom: 15px;"><strong>Email :</strong> {safe_email}</p>
                    {f'<p style="margin-bottom: 15px;"><strong>Entreprise :</strong> {safe_company}</p>' if company else ''}
                    <p style="margin-bottom: 15px;"><strong>Sujet :</strong> {subject_label}</p>
                </div>

                <div style="background-color: white; padding: 20px; border-radius: 8px;">
                    <h3 style="color: #374151; margin-bottom: 15px;">Message :</h3>
                    <p style="color: #4B5563; line-height: 1.6; white-space: pre-wrap;">{safe_message}</p>
                </div>

                <div style="margin-top: 20px; padding: 15px; background-color: #EDE9FE; border-radius: 8px;">
                    <p style="margin: 0; font-size: 14px; color: #6B5DC0;">
                        Pour repondre, utilisez : <a href="mailto:{safe_email}" style="color: #8475EC;">{safe_email}</a>
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

        import threading

        def _send_async(app_obj, to, subj, html):
            with app_obj.app_context():
                try:
                    send_email_via_system_config(to_email=to, subject=subj, html_content=html)
                    app_obj.logger.info(f"Contact form email sent from {safe_email}")
                except Exception as exc:
                    app_obj.logger.error(f"Failed to send contact form email: {exc}")

        threading.Thread(
            target=_send_async,
            args=(current_app._get_current_object(), 'support@finov-relance.com', email_subject, html_content),
            daemon=True,
        ).start()

        flash('Merci pour votre message ! Nous vous répondrons sous 24 heures.', 'success')
        return redirect(url_for('marketing.contact'))

    import time
    import secrets
    form_token = f"{time.time()}_{secrets.token_hex(16)}"
    session['contact_form_token'] = form_token
    return render_template('contact.html', form_timestamp=form_token, active_page='contact')

@marketing_bp.route('/guide')
@cache.cached(timeout=600)
def guide():
    """Liste des pages de guide publiées"""
    from models import GuidePage

    guides = GuidePage.query.filter_by(is_published=True).order_by(
        GuidePage.order.asc(),
        GuidePage.created_at.desc()
    ).all()

    return _cached_page('guide.html', guides=guides, active_page='guide')


@marketing_bp.route('/guide/<slug>')
@cache.cached(timeout=600)
def guide_page(slug):
    """Afficher une page de guide individuelle"""
    from models import GuidePage
    from flask import abort
    import bleach

    guide = GuidePage.query.filter_by(slug=slug, is_published=True).first()

    if not guide:
        abort(404)

    # Sanitize guide content to prevent XSS
    allowed_tags = [
        'p', 'br', 'strong', 'em', 'u', 'a', 'ul', 'ol', 'li',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre',
        'code', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
        'div', 'span', 'hr', 'sub', 'sup',
    ]
    allowed_attrs = {
        'a': ['href', 'title', 'target', 'rel'],
        'img': ['src', 'alt', 'title', 'width', 'height'],
        'td': ['colspan', 'rowspan'],
        'th': ['colspan', 'rowspan'],
        'div': ['class'],
        'span': ['class'],
        'p': ['class'],
        'pre': ['class'],
        'code': ['class'],
    }
    if guide.content:
        guide.content = bleach.clean(
            guide.content,
            tags=allowed_tags,
            attributes=allowed_attrs,
            strip=True,
        )

    # Validate video_url - only allow trusted embed domains
    if guide.video_url:
        allowed_video_prefixes = (
            'https://www.youtube.com/',
            'https://www.youtube-nocookie.com/',
            'https://player.vimeo.com/',
        )
        if not guide.video_url.startswith(allowed_video_prefixes):
            guide.video_url = None

    # Récupérer les autres guides pour la navigation
    other_guides = GuidePage.query.filter(
        GuidePage.id != guide.id,
        GuidePage.is_published == True
    ).order_by(GuidePage.order.asc()).limit(5).all()

    return _cached_page('guide_page.html', guide=guide, other_guides=other_guides, active_page='guide')
