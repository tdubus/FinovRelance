"""
Views for user management within company
Extracted from views.py monolith - Phase 6 Refactoring
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user

# Create users blueprint
users_bp = Blueprint('users', __name__, url_prefix='/users')

@users_bp.route('/')
@login_required
def users_list():
    """List all users in company"""
    from app import db
    from models import UserCompany, User

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions - only admins can manage users
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Seuls les administrateurs peuvent gérer les utilisateurs.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get all users for this company
    user_companies = db.session.query(UserCompany).filter_by(company_id=company.id).all()

    return render_template('users/list.html',
                         user_companies=user_companies,
                         company=company)


# API endpoint for users data
@users_bp.route('/api')
@login_required
def api_users():
    """API endpoint for users data (for datatables)"""
    from app import db
    from models import UserCompany, User

    company = current_user.get_selected_company()
    if not company:
        return jsonify({'error': 'Aucune entreprise sélectionnée'}), 400

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        return jsonify({'error': 'Accès refusé'}), 403

    # Get pagination parameters
    draw = request.args.get('draw', type=int)
    start = request.args.get('start', type=int)
    length = request.args.get('length', type=int)
    search_value = request.args.get('search[value]', '')

    # Base query
    query = db.session.query(UserCompany).join(User).filter(UserCompany.company_id == company.id)

    # Apply search filter
    if search_value:
        query = query.filter(
            db.or_(
                User.full_name.contains(search_value),
                User.email.contains(search_value),
                UserCompany.role.contains(search_value)
            )
        )

    # Get total count
    total_records = query.count()

    # Apply pagination
    if start is not None and length is not None:
        query = query.offset(start).limit(length)

    # Get results
    user_companies = query.all()

    # Format data for DataTables
    data = []
    for uc in user_companies:
        user = uc.user
        data.append({
            'id': user.id,
            'full_name': user.full_name,
            'email': user.email,
            'role': {
                'super_admin': 'Super Administrateur',
                'admin': 'Administrateur',
                'employe': 'Employé',
                'lecteur': 'Lecteur'
            }.get(uc.role, uc.role),
            'status': 'Actif' if uc.is_active else 'Inactif',
            'last_login': user.last_login.strftime('%d/%m/%Y %H:%M') if user.last_login else 'Jamais',
            'joined_at': uc.joined_at.strftime('%d/%m/%Y') if uc.joined_at else '',
            'actions': f'''
                <div class="btn-group" role="group">
                    <button type="button" class="btn btn-sm btn-outline-primary" onclick="editUser({user.id})">
                        <i class="bi bi-pencil"></i> Modifier
                    </button>
                    {'' if (uc.role == "super_admin" and user_role != "super_admin") or user.id == current_user.id else f'<button type="button" class="btn btn-sm btn-outline-danger" onclick="removeUser({user.id})"><i class="bi bi-trash"></i> Retirer</button>'}
                </div>
            '''
        })

    return jsonify({
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': total_records,
        'data': data
    })


@users_bp.route('/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    """Edit user in company"""
    from app import db
    from models import UserCompany, User
    from forms import EditUserForm

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Seuls les administrateurs peuvent modifier les utilisateurs.', 'error')
        return redirect(url_for('users.users_list'))

    # Get user and user_company relationship
    user_company = db.session.query(UserCompany).join(User).filter(
        UserCompany.company_id == company.id,
        UserCompany.user_id == user_id
    ).first()

    if not user_company:
        flash('Utilisateur non trouvé dans cette entreprise.', 'error')
        return redirect(url_for('users.users_list'))

    # Super admins cannot be modified by regular admins
    if user_company.role == 'super_admin' and current_user.id != user_id:
        current_user_role = current_user.get_role_in_company(company.id)
        if current_user_role != 'super_admin':
            flash('Seuls les super administrateurs peuvent modifier d\'autres super administrateurs.', 'error')
            return redirect(url_for('users.users_list'))

    user = user_company.user

    # Vérifier si l'utilisateur courant peut modifier la permission campagne
    plan_features = company.get_plan_features() or {}
    can_manage_campaign_permission = (
        user_role == 'super_admin' and
        plan_features.get('allows_email_sending', False)
    )

    # Initialize form with user data
    form = EditUserForm(
        original_email=user.email,
        company_id=company.id,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        role=user_company.role,
        can_create_campaigns=user_company.can_create_campaigns
    )

    if form.validate_on_submit():
        # VÉRIFICATION DES LICENCES lors du changement de rôle avec normalisation
        old_role = user_company.role
        new_role = form.role.data

        # Utiliser la fonction centralisée de validation des changements de rôle
        from utils.role_utils import validate_role_change
        can_change, message = validate_role_change(old_role, new_role, company)

        if not can_change:
            flash(f'Impossible de changer le rôle : {message}', 'error')
            return render_template('users/edit.html', form=form, user_company=user_company, user=user, company=company, can_manage_campaign_permission=can_manage_campaign_permission)

        # Update user data
        user.first_name = form.first_name.data
        user.last_name = form.last_name.data
        user.email = form.email.data
        user_company.role = new_role

        # SÉCURITÉ: Mettre à jour la permission de campagne uniquement si autorisé
        if can_manage_campaign_permission:
            user_company.can_create_campaigns = form.can_create_campaigns.data

        # Handle password reset if requested
        if form.reset_password.data and form.new_password.data:
            from werkzeug.security import generate_password_hash
            user.password_hash = generate_password_hash(form.new_password.data)
            user.must_change_password = True

            # Send email with new temporary password
            try:
                from email_fallback import send_password_reset_email
                send_password_reset_email(user.email, form.new_password.data, user.first_name)
                flash(f'Utilisateur {user.full_name} mis à jour. Un email avec le nouveau mot de passe temporaire a été envoyé.', 'success')
            except Exception as e:
                current_app.logger.error(f"Erreur envoi email: {e}")
                flash(f'Utilisateur {user.full_name} mis à jour. Attention : l\'email n\'a pas pu être envoyé.', 'warning')
        else:
            flash(f'Utilisateur {user.full_name} mis à jour avec succès.', 'success')

        try:
            db.session.commit()
            return redirect(url_for('users.users_list'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erreur mise à jour utilisateur: {e}")
            flash('Erreur lors de la mise à jour de l\'utilisateur.', 'error')

    return render_template('users/edit.html', form=form, user_company=user_company, user=user, company=company, can_manage_campaign_permission=can_manage_campaign_permission)


def safe_remove_user_from_company(user_id, company_id):
    """
    Retire un utilisateur d'une entreprise en supprimant uniquement son entrée UserCompany.
    Le compte utilisateur est conservé en base (notes, collector_id, etc. restent intacts).
    """
    from app import db
    from models import UserCompany, User, EmailConfiguration

    try:
        user_company = UserCompany.query.filter_by(
            user_id=user_id,
            company_id=company_id
        ).first()

        if not user_company:
            return False, "Utilisateur non trouvé dans cette entreprise."

        user = user_company.user
        user_name = user.full_name

        EmailConfiguration.query.filter_by(user_id=user_id, company_id=company_id).delete()

        db.session.delete(user_company)
        db.session.commit()

        from utils.secure_logging import sanitize_user_id_for_logs
        current_app.logger.info(f"Utilisateur user_id={sanitize_user_id_for_logs(user.id)} retiré de la company {company_id} (EmailConfiguration supprimée, compte conservé en base)")
        return True, f"{user_name} a été retiré de l'entreprise avec succès."

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erreur lors de la suppression de l'utilisateur {user_id}: {str(e)}")
        return False, f"Erreur lors de la suppression : {str(e)}"


@users_bp.route('/<int:user_id>/remove', methods=['POST'])
@login_required
def remove_user(user_id):
    """Remove user from company (with intelligent deletion)"""
    from app import db
    from models import UserCompany, User

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé. Seuls les administrateurs peuvent retirer des utilisateurs.', 'error')
        return redirect(url_for('users.users_list'))

    # Get user_company relationship for validation
    user_company = UserCompany.query.filter_by(
        company_id=company.id,
        user_id=user_id
    ).first()

    if not user_company:
        flash('Utilisateur non trouvé dans cette entreprise.', 'error')
        return redirect(url_for('users.users_list'))

    # Super admins can only be removed by other super admins
    if user_company.role == 'super_admin':
        # Check if current user is super admin
        if user_role != 'super_admin':
            flash('Seuls les super administrateurs peuvent retirer d\'autres super administrateurs.', 'error')
            return redirect(url_for('users.users_list'))

        # Prevent removing the last super admin
        super_admin_count = UserCompany.query.filter_by(
            company_id=company.id,
            role='super_admin'
        ).count()

        if super_admin_count <= 1:
            flash('Impossible de retirer le dernier super administrateur de l\'entreprise.', 'error')
            return redirect(url_for('users.users_list'))

    # Cannot remove yourself
    if user_id == current_user.id:
        flash('Vous ne pouvez pas vous retirer vous-même de l\'entreprise.', 'error')
        return redirect(url_for('users.users_list'))

    # Use intelligent removal service
    success, message = safe_remove_user_from_company(user_id, company.id)

    if success:
        flash(message, 'success')
    else:
        flash(message, 'error')

    return redirect(url_for('users.users_list'))