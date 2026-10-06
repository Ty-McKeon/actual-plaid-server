"""Cloudflare Zero Trust JWT authentication middleware.

Validates the `Cf-Access-Jwt-Assertion` header provided by Cloudflare Access
for protected dashboard and API endpoints, extracting user identity claims.
"""

import logging
import os
from functools import wraps
from typing import Any

import jwt
from flask import g, jsonify, request
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

DEFAULT_DEV_USER_ID = "7335d417-61da-459d-899c-0a01c76a2f94"
DEFAULT_DEV_EMAIL = "user@example.com"

# Cache for PyJWKClient instances by team domain
_jwks_clients: dict[str, PyJWKClient] = {}


def is_development_mode() -> bool:
    """Checks whether the application is running in local development mode."""
    return (
        os.getenv("FLASK_DEBUG") in ("1", "true", "True")
        or os.getenv("FLASK_ENV") == "development"
        or os.getenv("DEBUG") in ("1", "true", "True")
    )


def get_access_token() -> str | None:
    """Extracts Cloudflare Access JWT token from request headers or cookies.

    Resolution order:
      1. `Cf-Access-Jwt-Assertion` request header
      2. `CF_Authorization` cookie (browser requests)
      3. `Authorization: Bearer <token>` header
    """
    assertion = request.headers.get("Cf-Access-Jwt-Assertion")
    if assertion:
        return assertion.strip()

    cookie_token = request.cookies.get("CF_Authorization")
    if cookie_token:
        return cookie_token.strip()

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return None


def get_normalized_team_domain() -> str | None:
    """Normalizes the Cloudflare team domain environment variable.

    Returns:
        Hostname string (e.g. 'myteam.cloudflareaccess.com') or None.
    """
    raw = os.getenv("CLOUDFLARE_TEAM_DOMAIN", "").strip()
    if not raw:
        return None

    domain = raw.replace("https://", "").replace("http://", "").rstrip("/")
    if not domain.endswith(".cloudflareaccess.com"):
        domain = f"{domain}.cloudflareaccess.com"
    return domain


def get_jwks_client(team_domain: str) -> PyJWKClient:
    """Retrieves or creates a cached PyJWKClient for the given team domain."""
    if team_domain not in _jwks_clients:
        certs_url = f"https://{team_domain}/cdn-cgi/access/certs"
        _jwks_clients[team_domain] = PyJWKClient(
            certs_url,
            cache_jwk_set=True,
            lifespan=3600,
        )
    return _jwks_clients[team_domain]


def validate_cloudflare_jwt(token: str) -> dict[str, Any]:
    """Cryptographically verifies a Cloudflare Access JWT and returns its claims.

    Args:
        token: Raw JWT string from Cloudflare Access.

    Returns:
        Decoded claims dictionary containing 'sub', 'email', etc.

    Raises:
        jwt.PyJWTError: If signature, expiration, audience, or issuer validation fails.
        ValueError: If production verification is attempted without the team domain
            or application audience (AUD) tag configured.
    """
    team_domain = get_normalized_team_domain()
    expected_aud = (
        os.getenv("CLOUDFLARE_AUD")
        or os.getenv("CLOUDFLARE_AUDIENCE")
        or os.getenv("POLICY_AUD")
    )

    if team_domain:
        jwks_client = get_jwks_client(team_domain)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": f"https://{team_domain}",
            "options": {"require": ["exp", "iss", "sub"]},
        }
        if expected_aud:
            decode_kwargs["audience"] = expected_aud
        elif is_development_mode():
            decode_kwargs["options"]["verify_aud"] = False
        else:
            # Every Access application in a team shares signing keys, so without an
            # audience check a token issued for any other application would be accepted.
            raise ValueError(
                "CLOUDFLARE_AUD must be configured to verify Access JWTs in production."
            )

        return jwt.decode(token, signing_key.key, **decode_kwargs)

    if is_development_mode():
        logger.debug(
            "Development mode: Decoding Cloudflare JWT without signature verification."
        )
        return jwt.decode(token, options={"verify_signature": False})

    raise ValueError(
        "CLOUDFLARE_TEAM_DOMAIN must be configured to verify Access JWTs in production."
    )


def authenticate_request() -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    """Authenticates the incoming request via Cloudflare Zero Trust.

    Sets `g.user_id` and `g.email` if authentication succeeds.

    Returns:
        A tuple of `(payload_dict, None)` on success, or
        `(None, (json_response, status_code))` on authentication failure.
    """
    token = get_access_token()

    if not token:
        # Graceful fallback for local development environments
        if is_development_mode():
            dev_user_id = os.getenv("DEV_USER_ID", DEFAULT_DEV_USER_ID)
            dev_email = os.getenv("DEV_USER_EMAIL", DEFAULT_DEV_EMAIL)
            g.user_id = dev_user_id
            g.email = dev_email
            return {"sub": dev_user_id, "email": dev_email}, None

        return None, (
            jsonify(
                {
                    "error": "Unauthorized",
                    "message": "Missing Cloudflare Access assertion header (Cf-Access-Jwt-Assertion).",
                }
            ),
            401,
        )

    try:
        payload = validate_cloudflare_jwt(token)
        user_id = payload.get("sub")
        if not user_id:
            return None, (
                jsonify(
                    {
                        "error": "Unauthorized",
                        "message": "Invalid token: missing 'sub' subject claim.",
                    }
                ),
                401,
            )

        # Service tokens carry no email claim, so fall back to their common name
        email = payload.get("email") or payload.get("common_name") or ""

        g.user_id = str(user_id)
        g.email = str(email)
        return payload, None

    except jwt.ExpiredSignatureError:
        logger.warning("Cloudflare Access JWT has expired.")
        return None, (
            jsonify(
                {
                    "error": "Unauthorized",
                    "message": "Cloudflare Access token has expired.",
                }
            ),
            401,
        )
    except (jwt.PyJWTError, ValueError) as err:
        logger.warning("Cloudflare Access JWT validation failed: %s", err)
        return None, (
            jsonify(
                {
                    "error": "Unauthorized",
                    "message": "Invalid Cloudflare Access assertion.",
                }
            ),
            401,
        )
    except Exception:
        logger.exception("Unexpected error verifying Cloudflare Access token.")
        return None, (
            jsonify(
                {
                    "error": "Unauthorized",
                    "message": "Failed to validate authentication assertion.",
                }
            ),
            401,
        )


def require_cloudflare_auth(func):
    """Route decorator enforcing Cloudflare Zero Trust authentication.

    Verifies the `Cf-Access-Jwt-Assertion` header, populates `g.user_id` and `g.email`,
    and returns an HTTP 401 response if the assertion is missing or invalid.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        # The app-wide before_request hook has normally authenticated already
        if g.get("user_id") is None:
            _, err_response = authenticate_request()
            if err_response:
                return err_response
        return func(*args, **kwargs)

    return wrapper
