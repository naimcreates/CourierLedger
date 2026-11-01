"""Object-storage abstraction (Phase 2, Part 7).

Uploaded file bytes are referenced by the ``file_objects`` table
through a ``storage_key``; only metadata + SHA-256 live in the
database. This module is the single place that turns a storage key
into bytes, so a private object-storage backend (S3-compatible,
GCS, ...) can be swapped in later WITHOUT changing call sites:
implement ``StorageBackend`` and point ``get_storage_backend()`` at
it. No provider is selected here and no S3 SDK is added.

HONEST LABEL: the only backend shipped today is
``LocalFilesystemBackend`` — plain files under
``backend/var/imports/``. That is NOT production object storage:
no replication, no encryption at rest beyond the host disk, no
lifecycle policy, and availability is tied to one machine. It is
kept only because the M1 local-dev build needs somewhere to put
uploaded CSV bytes while remaining private-by-default (files are
never served back over HTTP — there is no download endpoint — and
every retrieve is tenant-checked, see ``retrieve``).

Security properties the abstraction enforces for EVERY backend:
- Tenant isolation: a storage key embeds the owning merchant id;
  ``retrieve``/``delete``/``metadata`` refuse a key whose merchant
  segment does not match the requesting merchant
  (``StorageAccessDenied``). A caller must still check workspace
  membership first (the API layer does); this is the second line
  of defence, not a replacement.
- No path traversal: keys are validated against a strict pattern
  before any filesystem path is built; the resolved path is
  checked to stay under the backend root.
- Size cap: ``store`` refuses objects over ``MAX_OBJECT_BYTES``
  (10 MB, matching the upload cap in ``app/api/imports.py``).
- Integrity: ``store`` verifies the bytes hash to the claimed
  SHA-256 before writing.

Retention: ``file_objects.purged_at`` exists for a future purge
job; no purge job exists yet and ``delete`` is only called by
such a job, never by the import path.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Same root the importer has always used (backend/var/imports/).
# ``var/`` is git-ignored; uploaded bytes are never committed.
DEFAULT_STORAGE_ROOT = Path(__file__).resolve().parent.parent.parent / "var" / "imports"

# 10 MB — matches MAX_UPLOAD_BYTES in app/api/imports.py. The API
# enforces it while streaming; the backend enforces it again here
# so no other caller can store a larger object by accident.
MAX_OBJECT_BYTES = 10 * 1024 * 1024

# Keys look like: local://<merchant-uuid>/<sha256-hex>.csv
_KEY_RE = re.compile(r"^local://([0-9a-fA-F-]{36})/([0-9a-f]{64})\.csv$")


class StorageError(Exception):
    """Base class for storage-backend failures."""


class StorageNotFound(StorageError):
    """No object exists for this key (in this tenant)."""


class StorageAccessDenied(StorageError):
    """The key belongs to a different merchant, or is malformed."""


class StorageTooLarge(StorageError):
    """The object exceeds MAX_OBJECT_BYTES."""


@dataclass(frozen=True)
class StoredObject:
    """Metadata for one stored object (no bytes)."""

    storage_key: str
    sha256: str
    size_bytes: int
    content_type: str


class StorageBackend(Protocol):
    """Minimal interface every storage backend must implement.

    ``merchant_id`` is always the workspace (merchant) id of the
    caller; backends MUST refuse cross-merchant access.
    """

    def store(
        self,
        merchant_id: uuid.UUID,
        *,
        sha256: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Persist ``data`` and return its storage key.

        Raises StorageTooLarge over MAX_OBJECT_BYTES, ValueError
        when the bytes do not hash to ``sha256``.
        """
        ...

    def retrieve(self, merchant_id: uuid.UUID, storage_key: str) -> bytes:
        """Return the stored bytes.

        Raises StorageAccessDenied for a key owned by another
        merchant (or a malformed key), StorageNotFound when the
        object does not exist.
        """
        ...

    def delete(self, merchant_id: uuid.UUID, storage_key: str) -> None:
        """Delete the stored object (and its metadata sidecar).

        Raises StorageAccessDenied / StorageNotFound as retrieve.
        """
        ...

    def metadata(self, merchant_id: uuid.UUID, storage_key: str) -> StoredObject:
        """Return metadata without reading the object bytes.

        Raises StorageAccessDenied / StorageNotFound as retrieve.
        """
        ...


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalFilesystemBackend:
    """Local-filesystem backend: content-addressed files under root.

    Layout: ``<root>/<merchant-uuid>/<sha256>.csv`` plus a
    ``<sha256>.csv.meta.json`` sidecar holding the declared content
    type (the bytes file itself carries no content-type metadata).
    Files written before sidecars existed read back as
    content_type "unknown". This backend is NOT production object
    storage — see the module docstring.
    """

    def __init__(self, root: Path | str = DEFAULT_STORAGE_ROOT):
        self.root = Path(root)

    # -- key handling -------------------------------------------------

    def _parse_key(self, merchant_id: uuid.UUID, storage_key: str) -> tuple[str, str]:
        """Validate a key and return (merchant segment, sha256).

        Raises StorageAccessDenied for anything malformed or owned
        by another merchant — never let a crafted key reach the
        filesystem.
        """
        match = _KEY_RE.match(storage_key or "")
        if match is None:
            raise StorageAccessDenied("Malformed storage key.")
        key_merchant, sha256 = match.groups()
        if key_merchant.lower() != str(merchant_id).lower():
            raise StorageAccessDenied("Storage key belongs to another merchant.")
        return key_merchant, sha256

    def _resolve(self, merchant_id: uuid.UUID, storage_key: str) -> tuple[Path, str]:
        """Return (object path, sha256) for a validated key.

        Defence in depth: even after the regex check, the resolved
        path must stay under the backend root.
        """
        _, sha256 = self._parse_key(merchant_id, storage_key)
        path = (self.root / str(merchant_id) / f"{sha256}.csv").resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise StorageAccessDenied("Storage key escapes the storage root.")
        return path, sha256

    @staticmethod
    def _meta_path(path: Path) -> Path:
        return path.with_suffix(".csv.meta.json")

    # -- StorageBackend interface -------------------------------------

    def store(
        self,
        merchant_id: uuid.UUID,
        *,
        sha256: str,
        data: bytes,
        content_type: str,
    ) -> str:
        if len(data) > MAX_OBJECT_BYTES:
            raise StorageTooLarge(
                f"Object is {len(data)} bytes; the limit is {MAX_OBJECT_BYTES}."
            )
        if _sha256_hex(data) != sha256.lower():
            raise ValueError("Data does not hash to the claimed SHA-256.")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256.lower()):
            raise ValueError("sha256 must be 64 lowercase hex characters.")
        storage_key = f"local://{merchant_id}/{sha256.lower()}.csv"
        target = self.root / str(merchant_id) / f"{sha256.lower()}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)  # content-addressed; re-upload rewrites identical bytes
        self._meta_path(target).write_text(
            json.dumps({"content_type": content_type}), encoding="utf-8"
        )
        return storage_key

    def retrieve(self, merchant_id: uuid.UUID, storage_key: str) -> bytes:
        path, _ = self._resolve(merchant_id, storage_key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise StorageNotFound(f"No stored object for key {storage_key!r}.") from None

    def delete(self, merchant_id: uuid.UUID, storage_key: str) -> None:
        path, _ = self._resolve(merchant_id, storage_key)
        try:
            path.unlink()
        except FileNotFoundError:
            raise StorageNotFound(f"No stored object for key {storage_key!r}.") from None
        self._meta_path(path).unlink(missing_ok=True)

    def metadata(self, merchant_id: uuid.UUID, storage_key: str) -> StoredObject:
        path, sha256 = self._resolve(merchant_id, storage_key)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise StorageNotFound(f"No stored object for key {storage_key!r}.") from None
        content_type = "unknown"
        try:
            sidecar = json.loads(self._meta_path(path).read_text(encoding="utf-8"))
            if isinstance(sidecar, dict) and isinstance(sidecar.get("content_type"), str):
                content_type = sidecar["content_type"]
        except (FileNotFoundError, ValueError, OSError):
            content_type = "unknown"
        return StoredObject(
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=size,
            content_type=content_type,
        )


def get_storage_backend(root: Path | str | None = None) -> StorageBackend:
    """Return the configured storage backend.

    Today this is always the local-filesystem backend. When a
    private object-storage provider is chosen (a founder decision,
    not made here), this factory is the one place that changes:
    return the new backend and leave every call site untouched.
    """
    return LocalFilesystemBackend(root or DEFAULT_STORAGE_ROOT)
