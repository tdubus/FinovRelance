import secrets
import hashlib
from datetime import datetime, timedelta
from app import db


class PasswordSetupToken(db.Model):
    __tablename__ = 'password_setup_tokens'

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    is_used = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship('User', backref=db.backref('setup_tokens', lazy=True))
    company = db.relationship('Company', backref=db.backref('setup_tokens', lazy=True, cascade='all, delete-orphan'))

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @classmethod
    def create_token(cls, user_id, company_id, ttl_hours=48):
        raw_token = secrets.token_urlsafe(32)
        token_record = cls(
            token_hash=cls.hash_token(raw_token),
            user_id=user_id,
            company_id=company_id,
            expires_at=datetime.utcnow() + timedelta(hours=ttl_hours)
        )
        db.session.add(token_record)
        return raw_token, token_record

    @classmethod
    def verify_token(cls, raw_token):
        token_hash = cls.hash_token(raw_token)
        record = cls.query.filter_by(token_hash=token_hash, is_used=False).first()
        if not record:
            return None, 'invalid'
        if record.expires_at < datetime.utcnow():
            return None, 'expired'
        return record, 'valid'

    def mark_used(self):
        self.is_used = True
        self.used_at = datetime.utcnow()
