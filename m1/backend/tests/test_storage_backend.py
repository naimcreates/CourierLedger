"""Storage-backend unit tests (Phase 2, Part 7).

Pure filesystem tests — no database, no app import beyond the
storage module — so they run against the SQLite test setup without
touching PostgreSQL. They prove the abstraction's own guarantees
(round-trip, tenant isolation, traversal refusal, size cap), NOT
that any real object-storage provider works: the only backend
under test is the local-filesystem one, which is explicitly not
production object storage.
"""

import hashlib
import uuid

import pytest

from app.services import storage
from app.services.storage import (
    LocalFilesystemBackend,
    StorageAccessDenied,
    StorageNotFound,
    StorageTooLarge,
    get_storage_backend,
)

MERCHANT_A = uuid.uuid4()
MERCHANT_B = uuid.uuid4()
DATA = b"tracking_id,amount\nTRK-1,5000\n"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture()
def backend(tmp_path):
    return LocalFilesystemBackend(tmp_path / "imports")


def test_round_trip_store_retrieve_and_metadata(backend):
    key = backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")
    assert key == f"local://{MERCHANT_A}/{SHA}.csv"
    assert backend.retrieve(MERCHANT_A, key) == DATA
    meta = backend.metadata(MERCHANT_A, key)
    assert meta.storage_key == key
    assert meta.sha256 == SHA
    assert meta.size_bytes == len(DATA)
    assert meta.content_type == "text/csv"


def test_re_store_same_bytes_returns_same_key(backend):
    key1 = backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")
    key2 = backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")
    assert key1 == key2
    assert backend.retrieve(MERCHANT_A, key1) == DATA


def test_store_rejects_hash_mismatch(backend):
    with pytest.raises(ValueError):
        backend.store(MERCHANT_A, sha256="0" * 64, data=DATA, content_type="text/csv")


def test_store_enforces_size_cap(backend, monkeypatch):
    monkeypatch.setattr(storage, "MAX_OBJECT_BYTES", 10)
    with pytest.raises(StorageTooLarge):
        backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")


def test_cross_tenant_retrieve_refused(backend):
    key = backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")
    with pytest.raises(StorageAccessDenied):
        backend.retrieve(MERCHANT_B, key)
    with pytest.raises(StorageAccessDenied):
        backend.metadata(MERCHANT_B, key)
    with pytest.raises(StorageAccessDenied):
        backend.delete(MERCHANT_B, key)
    # The object is untouched by the refused attempts.
    assert backend.retrieve(MERCHANT_A, key) == DATA


def test_path_traversal_keys_refused(backend):
    bad_keys = [
        f"local://{MERCHANT_A}/../../etc/passwd",
        f"local://{MERCHANT_A}/..%2f..%2fetc/passwd",
        f"local://{MERCHANT_A}/{SHA}.csv/../../etc/passwd",
        f"/var/imports/{MERCHANT_A}/{SHA}.csv",
        f"local://{MERCHANT_A}/{SHA}.txt",
        f"local://{MERCHANT_A}/{SHA}",
        "local://not-a-uuid/" + SHA + ".csv",
        "",
    ]
    for key in bad_keys:
        with pytest.raises(StorageAccessDenied):
            backend.retrieve(MERCHANT_A, key)


def test_delete_then_retrieve_is_not_found(backend):
    key = backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")
    backend.delete(MERCHANT_A, key)
    with pytest.raises(StorageNotFound):
        backend.retrieve(MERCHANT_A, key)
    with pytest.raises(StorageNotFound):
        backend.metadata(MERCHANT_A, key)
    with pytest.raises(StorageNotFound):
        backend.delete(MERCHANT_A, key)


def test_retrieve_missing_key_is_not_found(backend):
    key = f"local://{MERCHANT_A}/{SHA}.csv"
    with pytest.raises(StorageNotFound):
        backend.retrieve(MERCHANT_A, key)


def test_legacy_file_without_sidecar_reads_unknown_content_type(backend, tmp_path):
    # Files stored before sidecars existed have no .meta.json; the
    # backend must still serve them (content type "unknown") rather
    # than fail.
    target = tmp_path / "imports" / str(MERCHANT_A) / f"{SHA}.csv"
    target.parent.mkdir(parents=True)
    target.write_bytes(DATA)
    key = f"local://{MERCHANT_A}/{SHA}.csv"
    assert backend.retrieve(MERCHANT_A, key) == DATA
    assert backend.metadata(MERCHANT_A, key).content_type == "unknown"


def test_get_storage_backend_returns_local_backend(tmp_path):
    backend = get_storage_backend(root=tmp_path / "imports")
    assert isinstance(backend, LocalFilesystemBackend)
    key = backend.store(MERCHANT_A, sha256=SHA, data=DATA, content_type="text/csv")
    assert backend.retrieve(MERCHANT_A, key) == DATA
