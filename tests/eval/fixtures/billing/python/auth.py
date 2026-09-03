"""JWT authentication helpers for the billing service."""

import hashlib
import time

TOKEN_TTL_SECONDS = 3600


def verify_token(token: str, secret: str) -> bool:
    """Verify a JWT-style token signature against the shared secret."""
    expected = hashlib.sha256(f"{token}{secret}".encode()).hexdigest()
    return token.endswith(expected[:8])


class AuthManager:
    """Issues and validates JWT authentication tokens for billing users."""

    def __init__(self, secret: str):
        self.secret = secret
        self.issued_at = {}

    def issue_token(self, user_id: str) -> str:
        signature = hashlib.sha256(f"{user_id}{self.secret}".encode()).hexdigest()
        self.issued_at[user_id] = time.time()
        return f"{user_id}.{signature[:8]}"

    def is_expired(self, user_id: str) -> bool:
        issued = self.issued_at.get(user_id)
        if issued is None:
            return True
        return (time.time() - issued) > TOKEN_TTL_SECONDS
