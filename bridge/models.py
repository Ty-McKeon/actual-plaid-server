"""Database models and encryption utilities for Plaid configuration and items."""

import base64
import os
from pathlib import Path
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.types import TypeDecorator, String
from cryptography.fernet import Fernet

# Initialize SQLAlchemy database instance
db = SQLAlchemy()

def _format_fernet_key(key: bytes) -> bytes:
    """Ensure the key is 32 url-safe base64-encoded bytes."""
    try:
        decoded = base64.urlsafe_b64decode(key)
        if len(decoded) == 32:
            return key
    except Exception:
        pass
    # If the provided key is already 32 raw bytes, base64-encode it
    if len(key) == 32:
        return base64.urlsafe_b64encode(key)
    return key


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