"""Brain feed authentication using HMAC-signed tokens.

This module provides token-based authentication for the Brain feed dashboard.
Users authenticate with an access code and receive a long-lived HMAC-signed
token for WebSocket connections.

Security Model:
    1. User enters access code on the dashboard
    2. Code is verified against BRAIN_ACCESS_CODE
    3. If valid, server returns signed token with 30-day expiry
    4. Token is stored in browser localStorage
    5. Token is sent as query parameter on WebSocket connections

Token Format:
    {expiry_timestamp}:{hmac_signature}
    Example: "1735689600:a1b2c3d4e5f67890"

Environment Variables:
    BRAIN_ACCESS_CODE: The access code users must enter
    BRAIN_SECRET: Secret key for HMAC signing (keep secure!)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

# =============================================================================
# Configuration
# =============================================================================

BRAIN_ACCESS_CODE: str = os.environ.get("BRAIN_ACCESS_CODE", "")
"""Access code required to authenticate with the brain feed."""

BRAIN_SECRET: str = os.environ.get("BRAIN_SECRET", "")
"""Secret key for HMAC token signing. Keep this secure!"""

TOKEN_LIFETIME: int = 60 * 60 * 24 * 30
"""Token validity period in seconds (30 days)."""


# =============================================================================
# Authentication Functions
# =============================================================================


def is_configured() -> bool:
    """Check if brain authentication is properly configured.

    Returns:
        True if both BRAIN_ACCESS_CODE and BRAIN_SECRET are set.
    """
    return bool(BRAIN_ACCESS_CODE and BRAIN_SECRET)


def verify_code(code: str) -> bool:
    """Verify the user-provided access code.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        code: The access code provided by the user.

    Returns:
        True if code matches BRAIN_ACCESS_CODE.
    """
    if not BRAIN_ACCESS_CODE:
        return False
    return hmac.compare_digest(code, BRAIN_ACCESS_CODE)


def generate_token() -> str:
    """Generate a signed authentication token.

    Creates a token containing an expiry timestamp and HMAC signature.
    Token format: "{expiry_timestamp}:{hmac_signature}"

    Returns:
        Signed token string valid for TOKEN_LIFETIME seconds.
    """
    expires = int(time.time()) + TOKEN_LIFETIME
    payload = f"brain:{expires}"
    signature = hmac.new(
        BRAIN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return f"{expires}:{signature}"


def verify_token(token: str) -> bool:
    """Verify a token's signature and check expiry.

    Uses constant-time comparison for the signature check.

    Args:
        token: Token string in format "{expiry}:{signature}".

    Returns:
        True if token is valid and not expired.
    """
    if not BRAIN_SECRET:
        return False
    try:
        expires_str, signature = token.split(":", 1)
        expires = int(expires_str)

        # Check expiry
        if time.time() > expires:
            return False

        # Check signature using constant-time comparison
        payload = f"brain:{expires}"
        expected = hmac.new(
            BRAIN_SECRET.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        return hmac.compare_digest(signature, expected)
    except (ValueError, AttributeError):
        return False
