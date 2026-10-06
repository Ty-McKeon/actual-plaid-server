"""Database models and encryption utilities for Plaid configuration and items."""

import base64
import binascii
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.types import String, TypeDecorator

# Initialize SQLAlchemy database instance
db = SQLAlchemy()


def _format_fernet_key(key: bytes) -> bytes:
    """Ensure the key is 32 url-safe base64-encoded bytes."""
    try:
        if len(base64.urlsafe_b64decode(key)) == 32:
            return key
    except (binascii.Error, ValueError):
        pass

    # If the provided key is already 32 raw bytes, base64-encode it
    if len(key) == 32:
        return base64.urlsafe_b64encode(key)

    raise RuntimeError(
        "ENCRYPTION_KEY must be a Fernet key (32 url-safe base64-encoded bytes) "
        "or exactly 32 raw bytes."
    )


def load_master_key() -> bytes:
    """Load the symmetric encryption key used for securing sensitive database fields.

    Resolution order:
      1. Container / Docker secret file at `/run/secrets/encryption_key`
      2. Environment variable `ENCRYPTION_KEY`
      3. Insecure hardcoded development key (only permitted if `FLASK_DEBUG` / `FLASK_ENV` is dev)

    Raises:
        RuntimeError: If no encryption key can be located outside debug mode.

    Returns:
        bytes: URL-safe base64-encoded 32-byte key for Fernet.
    """
    # 1. Check for secret file mounted in Docker/Kubernetes
    secret_path = Path("/run/secrets/encryption_key")
    if secret_path.is_file():
        return _format_fernet_key(secret_path.read_text().strip().encode())

    # 2. Check for key supplied via environment variable
    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key:
        return _format_fernet_key(env_key.strip().encode())

    # 3. Fallback dummy key for local development only
    if (
        os.getenv("FLASK_DEBUG") in ("1", "true", "True")
        or os.getenv("FLASK_ENV") == "development"
        or os.getenv("DEBUG") in ("1", "true", "True")
    ):
        return base64.urlsafe_b64encode(b"dev-insecure-master-key-32bytes!")

    # Fail fast if running in production without an encryption key configured
    raise RuntimeError("Master ENCRYPTION_KEY not found.")


# Initialize Fernet cipher with the loaded master encryption key
fernet = Fernet(load_master_key())


class EncryptedString(TypeDecorator):
    """SQLAlchemy custom type that transparently encrypts and decrypts string values.

    Values are encrypted using Fernet symmetric encryption before being written to
    the database and decrypted back to plaintext upon retrieval.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Encrypt plaintext string before saving to database."""
        return fernet.encrypt(value.encode()).decode() if value else None

    def process_result_value(self, value, dialect):
        """Decrypt ciphertext from database back to plaintext string."""
        return fernet.decrypt(value.encode()).decode() if value else None


class UserPlaidConfigs(db.Model):
    """User configuration and credentials for the Plaid API.

    Stores authentication metadata and user-specific Plaid credentials.
    """

    # Cloudflare Access Subject (`sub` claim) identifying the authenticated user
    user_id = db.Column(db.String(128), primary_key=True)

    # User's email address from authentication headers
    user_email = db.Column(db.String(255), nullable=False)

    # Plaid client ID if configured on a per-user basis
    plaid_client_id = db.Column(db.String(128), nullable=True)

    # Encrypted Plaid API secret key
    plaid_secret = db.Column(EncryptedString(512), nullable=True)

    # Plaid environment to target: 'sandbox', 'development', or 'production'
    plaid_env = db.Column(db.String(32), default="sandbox")

    def to_dict(self) -> dict:
        """Serializes the record to a dictionary safe for API responses."""
        return {
            "user_id": self.user_id,
            "client_id": self.plaid_client_id,
            "env": self.plaid_env,
            "has_secret": bool(self.plaid_secret),
            # Never return raw secrets to the frontend
            "secret_preview": f"••••{self.plaid_secret[-4:]}"
            if self.plaid_secret
            else None,
        }

    @classmethod
    def get_dict_for_user(cls, user_id: str) -> dict | None:
        """Convenience query method returning the serialized user dict."""
        record = cls.query.filter_by(user_id=user_id).first()
        return record.to_dict() if record else None


