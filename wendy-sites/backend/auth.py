"""Brain feed authentication using HMAC-signed tokens."""

import hashlib
import hmac
import os
import time

BRAIN_ACCESS_CODE = os.environ.get("BRAIN_ACCESS_CODE", "")
BRAIN_SECRET = os.environ.get("BRAIN_SECRET", "")
TOKEN_LIFETIME = 60 * 60 * 24 * 30  # 30 days


def is_configured() -> bool:
    """Check if brain auth is configured."""
    return bool(BRAIN_ACCESS_CODE and BRAIN_SECRET)


def verify_code(code: str) -> bool:
    """Verify the access code."""
    if not BRAIN_ACCESS_CODE:
        return False
    return hmac.compare_digest(code, BRAIN_ACCESS_CODE)


def generate_token() -> str:
    """Generate a signed token with expiry."""
    expires = int(time.time()) + TOKEN_LIFETIME
    payload = f"brain:{expires}"
    signature = hmac.new(
        BRAIN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return f"{expires}:{signature}"


def verify_token(token: str) -> bool:
    """Verify token signature and expiry."""
    if not BRAIN_SECRET:
        return False
    try:
        expires_str, signature = token.split(":", 1)
        expires = int(expires_str)

        # Check expiry
        if time.time() > expires:
            return False

        # Check signature
        payload = f"brain:{expires}"
        expected = hmac.new(
            BRAIN_SECRET.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        return hmac.compare_digest(signature, expected)
    except (ValueError, AttributeError):
        return False
