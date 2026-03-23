"""
Views for import job history
Provides routes for viewing import job list and individual job details.
The classic CSV import has been replaced by the intelligent Excel/CSV connector in Settings.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user

# Create import blueprint
import_bp = Blueprint('import', __name__, url_prefix='/import')


@import_bp.route('/jobs')
@login_required
def list_jobs():
    """List all import jobs for the current company"""
    from models import ImportJob

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    # Get all jobs for this company, ordered by most recent first
    jobs = ImportJob.query.filter_by(company_id=company.id).order_by(ImportJob.created_at.desc()).all()

    return render_template('import/jobs_list.html', jobs=jobs, company=company)


@import_bp.route('/jobs/<int:job_id>')
@login_required
def view_job(job_id):
    """View details of a specific import job"""
    from models import ImportJob

    company = current_user.get_selected_company()
    if not company:
        flash('Aucune entreprise sélectionnée.', 'error')
        return redirect(url_for('auth.logout'))

    job = ImportJob.query.filter_by(id=job_id, company_id=company.id).first()
    if not job:
        flash('Import introuvable.', 'error')
        return redirect(url_for('import.list_jobs'))

    # Check permissions
    user_role = current_user.get_role_in_company(company.id)
    if user_role not in ['super_admin', 'admin']:
        flash('Accès refusé.', 'error')
        return redirect(url_for('main.dashboard'))

    return render_template('import/job_detail.html', job=job, company=company)
