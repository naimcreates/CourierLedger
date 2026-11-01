"""In-memory rate limiting (M1, simple and documented).

Login rule: at most ``settings.login_max_failures`` failed logins per
(IP + lower-cased email) within ``settings.login_window_seconds``.
A further attempt inside the window is rejected with HTTP 429.
Phase 1 adds a second, per-account login bucket (same limits, keyed
on the email alone) so rotating IPs does not defeat the limit, and a
generic per-scope bucket used by register, password-reset request and
member invites (per IP / per workspace, with their own limits).

Limitations, stated plainly: this is process-local memory, so it does
not survive a restart and does not share state across workers. That is
acceptable for M0/M1 single-process dev; a multi-worker deployment
must replace it with a shared store (e.g. Redis) before production.
``X-Forwarded-For`` is NOT read here: the login limiter keys on
``request.client.host`` (the direct peer). Behind a reverse proxy that
peer is the proxy unless uvicorn runs with ``--proxy-headers`` and a
trusted ``--forwarded-allow-ips`` list — see the production deployment
reference (PRODUCTION_SECRETS_GUIDE.md / SECURITY_REMEDIATION.md).
Successful login clears the key's failure history.
"""

import time
from collections import defaultdict

from app.config import settings

_attempts: dict[str, list[float]] = defaultdict(list)
# Phase 1: per-account login bucket (email only, no IP).
_account_attempts: dict[str, list[float]] = defaultdict(list)
# Phase 1: generic scopes (register / reset-request / invite).
_generic_attempts: dict[str, list[float]] = defaultdict(list)


def _key(client_ip: str, email: str) -> str:
    return f"{client_ip}|{email.strip().lower()}"


def _prune(store: dict[str, list[float]], key: str, now: float, window: float) -> None:
    window_start = now - window
    store[key] = [t for t in store[key] if t > window_start]
    if not store[key]:
        store.pop(key, None)


def is_blocked(client_ip: str, email: str) -> bool:
    """True when either the (IP+email) bucket or the per-account
    (email-only) bucket is at the login limit."""
    now = time.monotonic()
    window = settings.login_window_seconds
    key = _key(client_ip, email)
    _prune(_attempts, key, now, window)
    if len(_attempts.get(key, [])) >= settings.login_max_failures:
        return True
    account_key = email.strip().lower()
    _prune(_account_attempts, account_key, now, window)
    return len(_account_attempts.get(account_key, [])) >= settings.login_max_failures


def record_failure(client_ip: str, email: str) -> None:
    now = time.monotonic()
    window = settings.login_window_seconds
    key = _key(client_ip, email)
    _prune(_attempts, key, now, window)
    _attempts[key].append(now)
    account_key = email.strip().lower()
    _prune(_account_attempts, account_key, now, window)
    _account_attempts[account_key].append(now)


def record_success(client_ip: str, email: str) -> None:
    _attempts.pop(_key(client_ip, email), None)
    _account_attempts.pop(email.strip().lower(), None)


def is_limited(scope: str, key: str, max_attempts: int, window_seconds: int) -> bool:
    """Generic bucket check: ``scope|key`` has >= max_attempts recorded
    inside the window. Used for register / password-reset request /
    member invites (Phase 1, B-F5/B-F9)."""
    now = time.monotonic()
    full_key = f"{scope}|{key}"
    _prune(_generic_attempts, full_key, now, window_seconds)
    return len(_generic_attempts.get(full_key, [])) >= max_attempts


def record_attempt(scope: str, key: str, max_attempts: int, window_seconds: int) -> None:
    now = time.monotonic()
    full_key = f"{scope}|{key}"
    _prune(_generic_attempts, full_key, now, window_seconds)
    _generic_attempts[full_key].append(now)


def reset_for_tests() -> None:
    """Test helper only."""
    _attempts.clear()
    _account_attempts.clear()
    _generic_attempts.clear()
