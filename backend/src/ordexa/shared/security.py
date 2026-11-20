"""Password hashing (argon2id) and access tokens (JWT, HS256)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from ordexa.config import get_settings
from ordexa.shared.db import utcnow
from ordexa.shared.errors import ErrorCode, OrdexaError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def issue_token(*, user_id: uuid.UUID, tenant_id: uuid.UUID, perm_version: int) -> tuple[str, int]:
    s = get_settings()
    now = utcnow()
    exp = now + timedelta(minutes=s.jwt_ttl_minutes)
    token = jwt.encode(
        {
            "sub": str(user_id),
            "tid": str(tenant_id),
            "pv": perm_version,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        },
        s.jwt_secret,
        algorithm="HS256",
    )
    return token, int(exp.timestamp())


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise OrdexaError(ErrorCode.AUTHENTICATION_REQUIRED, "invalid or expired token") from exc
