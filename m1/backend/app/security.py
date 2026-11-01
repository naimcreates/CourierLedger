"""Password hashing and session-token helpers (M1 auth).

- Passwords: Argon2id via argon2-cffi, time_cost=3, memory_cost=65536 KiB,
  parallelism=4 (OWASP-recommended parameters for Argon2id).
- Sessions: opaque random tokens (secrets.token_urlsafe, 256 bits of
  entropy). Only the SHA-256 hash of a token is stored in the database;
  the raw token is never persisted or logged. Comparisons use
  hmac.compare_digest (constant time).
"""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Argon2id parameters (documented; OWASP recommendation for Argon2id).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 65536
ARGON2_PARALLELISM = 4

_password_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
)


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id. Never store the result
    of this next to a plaintext password."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True only if the password matches the stored hash.
    Any malformed hash or mismatch returns False (never raises)."""
    try:
        return bool(_password_hasher.verify(password_hash, password))
    except (VerifyMismatchError, Exception):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return bool(_password_hasher.check_needs_rehash(password_hash))
    except Exception:
        return False


def generate_session_token() -> str:
    """256-bit opaque random session token, URL-safe base64."""
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def generate_password_reset_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token. This is the only form stored in
    the database (user_sessions.token_hash, password_reset_tokens)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison for hashes / CSRF tokens."""
    return hmac.compare_digest(a, b)
