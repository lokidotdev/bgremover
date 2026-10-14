"""Single-admin password login with a signed, expiring session cookie."""

from __future__ import annotations

import hmac
import time

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

COOKIE = "bgr_admin"
SESSION_SECONDS = 7 * 24 * 3600


class AdminAuth:
    def __init__(self, password: str, secret_key: str):
        if not password:
            raise ValueError("an admin password is required")
        self._password = password.encode()
        self._signer = TimestampSigner(secret_key, salt="admin-session")

    def login(self, password: str) -> str | None:
        """Returns a session token for the right password, else None (after a short delay)."""
        if hmac.compare_digest(password.encode(), self._password):
            return self._signer.sign(b"admin").decode()
        time.sleep(0.5)  # blunt brute force
        return None

    def verify(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            return self._signer.unsign(token, max_age=SESSION_SECONDS) == b"admin"
        except (BadSignature, SignatureExpired):
            return False
