"""In-memory login rate limiting (M1, simple and documented).

Rule: at most ``settings.login_max_failures`` failed logins per
(IP + lower-cased email) within ``settings.login_window_seconds``.
A further attempt inside the window is rejected with HTTP 429.

Limitations, stated plainly: this is process-local memory, so it does
not survive a restart and does not share state across workers. That is
acceptable for M0/M1 single-process dev; a multi-worker deployment
must replace it with a shared store (e.g. Redis) before production.
Successful login clears the key's failure history.
"""

import time
from collections import defaultdict

from app.config import settings

_attempts: dict[str, list[float]] = defaultdict(list)


def _key(client_ip: str, email: str) -> str:
    return f"{client_ip}|{email.strip().lower()}"


def _prune(key: str, now: float) -> None:
    window_start = now - settings.login_window_seconds
    _attempts[key] = [t for t in _attempts[key] if t > window_start]
    if not _attempts[key]:
        _attempts.pop(key, None)


def is_blocked(client_ip: str, email: str) -> bool:
    now = time.monotonic()
    key = _key(client_ip, email)
    _prune(key, now)
    return len(_attempts.get(key, [])) >= settings.login_max_failures


def record_failure(client_ip: str, email: str) -> None:
    now = time.monotonic()
    key = _key(client_ip, email)
    _prune(key, now)
    _attempts[key].append(now)


def record_success(client_ip: str, email: str) -> None:
    _attempts.pop(_key(client_ip, email), None)


def reset_for_tests() -> None:
    """Test helper only."""
    _attempts.clear()
