"""Encryption for secrets stored in the database (WhatsApp access tokens, app secrets).

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key comes from ORDEXA_SECRET_KEY, or is derived from
ORDEXA_JWT_SECRET. If that secret changes, stored integration secrets can no longer be read and must be re-entered.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from ordexa.config import get_settings


def _fernet() -> Fernet:
    material = os.environ.get("ORDEXA_SECRET_KEY") or get_settings().jwt_secret
    key = base64.urlsafe_b64encode(hashlib.sha256(("ordexa-secrets:" + material).encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None  # key changed: the admin re-enters the secret