class PlaidItems(db.Model):
    """Represents a linked financial institution (Plaid Item) associated with a user.

    A single user can link multiple institutions, each producing a distinct Plaid Item.
    """

    # Unique identifier for the local record
    id = db.Column(db.Integer, primary_key=True)

    # User ID referencing the owner in UserPlaidConfig
    user_id = db.Column(
        db.String(128), db.ForeignKey("user_plaid_configs.user_id"), nullable=False
    )

    # Plaid-assigned item identifier returned upon public token exchange
    item_id = db.Column(db.String(128), unique=True, nullable=False)

    # Plaid financial institution identifier (e.g. 'ins_109508')
    institution_id = db.Column(db.String(64), nullable=True)

    # Human-readable name of the bank or financial institution
    institution_name = db.Column(db.String(255), nullable=True)

    # Encrypted access token used to query accounts and transactions for this Item
    access_token = db.Column(EncryptedString(512), nullable=False)


# How long a setup token may sit unclaimed before it stops being accepted
SETUP_TOKEN_TTL = timedelta(hours=24)

# Marks a stored SimpleFIN password as hashed (older rows were stored in plaintext)
SIMPLEFIN_HASH_PREFIX = "sha256$"


def hash_simplefin_password(password: str) -> str:
    """Hashes a SimpleFIN access password for storage.

    Passwords are 256-bit random tokens rather than user-chosen secrets, so a
    fast unsalted digest is sufficient to keep them unrecoverable from the DB.
    """
    return SIMPLEFIN_HASH_PREFIX + hashlib.sha256(password.encode()).hexdigest()


class SimpleFinCredentials(db.Model):
    """
    User configuration and credentials for the SimpleFIN API.

    Stores authentication metadata and user-specific SimpleFIN credentials.
    """

    # 1. Internal surrogate PK
    id = db.Column(db.Integer, primary_key=True)

    # 2. Scoped to the user (indexed, not unique)
    user_id = db.Column(db.String, nullable=False, index=True)

    # 3. Lookup tokens (indexed & unique for fast resolution)
    claim_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    username = db.Column(db.String(64), unique=True, nullable=True, index=True)
    # Hash of the access password (see `hash_simplefin_password`), never the plaintext
    password = db.Column(db.String(128), nullable=True)

    # 4. Lifecycle state
    is_claimed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @staticmethod
    def _setup_token_cutoff() -> datetime:
        """Creation time before which an unclaimed setup token has expired (naive UTC)."""
        return datetime.now(timezone.utc).replace(tzinfo=None) - SETUP_TOKEN_TTL

    @property
    def is_expired(self) -> bool:
        """Whether this is a setup token that went unclaimed past its lifetime."""
        if self.is_claimed:
            return False
        if self.created_at is None:
            return True
        created_at = self.created_at
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        return created_at < self._setup_token_cutoff()

    @classmethod
    def purge_expired(cls) -> None:
        """Deletes expired unclaimed setup tokens. The caller commits."""
        cls.query.filter(
            cls.is_claimed.is_(False),
            db.or_(cls.created_at.is_(None), cls.created_at < cls._setup_token_cutoff()),
        ).delete(synchronize_session=False)

    def check_password(self, password: str) -> bool:
        """Constant-time comparison of a presented password against the stored hash."""
        if not self.password:
            return False
        return secrets.compare_digest(
            self.password.encode(), hash_simplefin_password(password).encode()
        )


def hash_legacy_simplefin_passwords() -> None:
    """Hashes any SimpleFIN passwords written in plaintext by older versions."""
    legacy = SimpleFinCredentials.query.filter(
        SimpleFinCredentials.password.isnot(None),
        SimpleFinCredentials.password.notlike(f"{SIMPLEFIN_HASH_PREFIX}%"),
    ).all()
    for credentials in legacy:
        credentials.password = hash_simplefin_password(credentials.password)
    if legacy:
        db.session.commit()
