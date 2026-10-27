from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, Text, Boolean, DateTime, JSON, ForeignKey, UniqueConstraint, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = 'accounts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    remote_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    account_type: Mapped[str] = mapped_column(String(32))
    allowlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint('platform', 'remote_id'),)


class Capability(Base):
    __tablename__ = 'capabilities'
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    supported: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Credential(Base):
    __tablename__ = 'credentials'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'), unique=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Post(Base):
    __tablename__ = 'posts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    remote_id: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint('account_id', 'remote_id'),)


class Media(Base):
    __tablename__ = 'media'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    requested_timezone: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default='pending_mock')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    authorization_subject: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    subject: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Idempotency(Base):
    __tablename__ = 'idempotency_records'
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Operation(Base):
    __tablename__ = 'operations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthFlow(Base):
    __tablename__ = 'oauth_flows'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    state_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    started: Mapped[bool] = mapped_column(Boolean, default=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Publication(Base):
    __tablename__ = 'publications'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint('principal', 'idempotency_key'),)


class PublicationTarget(Base):
    __tablename__ = 'publication_targets'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    publication_id: Mapped[str] = mapped_column(ForeignKey('publications.id'))
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    state: Mapped[str] = mapped_column(String(32), default='prepared')
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(256))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint('publication_id', 'account_id'),)


class PublicationApproval(Base):
    __tablename__ = 'publication_approvals'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    publication_id: Mapped[str] = mapped_column(ForeignKey('publications.id'), unique=True)
    principal: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[str] = mapped_column(String(128))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    channel: Mapped[str] = mapped_column(String(32), default='owner_cli', server_default='owner_cli')
    oauth_client_id: Mapped[str | None] = mapped_column(String(128))


class ApprovalPortalSession(Base):
    __tablename__ = 'approval_portal_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    owner: Mapped[str] = mapped_column(String(128))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PublicationAsset(Base):
    __tablename__ = 'publication_assets'
    publication_id: Mapped[str] = mapped_column(ForeignKey('publications.id'), primary_key=True)
    media_id: Mapped[str] = mapped_column(ForeignKey('media.id'), primary_key=True)


class OwnerOIDCFlow(Base):
    __tablename__ = 'owner_oidc_flows'
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
