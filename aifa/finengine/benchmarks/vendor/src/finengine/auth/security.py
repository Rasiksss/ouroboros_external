"""Password hashing (bcrypt) + JWT encode/decode."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from finengine.config import settings

_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_ALGO = "HS256"


def hash_password(password: str) -> str:
    return _ctx.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ctx.verify(password, hashed)
    except Exception:
        return False


def create_access_token(user_id: int, *, ttl_days: Optional[int] = None) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=ttl_days or settings.auth_token_ttl_days)
    payload = {"sub": str(user_id), "iat": int(now.timestamp()), "exp": int(exp.timestamp())}
    return jwt.encode(payload, settings.auth_secret_key, algorithm=_ALGO)


def decode_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, settings.auth_secret_key, algorithms=[_ALGO])
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except (JWTError, ValueError, TypeError):
        return None
