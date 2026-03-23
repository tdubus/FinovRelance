import uuid
import time
import json
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional
import logging
from flask import Blueprint, Response, stream_with_context
from flask_login import login_required

logger = logging.getLogger(__name__)

import_progress_bp = Blueprint('import_progress', __name__)

# Redis key prefix for import progress sessions
REDIS_PREFIX = 'import_progress:'
REDIS_TTL = 7200  # 2 hours


def _get_redis_client():
    """Get Redis client from Flask app config. Returns None if unavailable."""
    try:
        import os
        redis_url = os.environ.get('REDIS_URL')
        if not redis_url:
            return None
        import redis
        client = redis.from_url(redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None


class ImportProgress:
    """Represents a single import progress session (used for in-memory fallback)."""
    def __init__(self, session_id: str, total_rows: int):
        self.session_id = session_id
        self.total_rows = total_rows
        self.current_row = 0
        self.current_step = 'Initialisation'
        self.started_at = datetime.utcnow()
        self.completed_at: Optional[datetime] = None
        self.status = 'running'
        self.error_message: Optional[str] = None
        self.messages = []
        self._lock = threading.Lock()  # Thread safety for message list

    def update(self, current_row: int, step: str, message: Optional[str] = None):
        self.current_row = current_row
        self.current_step = step
        if message:
            with self._lock:
                self.messages.append({
                    'timestamp': datetime.utcnow().isoformat(),
                    'message': message
                })

    def complete(self, success: bool = True, error_message: Optional[str] = None):
        self.completed_at = datetime.utcnow()
        self.status = 'completed' if success else 'error'
        self.error_message = error_message
        # Force 100% completion even if total_rows was initially 0
        if self.total_rows > 0:
            self.current_row = self.total_rows
        else:
            # If total is still unknown, force display to 100%
            self.current_row = 100
            self.total_rows = 100

    def get_progress_percent(self) -> int:
        if self.total_rows == 0:
            return 0  # Return 0% when total is unknown (not 100%)
        return min(100, int((self.current_row / self.total_rows) * 100))

    def to_dict(self) -> dict:
        # Thread-safe copy of messages
        with self._lock:
            recent_messages = self.messages[-5:]

        return {
            'session_id': self.session_id,
            'total_rows': self.total_rows,
            'current_row': self.current_row,
            'current_step': self.current_step,
            'progress_percent': self.get_progress_percent(),
            'status': self.status,
            'error_message': self.error_message,
            'elapsed_seconds': (datetime.utcnow() - self.started_at).total_seconds(),
            'messages': recent_messages
        }


class ImportProgressManager:
    """Manages import progress sessions.

    Primary storage: Redis (shared across instances).
    Fallback: In-memory dict (single instance only).

    The public API is identical regardless of storage backend.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._redis = None
            cls._instance._redis_checked = False
            # In-memory fallback
            cls._instance.sessions: Dict[str, ImportProgress] = {}
            cls._instance.cleanup_interval = 3600
            cls._instance.last_cleanup = datetime.utcnow()
            cls._instance.session_lock = threading.Lock()
        return cls._instance

    def _get_redis(self):
        """Lazy-init Redis connection with caching."""
        if not self._redis_checked:
            self._redis = _get_redis_client()
            self._redis_checked = True
            if self._redis:
                logger.info("ImportProgressManager: using Redis storage")
            else:
                logger.info("ImportProgressManager: using in-memory fallback")
        return self._redis

    def _redis_key(self, session_id: str) -> str:
        return f"{REDIS_PREFIX}{session_id}"

    def create_session(self, total_rows: int) -> str:
        session_id = str(uuid.uuid4())
        redis_client = self._get_redis()

        if redis_client:
            try:
                data = {
                    'session_id': session_id,
                    'total_rows': total_rows,
                    'current_row': 0,
                    'current_step': 'Initialisation',
                    'started_at': datetime.utcnow().isoformat(),
                    'completed_at': None,
                    'status': 'running',
                    'error_message': None,
                    'messages': []
                }
                redis_client.setex(
                    self._redis_key(session_id),
                    REDIS_TTL,
                    json.dumps(data)
                )
                logger.info(f'Session de progression creee (Redis): {session_id} ({total_rows} lignes)')
                return session_id
            except Exception as e:
                logger.warning(f"Redis write failed, falling back to memory: {e}")

        # Fallback: in-memory
        with self.session_lock:
            self.sessions[session_id] = ImportProgress(session_id, total_rows)
            logger.info(f'Session de progression creee (memoire): {session_id} ({total_rows} lignes)')
            self._cleanup_old_sessions()
        return session_id

    def get_session(self, session_id: str) -> Optional[ImportProgress]:
        """Get session for SSE streaming. Returns ImportProgress object for compatibility."""
        redis_client = self._get_redis()

        if redis_client:
            try:
                raw = redis_client.get(self._redis_key(session_id))
                if raw:
                    return self._data_to_progress(json.loads(raw))
            except Exception as e:
                logger.warning(f"Redis read failed: {e}")

        # Fallback: in-memory
        with self.session_lock:
            return self.sessions.get(session_id)

    def update_progress(self, session_id: str, current_row: int, step: str, message: Optional[str] = None):
        redis_client = self._get_redis()

        if redis_client:
            try:
                raw = redis_client.get(self._redis_key(session_id))
                if raw:
                    data = json.loads(raw)
                    data['current_row'] = current_row
                    data['current_step'] = step
                    if message:
                        messages = data.get('messages', [])
                        messages.append({
                            'timestamp': datetime.utcnow().isoformat(),
                            'message': message
                        })
                        # Keep only last 20 messages to limit Redis memory
                        data['messages'] = messages[-20:]
                    redis_client.setex(
                        self._redis_key(session_id),
                        REDIS_TTL,
                        json.dumps(data)
                    )
                    return
            except Exception as e:
                logger.warning(f"Redis update failed: {e}")

        # Fallback: in-memory
        with self.session_lock:
            session = self.sessions.get(session_id)
            if session:
                session.update(current_row, step, message)

    def complete_session(self, session_id: str, success: bool = True, error_message: Optional[str] = None):
        redis_client = self._get_redis()

        if redis_client:
            try:
                raw = redis_client.get(self._redis_key(session_id))
                if raw:
                    data = json.loads(raw)
                    data['completed_at'] = datetime.utcnow().isoformat()
                    data['status'] = 'completed' if success else 'error'
                    data['error_message'] = error_message
                    total_rows = data.get('total_rows', 0)
                    if total_rows > 0:
                        data['current_row'] = total_rows
                    else:
                        data['current_row'] = 100
                        data['total_rows'] = 100
                    redis_client.setex(
                        self._redis_key(session_id),
                        REDIS_TTL,
                        json.dumps(data)
                    )
                    logger.info(f'Session terminee (Redis): {session_id} (statut: {data["status"]})')
                    return
            except Exception as e:
                logger.warning(f"Redis complete failed: {e}")

        # Fallback: in-memory
        with self.session_lock:
            session = self.sessions.get(session_id)
            if session:
                session.complete(success, error_message)
                logger.info(f'Session terminee (memoire): {session_id} (statut: {session.status})')

    def set_total_rows(self, session_id: str, total_rows: int):
        """Thread-safe way to update total_rows for a session"""
        redis_client = self._get_redis()

        if redis_client:
            try:
                raw = redis_client.get(self._redis_key(session_id))
                if raw:
                    data = json.loads(raw)
                    data['total_rows'] = total_rows
                    redis_client.setex(
                        self._redis_key(session_id),
                        REDIS_TTL,
                        json.dumps(data)
                    )
                    return
            except Exception as e:
                logger.warning(f"Redis set_total_rows failed: {e}")

        # Fallback: in-memory
        with self.session_lock:
            session = self.sessions.get(session_id)
            if session:
                session.total_rows = total_rows

    def _data_to_progress(self, data: dict) -> ImportProgress:
        """Convert Redis JSON data to an ImportProgress object for SSE compatibility."""
        session_id = data['session_id']
        total_rows = data.get('total_rows', 0)
        progress = ImportProgress(session_id, total_rows)
        progress.current_row = data.get('current_row', 0)
        progress.current_step = data.get('current_step', 'Initialisation')
        progress.status = data.get('status', 'running')
        progress.error_message = data.get('error_message')
        progress.messages = data.get('messages', [])

        started_at = data.get('started_at')
        if started_at:
            try:
                progress.started_at = datetime.fromisoformat(started_at)
            except (ValueError, TypeError):
                pass

        completed_at = data.get('completed_at')
        if completed_at:
            try:
                progress.completed_at = datetime.fromisoformat(completed_at)
            except (ValueError, TypeError):
                pass

        return progress

    def _cleanup_old_sessions(self):
        """Cleanup old in-memory sessions only (Redis handles TTL automatically)."""
        now = datetime.utcnow()
        if (now - self.last_cleanup).total_seconds() < self.cleanup_interval:
            return

        cutoff = now - timedelta(hours=2)
        to_remove = []

        for session_id, session in self.sessions.items():
            if session.completed_at and session.completed_at < cutoff:
                to_remove.append(session_id)

        for session_id in to_remove:
            del self.sessions[session_id]

        if to_remove:
            logger.info(f'Nettoyage: {len(to_remove)} sessions expirees supprimees')

        self.last_cleanup = now


progress_manager = ImportProgressManager()


@import_progress_bp.route('/import/progress/<session_id>')
@login_required
def stream_import_progress(session_id: str):
    """Server-Sent Events endpoint for real-time import progress updates"""

    def generate():
        session = progress_manager.get_session(session_id)
        if not session:
            yield f"data: {json.dumps({'error': 'Session not found'})}\n\n"
            return

        # Support up to 30 minutes of import time (3600 iterations x 0.5s)
        max_iterations = 3600
        iteration = 0

        while iteration < max_iterations:
            session = progress_manager.get_session(session_id)
            if not session:
                break

            data = session.to_dict()
            yield f"data: {json.dumps(data)}\n\n"

            if session.status in ['completed', 'error']:
                # Send final status and close
                break

            time.sleep(0.5)
            iteration += 1

        # Send final heartbeat if still running (timed out)
        if session and session.status == 'running':
            timeout_data = {'timeout': True, 'message': 'Import still running but SSE timed out'}
            yield f"data: {json.dumps(timeout_data)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )
