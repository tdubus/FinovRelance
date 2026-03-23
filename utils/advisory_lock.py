"""
Advisory lock utilities for multi-instance safety.

Uses PostgreSQL pg_try_advisory_lock to ensure only one instance
runs a given job at a time. Safe for use with multiple Gunicorn workers
or multiple Docker containers behind a load balancer.
"""

import functools
import hashlib
import logging
from flask import jsonify

logger = logging.getLogger(__name__)

# Fixed lock IDs for each cron job (unique per job)
LOCK_SYNC_EMAIL_V3 = 100001
LOCK_REFRESH_EMAIL_TOKENS = 100002
LOCK_DATABASE_BACKUP = 100003
LOCK_CLEANUP_OLD_LOGS = 100004
LOCK_APPLY_PENDING_CHANGES = 100005
LOCK_REFRESH_ACCOUNTING_TOKENS = 100006
LOCK_SYNC_MONITOR = 100007


def _get_lock_id(name):
    """Convert a string name to a consistent int64 lock ID."""
    h = hashlib.md5(name.encode()).digest()
    return int.from_bytes(h[:8], byteorder='big', signed=True)


def advisory_lock(lock_id):
    """Decorator for Flask route functions that should only run on one instance.

    Uses pg_try_advisory_lock (non-blocking). If the lock is already held
    by another connection, returns HTTP 200 with a message indicating
    the job is already running elsewhere.

    The lock is released in a finally block to guarantee cleanup.

    Args:
        lock_id: Integer lock ID (use constants from this module).
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            from app import db
            from sqlalchemy import text

            lock_acquired = False
            try:
                # Try to acquire the advisory lock (non-blocking)
                result = db.session.execute(
                    text('SELECT pg_try_advisory_lock(:lock_id)'),
                    {'lock_id': lock_id}
                )
                lock_acquired = result.scalar()

                if not lock_acquired:
                    logger.info(
                        f"Advisory lock {lock_id} already held - "
                        f"job '{f.__name__}' skipped on this instance"
                    )
                    return jsonify({
                        'success': True,
                        'message': 'Already running on another instance',
                        'status': 'skipped_lock'
                    }), 200

                # Lock acquired, run the actual function
                return f(*args, **kwargs)

            except Exception as e:
                logger.error(f"Error in advisory_lock decorator for {f.__name__}: {e}")
                raise

            finally:
                # Always release the lock if we acquired it
                if lock_acquired:
                    try:
                        db.session.execute(
                            text('SELECT pg_advisory_unlock(:lock_id)'),
                            {'lock_id': lock_id}
                        )
                        db.session.commit()
                    except Exception as unlock_error:
                        logger.error(f"Error releasing advisory lock {lock_id}: {unlock_error}")

        return wrapper
    return decorator


def try_advisory_lock(db, lock_id):
    """Low-level function to try acquiring an advisory lock.

    For use outside of Flask route decorators (e.g., background threads).

    Args:
        db: SQLAlchemy db instance
        lock_id: Integer lock ID

    Returns:
        bool: True if lock was acquired, False otherwise
    """
    from sqlalchemy import text
    try:
        result = db.session.execute(
            text('SELECT pg_try_advisory_lock(:lock_id)'),
            {'lock_id': lock_id}
        )
        return result.scalar()
    except Exception as e:
        logger.error(f"Error acquiring advisory lock {lock_id}: {e}")
        return False


def release_advisory_lock(db, lock_id):
    """Low-level function to release an advisory lock.

    Args:
        db: SQLAlchemy db instance
        lock_id: Integer lock ID
    """
    from sqlalchemy import text
    try:
        db.session.execute(
            text('SELECT pg_advisory_unlock(:lock_id)'),
            {'lock_id': lock_id}
        )
        db.session.commit()
    except Exception as e:
        logger.error(f"Error releasing advisory lock {lock_id}: {e}")
