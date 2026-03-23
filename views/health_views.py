"""
Endpoint /health pour monitoring applicatif.
Public (pas de login_required) pour Coolify health check et monitoring externe.
"""

from flask import Blueprint, jsonify
from sqlalchemy import text
import time

health_bp = Blueprint('health', __name__)


@health_bp.route('/health')
def health_check():
    """Health check avec metriques de performance (DB latency, pool stats)"""
    from app import db

    checks = {}

    # DB connectivity + latency
    try:
        start = time.time()
        db.session.execute(text('SELECT 1'))
        checks['database'] = {
            'status': 'ok',
            'latency_ms': round((time.time() - start) * 1000, 2)
        }
    except Exception as e:
        checks['database'] = {'status': 'error', 'error': str(e)}

    # DB pool stats
    try:
        pool = db.engine.pool
        checks['pool'] = {
            'size': pool.size(),
            'checked_out': pool.checkedout(),
            'overflow': pool.overflow(),
        }
    except Exception:
        checks['pool'] = {'status': 'unavailable'}

    overall = 'ok' if all(
        c.get('status') == 'ok'
        for c in checks.values()
        if isinstance(c, dict) and 'status' in c
    ) else 'degraded'

    return jsonify({
        'status': overall,
        'checks': checks,
    }), 200 if overall == 'ok' else 503
