"""
API routes for notification management
"""

from flask import Blueprint, jsonify
from flask_login import login_required, current_user
from models import Notification
from notification_system import get_unread_notifications, get_recent_notifications

notification_bp = Blueprint('notifications_system', __name__)

@notification_bp.route('/api/notifications/unread')
@login_required
def api_unread_notifications():
    """Get unread notifications count for current user"""
    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({'count': 0})

        notifications = get_unread_notifications(current_user.id, company.id)
        return jsonify({
            'count': len(notifications),
            'notifications': [notif.to_dict() for notif in notifications[:5]]  # Only send 5 most recent
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@notification_bp.route('/api/notifications/recent')
@login_required
def api_recent_notifications():
    """Get recent notifications for current user"""
    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({'notifications': []})

        notifications = get_recent_notifications(current_user.id, company.id, limit=10)
        return jsonify({
            'notifications': [notif.to_dict() for notif in notifications]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@notification_bp.route('/api/notifications/<int:notification_id>/mark-read', methods=['POST'])
@login_required
def api_mark_notification_read(notification_id):
    """Mark a specific notification as read"""
    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({'error': 'No company selected'}), 400

        notification = Notification.query.filter_by(
            id=notification_id,
            user_id=current_user.id,
            company_id=company.id
        ).first()

        if not notification:
            return jsonify({'error': 'Notification not found'}), 404

        notification.mark_as_read()
        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@notification_bp.route('/api/notifications/mark-all-read', methods=['POST'])
@login_required
def api_mark_all_read():
    """Mark all notifications as read for current user"""
    try:
        company = current_user.get_selected_company()
        if not company:
            return jsonify({'error': 'No company selected'}), 400

        Notification.query.filter_by(
            user_id=current_user.id,
            company_id=company.id,
            is_read=False
        ).update({'is_read': True})

        from app import db
        db.session.commit()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500