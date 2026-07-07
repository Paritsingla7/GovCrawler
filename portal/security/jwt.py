"""JWT access tokens + opaque refresh tokens.

Secret and TTLs are read from config.auth (see portal/api/server.py, which
generates+persists a random jwt_secret on first run if empty).
"""
import hashlib
import secrets
import time

import jwt as pyjwt


def create_access_token(user_id: int, token_version: int, secret: str, ttl_minutes: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "tv": token_version,
        "type": "access",
        "iat": now,
        "exp": now + ttl_minutes * 60,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    """Raises jwt.PyJWTError (or a subclass) on any invalid/expired token."""
    return pyjwt.decode(token, secret, algorithms=["HS256"])


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
