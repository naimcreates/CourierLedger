"""CSV import framework (M1 Milestone 4).

Configurable column mapping, immutable mapping snapshots per import,
original files preserved (file_objects + SHA-256), raw values
preserved verbatim, duplicate detection, ambiguous matches flagged
for review, never a match on amount alone.

SCOPE AND HONESTY RULES (kickoff brief + V2.2.1):
- No courier CSV template is invented here. The mapping is supplied
  by the caller (canonical field -> source header); the only fixture
  layouts in the repo are clearly labelled SYNTHETIC EXAMPLES of a
  generic statement layout (see tests/data/). No Steadfast / Pathao /
  Paperfly / RedX / eCourier template or rate is claimed.
- Money is integer poisha only. A money cell is parsed as:
    column absent  -> (None, 'column_absent')
    blank          -> (None, 'blank')
    digits, optional thousands commas, optional sign, exactly 0
                   -> (0, 'explicit_zero')
    digits, value > 0 (negative collection/fees are always invalid,
    CL-02; a negative payout is invalid unless the rule version for
    the account explicitly permits it -- OD-12 open, default False)
                   -> (value, 'present_valid')
    anything else  -> (None, 'invalid')
  Absent / blank / invalid / explicit zero are four different states
  (V2.2.1); none is silently treated as zero.
- Matching precedence (V2.1, implemented, never amount alone):
    P0 strong: exactly one active non-superseded shipment in this
      merchant whose normalised tracking id equals the row's ->
      strong_matched, candidate (strong_tracking, rank 1), shipment
      linked.
    P1 repeated tracking: >1 such shipment -> ambiguous, one
      candidate per shipment, no shipment linked.
    P2 weak fallback: no tracking match, but an exact merchant order
      ref to exactly one shipment, same courier account, statement
      date within 7 days after the shipment's shipped_on (both dates
      present), no other candidate -> ambiguous with one candidate
      (weak_order_ref, rank 1), NO shipment linked -- weak matches
      always need human confirmation.
    Duplicate tracking inside the batch: every row sharing a
      normalised tracking id with another row in the same batch ->
      match_status 'duplicate', DUPLICATE_TRACKING warning, no
      candidates, no shipment linked (a human resolves it).
    Otherwise: unmatched.
  Amounts are NEVER compared for matching anywhere in this module.
- A mapping snapshot is written once, on the import batch, and no
  service or API in this codebase mutates it afterwards. A re-import
  with a different mapping creates a NEW batch with its own snapshot.
- Idempotency (CL-04/CL-14/CL-24): import_create keys, claimed via
  app/services/idempotency_service.py. Same key + same request hash
  after completion replays the stored batch; a different hash
  conflicts; a live claim conflicts. The request hash covers the
  merchant, the file bytes, the mapping and the courier account.
- File preservation: the uploaded bytes are stored under
  backend/var/imports/<merchant_id>/<sha256>.csv (local-dev stand-in
  for object storage; storage_key "local://..."). The file_objects row
  carries the SHA-256 and size; re-uploading identical bytes for the
  same merchant reuses the file row (same bytes = same file).
- Transaction discipline: run_import() owns one transaction -- parse,
  rows, statement lines, candidates, errors, batch counts and the
  idempotency completion commit together; any failure rolls all of it
  back and fails the idempotency key (conditional fail, CL-24).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CANONICAL_FIELDS,
    FileObject,
    ImportBatch,
    ImportError,
    ImportRow,
    Order,
    Shipment,
    ShipmentMatchCandidate,
    StatementLine,
)
from app.db.session import set_tenant_context
from app.logging_config import get_logger
from app.services import idempotency_service
from app.services.workspace_service import get_membership

logger = get_logger(__name__)

# Where uploaded bytes live in local dev (object-storage stand-in).
# Resolved relative to this file's backend/ directory.
VAR_IMPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "var" / "imports"

MAX_ROWS_RETURNED = 500  # detail endpoint cap (see api/imports.py)
MAX_ERRORS_RETURNED = 500  # detail endpoint errors cap (C-1.9)
# Phase 1 (C-2.1): ingestion caps. A 10 MB file can otherwise expand
# into hundreds of thousands of rows in one transaction.
MAX_IMPORT_ROWS = 50_000
MAX_IMPORT_COLUMNS = 100
# Explicit per-field limit (Python csv default is 131072 bytes); a
# larger field is a parse failure, not a silent truncation.
CSV_FIELD_SIZE_LIMIT = 1024 * 1024  # 1 MB per field
# Phase 1 (C-2.3): cells whose stripped value starts with one of these
# are flagged FORMULA_LIKE_VALUE at ingest (a future export must
# neutralise them; stored raw values are never rewritten — verbatim
# preservation is a design rule). A plain signed/unsigned number
# (e.g. "-500") is not a formula and is not flagged.
FORMULA_LIKE_PREFIXES = ("=", "+", "-", "@", "|", "%")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)$")


class ImportAccessError(Exception):
    """No active membership for this workspace (API maps to 404)."""


class MappingError(ValueError):
    """The supplied mapping is not usable (API maps to 422)."""


class ImportFailedError(Exception):
    """The import could not be completed (idempotency conflict,
    ownership lost, or an unparsable file)."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_tracking(value: str | None) -> str | None:
    """Tracking-id normalisation for matching: strip, upper-case,
    drop internal whitespace. Returns None for blank input."""
    if value is None:
        return None
    cleaned = re.sub(r"\s+", "", value.strip().upper())
    return cleaned or None


def _header_key(header: str) -> str:
    """Header lookup key: stripped, case-insensitive."""
    return header.strip().lower()


def parse_money(raw: str | None, *, column_present: bool, allow_negative: bool = False):
    """Parse one money cell to (value_poisha | None, dq_state).

    See the module docstring for the state table. ``column_present``
    is False when the mapped source header does not exist in the file
    at all (every row then gets 'column_absent' for that field)."""
    if not column_present:
        return None, "column_absent"
    if raw is None or raw.strip() == "":
        return None, "blank"
    cleaned = raw.strip().replace(",", "")
    if re.fullmatch(r"[+-]?\d+", cleaned) is None:
        return None, "invalid"
    value = int(cleaned)
    if value == 0:
        return 0, "explicit_zero"
    if value < 0 and not allow_negative:
        return None, "invalid"
    return value, "present_valid"


def parse_statement_date(raw: str | None) -> date | None:
    """ISO YYYY-MM-DD only; anything else is not a date (None)."""
    if raw is None or raw.strip() == "":
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_mapping(mapping: dict) -> dict:
    """Validate a caller-supplied mapping (canonical -> source header).
    Returns the mapping with stripped source headers. Raises
    MappingError on: not a dict, empty, unknown canonical field, a
    non-string/blank source header, or one source header mapped to
    two canonical fields."""
    if not isinstance(mapping, dict) or not mapping:
        raise MappingError("Mapping must be a non-empty object of canonical field -> source header.")
    cleaned: dict[str, str] = {}
    seen_sources: dict[str, str] = {}
    for canonical, source in mapping.items():
        if canonical not in CANONICAL_FIELDS:
            raise MappingError(f"Unknown canonical field: {canonical!r}.")
        if not isinstance(source, str) or source.strip() == "":
            raise MappingError(f"Source header for {canonical!r} must be a non-empty string.")
        header = source.strip()
        if len(header) > 500:  # Phase 1 (C-1.7): mapping headers are unbounded strings otherwise
            raise MappingError(f"Source header for {canonical!r} must be at most 500 characters.")
        key = _header_key(header)
        if key in seen_sources:
            raise MappingError(
                f"Source header {header!r} is mapped to both "
                f"{seen_sources[key]!r} and {canonical!r}."
            )
        seen_sources[key] = canonical
        cleaned[canonical] = header
    return cleaned


def _is_formula_like(value: str | None) -> bool:
    """True when a cell looks like a spreadsheet formula (Phase 1,
    C-2.3). Plain numbers (e.g. "-500", "+12.5") are not formulas."""
    if value is None:
        return False
    stripped = str(value).strip()
    if not stripped or not stripped.startswith(FORMULA_LIKE_PREFIXES):
        return False
    return _NUMERIC_RE.match(stripped) is None


def parse_csv_bytes(data: bytes):
    """Decode + parse CSV bytes.

    Returns (headers, rows) where headers is the stripped header list
    and rows is a list of dicts: {header: raw string} with ABSENT keys
    omitted (a missing trailing field is absent, not blank) and blank
    cells kept as "". Raises ImportFailedError on undecodable bytes,
    an empty file, or a header row with no usable columns.
    Row field-count mismatches are NOT raised here -- they are handled
    per-row by the importer (parse_status 'failed').
    Phase 1 (C-2.1): the reader is iterated (never list(reader)) and
    the row/column caps are enforced during iteration, so an oversized
    file is rejected before it materialises into hundreds of thousands
    of rows; csv.field_size_limit is set explicitly.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFailedError(f"File is not valid UTF-8 text: {exc}") from exc
    csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)
    reader = csv.reader(io.StringIO(text))
    try:
        rows_iter = iter(reader)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise ImportFailedError("File is empty.") from None
        headers = [h.strip() for h in header_row]
        if not any(headers):
            raise ImportFailedError("File has no header row.")
        if len([h for h in headers if h]) > MAX_IMPORT_COLUMNS:
            raise ImportFailedError(
                f"File has more than {MAX_IMPORT_COLUMNS} columns."
            )
        if len(set(_header_key(h) for h in headers if h)) != len([h for h in headers if h]):
            raise ImportFailedError("File has duplicate header names.")
        parsed: list[dict] = []
        for raw_row in rows_iter:
            if len(parsed) >= MAX_IMPORT_ROWS:
                raise ImportFailedError(
                    f"File has more than {MAX_IMPORT_ROWS} rows."
                )
            if len(raw_row) == 0 or all(str(v).strip() == "" for v in raw_row):
                # A genuinely blank line: an empty record, which the
                # importer records as parse_status 'skipped_blank' (it is
                # still a line of the file, so it keeps its row number).
                parsed.append({})
                continue
            if len(raw_row) != len(headers):
                # Field-count mismatch: keep the raw row for the failed-row
                # path (the importer records it, it does not crash).
                parsed.append({"__raw__": raw_row, "__mismatch__": True})
                continue
            record = {}
            for header, value in zip(headers, raw_row):
                if value is None:
                    continue  # absent stays absent
                record[header] = value
            parsed.append(record)
    except csv.Error as exc:
        raise ImportFailedError(f"CSV could not be parsed: {exc}") from exc
    return headers, parsed


def _raw_line_hash(headers: list[str], record: dict) -> str:
    """Hash of the raw line in header order (absent -> empty slot),
    so a duplicate raw line is detectable even if parsing differs."""
    joined = "\x1f".join(record.get(h, "") for h in headers)
    return _sha256_hex(joined.encode("utf-8"))


def _request_hash(merchant_id, file_sha256: str, mapping: dict, account_id) -> str:
    canonical = json.dumps(
        {"merchant_id": str(merchant_id), "file_sha256": file_sha256,
         "mapping": mapping, "account_id": str(account_id)},
        sort_keys=True,
    )
    return _sha256_hex(canonical.encode("utf-8"))


def _normalise_content_type(declared: str | None) -> str:
    """Store the upload's declared content type instead of hardcoding
    text/csv (Phase 1, C-2.2). Only the two types the file_objects
    CHECK constraint allows are stored verbatim; anything else —
    including an absent declaration — is stored as "unknown" (the
    constraint was widened for it in Alembic migration 0003). The
    declared type is a client claim, not a verified fact: the only
    real gate remains "does it decode as UTF-8 and parse as CSV"."""
    if declared in ("text/csv", "application/vnd.ms-excel"):
        return declared
    return "unknown"


def _find_or_create_file_object(
    db: Session, merchant_id, user_id, filename: str, data: bytes, file_sha256: str,
    content_type: str | None = None,
) -> FileObject:
    existing = db.execute(
        select(FileObject).where(
            (FileObject.merchant_id == merchant_id)
            & (FileObject.sha256 == file_sha256)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    storage_key = f"local://{merchant_id}/{file_sha256}.csv"
    target = VAR_IMPORTS_DIR / str(merchant_id) / f"{file_sha256}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)  # content-addressed; an identical re-upload rewrites identical bytes
    file_object = FileObject(
        merchant_id=merchant_id,
        storage_key=storage_key,
        original_filename=filename,
        sha256=file_sha256,
        size_bytes=len(data),
        content_type=_normalise_content_type(content_type),
        uploaded_by_user_id=user_id,
    )
    db.add(file_object)
    db.flush()
    return file_object


def _decide_match(db: Session, merchant_id, account_id, *, tracking, ref, statement_date):
    """Decide the match for one statement line (see module docstring).
    Pure decision: returns (match_status, shipment_id_or_None,
    candidates, error_tuple_or_None) where candidates is a list of
    (shipment_id, basis, rank) and the error tuple is
    (code, severity, message) for an AMBIGUOUS_MATCH warning, or None.
    Amounts are never consulted. Nothing is written here -- the caller
    inserts the line with its final match_status, because
    statement_lines is INSERT-only for the runtime role (roles.sql)."""
    if tracking:
        candidates = db.execute(
            select(Shipment).where(
                (Shipment.merchant_id == merchant_id)
                & (Shipment.tracking_id_normalised == tracking)
                & (Shipment.is_superseded.is_(False))
            )
        ).scalars().all()
        if len(candidates) == 1:
            return ("strong_matched", candidates[0].id,
                    [(candidates[0].id, "strong_tracking", 1)], None)
        if len(candidates) > 1:
            return ("ambiguous", None,
                    [(s.id, "strong_tracking", rank) for rank, s in enumerate(candidates, start=1)],
                    ("AMBIGUOUS_MATCH", "warning",
                     f"{len(candidates)} shipments share tracking id {tracking!r}; human confirmation required."))
    # No tracking match: weak fallback on the merchant order ref.
    if ref:
        ref = ref.strip()
    if ref and statement_date is not None:
        weak = (
            db.execute(
                select(Shipment)
                .join(Order, (Order.merchant_id == Shipment.merchant_id)
                      & (Order.id == Shipment.order_id))
                .where(
                    (Shipment.merchant_id == merchant_id)
                    & (Order.merchant_order_ref == ref)
                    & (Shipment.merchant_courier_account_id == account_id)
                    & (Shipment.is_superseded.is_(False))
                    & (Shipment.shipped_on.is_not(None))
                )
            ).scalars().all()
        )
        in_window = [
            s for s in weak
            if 0 <= (statement_date - s.shipped_on).days <= 7
        ]
        if len(in_window) == 1:
            return ("ambiguous", None,
                    [(in_window[0].id, "weak_order_ref", 1)],
                    ("AMBIGUOUS_MATCH", "warning",
                     "Weak fallback match on merchant order ref; human confirmation required "
                     "(weak matches are never confirmed automatically)."))
        if len(in_window) > 1:
            return ("ambiguous", None,
                    [(s.id, "weak_order_ref", rank) for rank, s in enumerate(in_window, start=1)],
                    ("AMBIGUOUS_MATCH", "warning",
                     f"{len(in_window)} shipments match merchant order ref {ref!r} in the date window; "
                     "human confirmation required."))
    return ("unmatched", None, [], None)


def _add_error(db: Session, line: StatementLine | None, code: str, severity: str,
               message: str, field_name: str | None = None, raw_value: str | None = None,
               batch: ImportBatch | None = None):
    """Insert one import_errors row. ``line`` may be None for
    batch-level errors (import_row_id NULL then); ``batch`` is used
    only in that case."""
    merchant_id = line.merchant_id if line is not None else batch.merchant_id
    batch_id = line.import_batch_id if line is not None else batch.id
    db.add(ImportError(
        merchant_id=merchant_id,
        import_batch_id=batch_id,
        import_row_id=line.import_row_id if line is not None else None,
        field_name=field_name,
        severity=severity,
        error_code=code,
        message=message,
        raw_value=raw_value,
    ))


def run_import(
    db: Session,
    *,
    user,
    merchant_id,
    merchant_courier_account_id,
    filename: str,
    data: bytes,
    mapping: dict,
    idempotency_key: str | None = None,
    claimed_by_instance: str = "csv-import",
    content_type: str | None = None,
) -> dict:
    """Run one CSV import. Returns a summary dict (batch id, status,
    counts, file sha256, mapping snapshot, idempotency outcome).

    Raises ImportAccessError (no membership), MappingError (bad
    mapping), ImportFailedError (idempotency conflict / ownership
    lost / unparsable file). On any failure after the claim, the
    idempotency key is failed (CL-24) and the transaction rolls back.
    The caller must NOT commit on an exception; on success this
    function commits."""
    membership = get_membership(db, user.id, merchant_id)
    if membership is None:
        raise ImportAccessError("No active membership for this workspace.")
    # Tenant context must be set BEFORE any tenant-table read: the
    # merchant_courier_accounts SELECT policy (RLS) keys on
    # app.current_merchant_id, and without it the account lookup
    # below returns nothing even for a real account. (Found live:
    # the account check 404'd on every upload until this moved up.)
    set_tenant_context(db, merchant_id)
    from app.db.models import MerchantCourierAccount

    account = db.execute(
        select(MerchantCourierAccount).where(
            (MerchantCourierAccount.merchant_id == merchant_id)
            & (MerchantCourierAccount.id == merchant_courier_account_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise ImportAccessError("Courier account not found in this workspace.")
    mapping = validate_mapping(mapping)

    file_sha256 = _sha256_hex(data)
    request_hash = _request_hash(merchant_id, file_sha256, mapping, merchant_courier_account_id)
    key = idempotency_key or f"import-{uuid.uuid4().hex}"
    try:
        outcome, key_row = idempotency_service.claim(
            db, merchant_id, "import_create", key, request_hash, claimed_by_instance
        )
    except idempotency_service.IdempotencyConflictError as exc:
        raise ImportFailedError(str(exc)) from exc
    if outcome == "replay":
        batch = db.get(ImportBatch, key_row.result_import_batch_id)
        return _batch_summary(db, batch, outcome="replay")

    file_object = _find_or_create_file_object(
        db, merchant_id, user.id, filename, data, file_sha256,
        content_type=content_type,
    )
    batch = ImportBatch(
        merchant_id=merchant_id,
        file_object_id=file_object.id,
        merchant_courier_account_id=merchant_courier_account_id,
        mapping_snapshot=dict(mapping),
        status="uploaded",
        created_by_user_id=user.id,
    )
    db.add(batch)
    db.flush()

    try:
        headers, records = parse_csv_bytes(data)
    except ImportFailedError:
        batch.status = "parse_failed"
        _add_error(db, None, "VALUE_UNPARSABLE", "error",
                   "File could not be parsed as CSV.", batch=batch)
        _finish_batch(db, batch, key_row, key, merchant_id, failed=True)
        db.commit()
        return _batch_summary(db, batch, outcome="claimed")

    # Resolve mapped headers to actual file headers (case-insensitive).
    header_by_key = {_header_key(h): h for h in headers if h}
    resolved: dict[str, str | None] = {}
    for canonical, source in mapping.items():
        resolved[canonical] = header_by_key.get(_header_key(source))
    for canonical, actual in resolved.items():
        if actual is None:
            _add_error(db, None, "COLUMN_ABSENT", "warning",
                       f"Mapped source header {mapping[canonical]!r} for {canonical!r} "
                       "is absent from the file; every row records column_absent for it.",
                       field_name=canonical, batch=batch)
    batch.status = "mapped"
    db.flush()

    # Duplicate tracking within the batch (normalised), for P3.
    tracking_counts: dict[str, int] = {}
    for record in records:
        if record.get("__mismatch__"):
            continue
        source = resolved.get("tracking_id")
        raw = record.get(source) if source else None
        norm = normalise_tracking(raw)
        if norm:
            tracking_counts[norm] = tracking_counts.get(norm, 0) + 1

    row_count = 0
    parsed_count = 0
    error_row_count = 0
    for row_number, record in enumerate(records, start=1):
        row_count += 1
        if record.get("__mismatch__"):
            import_row = ImportRow(
                merchant_id=merchant_id,
                import_batch_id=batch.id,
                row_number=row_number,
                raw_values={"__raw__": " ".join(record["__raw__"])},
                raw_line_hash=_sha256_hex(" ".join(record["__raw__"]).encode("utf-8")),
                parse_status="failed",
            )
            db.add(import_row)
            db.flush()
            _add_error(db, _row_line_stub(import_row), "VALUE_UNPARSABLE", "error",
                       "Row has a different number of fields than the header row; "
                       "it was preserved but not parsed into a statement line.")
            error_row_count += 1
            continue
        if all((v is None or str(v).strip() == "") for v in record.values()):
            db.add(ImportRow(
                merchant_id=merchant_id,
                import_batch_id=batch.id,
                row_number=row_number,
                raw_values={},
                raw_line_hash=_raw_line_hash(headers, record),
                parse_status="skipped_blank",
            ))
            continue
        # Parse first, decide the match second, insert last: the
        # runtime role may INSERT import_rows / statement_lines but
        # never UPDATE them (roles.sql), so every row and line is
        # written once, already carrying its final parse_status and
        # match_status.
        fields, parse_errors = _parse_record_fields(headers, record, resolved)
        norm = fields["tracking_id_normalised"]
        if norm and tracking_counts.get(norm, 0) > 1:
            # Duplicate tracking: matching is suspended for these rows
            # (no candidates are written at all -- a human resolves it).
            match_status, shipment_id, candidates, match_error = (
                "duplicate", None, [],
                ("DUPLICATE_TRACKING", "warning",
                 f"Tracking id {norm!r} appears {tracking_counts[norm]} times in this "
                 "batch; rows are preserved and no shipment is linked until a human resolves it.",
                 "tracking_id", norm),
            )
        else:
            match_status, shipment_id, candidates, match_error = _decide_match(
                db, merchant_id, merchant_courier_account_id,
                tracking=norm,
                ref=fields["merchant_order_ref_raw"],
                statement_date=fields["statement_date"],
            )
        errors = list(parse_errors)
        if match_error is not None:
            # _decide_match returns a 3-tuple (code, severity, message);
            # the duplicate branch above returns the full 5-tuple.
            errors.append(match_error if len(match_error) == 5 else (*match_error, None, None))
        parsed_count += 1
        if errors:
            error_row_count += 1
        import_row = ImportRow(
            merchant_id=merchant_id,
            import_batch_id=batch.id,
            row_number=row_number,
            raw_values={h: record[h] for h in headers if h in record},
            raw_line_hash=_raw_line_hash(headers, record),
            parse_status="parsed_with_warnings" if errors else "parsed",
        )
        db.add(import_row)
        db.flush()
        statement_line = StatementLine(
            merchant_id=merchant_id,
            import_row_id=import_row.id,
            import_batch_id=batch.id,
            match_status=match_status,
            shipment_id=shipment_id,
            tracking_id_raw=fields["tracking_id_raw"],
            tracking_id_normalised=norm,
            merchant_order_ref_raw=fields["merchant_order_ref_raw"],
            reported_status_raw=fields["reported_status_raw"],
            statement_date=fields["statement_date"],
            reported_collected_poisha=fields["reported_collected_poisha"],
            reported_collected_state=fields["reported_collected_state"],
            reported_payout_poisha=fields["reported_payout_poisha"],
            reported_payout_state=fields["reported_payout_state"],
            reported_delivery_fee_poisha=fields["reported_delivery_fee_poisha"],
            reported_delivery_fee_state=fields["reported_delivery_fee_state"],
            reported_cod_fee_poisha=fields["reported_cod_fee_poisha"],
            reported_cod_fee_state=fields["reported_cod_fee_state"],
            reported_return_fee_poisha=fields["reported_return_fee_poisha"],
            reported_return_fee_state=fields["reported_return_fee_state"],
            reported_other_fee_poisha=fields["reported_other_fee_poisha"],
            reported_other_fee_state=fields["reported_other_fee_state"],
            negative_payout_permitted=False,
        )
        db.add(statement_line)
        db.flush()
        for shipment_id_c, basis, rank in candidates:
            db.add(ShipmentMatchCandidate(
                merchant_id=merchant_id,
                statement_line_id=statement_line.id,
                shipment_id=shipment_id_c,
                match_basis=basis,
                rank=rank,
            ))
        for code, severity, message, field_name, raw_value in errors:
            _add_error(db, statement_line, code, severity, message,
                       field_name=field_name, raw_value=raw_value)
        db.flush()

    batch.row_count = row_count
    batch.parsed_row_count = parsed_count
    batch.error_row_count = error_row_count
    batch.status = "ready" if error_row_count == 0 else "parsed"
    _finish_batch(db, batch, key_row, key, merchant_id, failed=False)
    db.commit()
    logger.info("csv_import_completed", extra={
        "batch_id": str(batch.id), "merchant_id": str(merchant_id),
        "rows": row_count, "parsed": parsed_count, "error_rows": error_row_count,
    })
    return _batch_summary(db, batch, outcome="claimed")


def _row_line_stub(import_row: ImportRow):
    """A minimal stand-in carrying the ids _add_error needs for a
    row that has no statement line."""
    class _Stub:
        merchant_id = import_row.merchant_id
        import_batch_id = import_row.import_batch_id
        import_row_id = import_row.id
    return _Stub()


def _parse_record_fields(headers, record, resolved):
    """Parse one CSV record into statement-line fields + parse errors.
    Pure: no database writes. Returns (fields, errors) where fields
    holds the values for a StatementLine and errors is a list of
    (code, severity, message, field_name, raw_value) tuples. The
    caller inserts the row/line/errors with their final values,
    because import_rows / statement_lines are INSERT-only for the
    runtime role (roles.sql) -- nothing may be updated after insert."""
    def _cell(canonical: str):
        source = resolved.get(canonical)
        if source is None:
            return None, False  # column absent from the file
        return record.get(source), True

    def _money(canonical: str, *, allow_negative: bool = False):
        raw, present = _cell(canonical)
        value, state = parse_money(raw, column_present=present, allow_negative=allow_negative)
        return raw, value, state

    tracking_raw, tracking_present = _cell("tracking_id")
    ref_raw, _ = _cell("merchant_order_ref")
    status_raw, _ = _cell("shipment_status")
    date_raw, _ = _cell("statement_date")
    statement_date = parse_statement_date(date_raw)

    collected_raw, collected, collected_state = _money("reported_collected")
    payout_raw, payout, payout_state = _money("reported_payout")
    delivery_raw, delivery, delivery_state = _money("delivery_fee")
    cod_raw, cod, cod_state = _money("cod_fee")
    return_raw, ret, return_state = _money("return_fee")
    other_raw, other, other_state = _money("other_fee")

    fields = {
        "tracking_id_raw": tracking_raw,
        "tracking_id_normalised": normalise_tracking(tracking_raw),
        "merchant_order_ref_raw": ref_raw,
        "reported_status_raw": status_raw,
        "statement_date": statement_date,
        "reported_collected_poisha": collected,
        "reported_collected_state": collected_state,
        "reported_payout_poisha": payout,
        "reported_payout_state": payout_state,
        "reported_delivery_fee_poisha": delivery,
        "reported_delivery_fee_state": delivery_state,
        "reported_cod_fee_poisha": cod,
        "reported_cod_fee_state": cod_state,
        "reported_return_fee_poisha": ret,
        "reported_return_fee_state": return_state,
        "reported_other_fee_poisha": other,
        "reported_other_fee_state": other_state,
    }

    errors = []
    for canonical, raw, state in (
        ("reported_collected", collected_raw, collected_state),
        ("reported_payout", payout_raw, payout_state),
        ("delivery_fee", delivery_raw, delivery_state),
        ("cod_fee", cod_raw, cod_state),
        ("return_fee", return_raw, return_state),
        ("other_fee", other_raw, other_state),
    ):
        if state == "column_absent":
            errors.append(("COLUMN_ABSENT", "warning",
                           f"Column for {canonical!r} is absent from the file.",
                           canonical, None))
        elif state == "blank":
            errors.append(("VALUE_BLANK", "warning",
                           f"Value for {canonical!r} is blank; recorded as unknown, not zero.",
                           canonical, raw))
        elif state == "invalid":
            errors.append(("VALUE_INVALID", "warning",
                           f"Value for {canonical!r} is not a valid integer-poisha amount; recorded as unknown.",
                           canonical, raw))
    if date_raw is not None and str(date_raw).strip() != "" and statement_date is None:
        errors.append(("VALUE_INVALID", "warning",
                       "Statement date is not a valid YYYY-MM-DD date; recorded as unknown.",
                       "statement_date", date_raw))
    if tracking_present and (tracking_raw is None or str(tracking_raw).strip() == ""):
        errors.append(("VALUE_BLANK", "warning",
                       "Tracking id is blank; the row can only match by merchant order ref.",
                       "tracking_id", tracking_raw))
    # Phase 1 (C-2.3): flag formula-like cells at ingest. The raw
    # value is preserved verbatim (never rewritten); the warning
    # exists so a future export can neutralise the cell before it
    # reaches a spreadsheet. One warning per cell, even when the cell
    # already produced a money/date warning above.
    for header, value in record.items():
        if _is_formula_like(value):
            errors.append(("FORMULA_LIKE_VALUE", "warning",
                           f"Cell in column {header!r} starts with a formula-like character; "
                           "stored verbatim — neutralise it before any spreadsheet export.",
                           header, value))
    return fields, errors


def _finish_batch(db, batch, key_row, key, merchant_id, *, failed: bool) -> None:
    """Complete (or fail) the idempotency key for this import.
    A False completion means ownership was lost mid-import: raise so
    the caller rolls the whole import back (CL-24)."""
    if failed:
        idempotency_service.fail(
            db, merchant_id, "import_create", key, key_row.claim_token
        )
        return
    completed = idempotency_service.complete(
        db, merchant_id, "import_create", key, key_row.claim_token, batch.id
    )
    if not completed:
        raise ImportFailedError(
            "Idempotency lease was lost during the import; the import was rolled back."
        )


def _batch_summary(db, batch: ImportBatch, *, outcome: str) -> dict:
    # Commit ends the transaction and clears the transaction-local
    # app.current_merchant_id setting; re-assert it before refresh,
    # or the RLS policy hides the row from the runtime role and
    # refresh raises "Could not refresh instance" (same load-bearing
    # pattern as workspace_service.create_workspace, B-1).
    set_tenant_context(db, batch.merchant_id)
    db.refresh(batch)
    return {
        "batch_id": str(batch.id),
        "merchant_id": str(batch.merchant_id),
        "status": batch.status,
        "row_count": batch.row_count,
        "parsed_row_count": batch.parsed_row_count,
        "error_row_count": batch.error_row_count,
        "mapping_snapshot": batch.mapping_snapshot,
        "file_object_id": str(batch.file_object_id),
        "idempotency_outcome": outcome,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


def get_batch_detail(db: Session, merchant_id, batch_id) -> dict | None:
    """Batch + rows + statement lines + errors for the detail API.
    Returns None when the batch is not in this workspace. Rows are
    capped (MAX_ROWS_RETURNED) with a truncated flag."""
    set_tenant_context(db, merchant_id)
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.merchant_id != merchant_id:
        return None
    rows = db.execute(
        select(ImportRow)
        .where(ImportRow.import_batch_id == batch.id)
        .order_by(ImportRow.row_number)
        .limit(MAX_ROWS_RETURNED + 1)
    ).scalars().all()
    truncated = len(rows) > MAX_ROWS_RETURNED
    rows = rows[:MAX_ROWS_RETURNED]
    # Phase 1 (C-1.9): statement lines are loaded only for the rows
    # actually returned — never the whole batch — and the errors list
    # is capped with a truncated flag, like the rows list.
    row_ids = [r.id for r in rows]
    line_by_row = {
        line.import_row_id: line
        for line in db.execute(
            select(StatementLine).where(StatementLine.import_row_id.in_(row_ids))
        ).scalars().all()
    } if row_ids else {}
    error_rows = db.execute(
        select(ImportError)
        .where(ImportError.import_batch_id == batch.id)
        .order_by(ImportError.created_at)
        .limit(MAX_ERRORS_RETURNED + 1)
    ).scalars().all()
    truncated_errors = len(error_rows) > MAX_ERRORS_RETURNED
    errors = error_rows[:MAX_ERRORS_RETURNED]
    return {
        "batch": _batch_summary(db, batch, outcome="stored"),
        "rows": [
            {
                "row_number": r.row_number,
                "parse_status": r.parse_status,
                "raw_values": r.raw_values,
                "statement_line": (
                    {
                        "tracking_id_raw": line_by_row[r.id].tracking_id_raw,
                        "tracking_id_normalised": line_by_row[r.id].tracking_id_normalised,
                        "merchant_order_ref_raw": line_by_row[r.id].merchant_order_ref_raw,
                        "reported_status_raw": line_by_row[r.id].reported_status_raw,
                        "statement_date": (
                            line_by_row[r.id].statement_date.isoformat()
                            if line_by_row[r.id].statement_date else None
                        ),
                        "match_status": line_by_row[r.id].match_status,
                        "shipment_id": (
                            str(line_by_row[r.id].shipment_id)
                            if line_by_row[r.id].shipment_id else None
                        ),
                        "reported_collected_poisha": line_by_row[r.id].reported_collected_poisha,
                        "reported_collected_state": line_by_row[r.id].reported_collected_state,
                        "reported_payout_poisha": line_by_row[r.id].reported_payout_poisha,
                        "reported_payout_state": line_by_row[r.id].reported_payout_state,
                        "reported_delivery_fee_poisha": line_by_row[r.id].reported_delivery_fee_poisha,
                        "reported_delivery_fee_state": line_by_row[r.id].reported_delivery_fee_state,
                        "reported_cod_fee_poisha": line_by_row[r.id].reported_cod_fee_poisha,
                        "reported_cod_fee_state": line_by_row[r.id].reported_cod_fee_state,
                        "reported_return_fee_poisha": line_by_row[r.id].reported_return_fee_poisha,
                        "reported_return_fee_state": line_by_row[r.id].reported_return_fee_state,
                        "reported_other_fee_poisha": line_by_row[r.id].reported_other_fee_poisha,
                        "reported_other_fee_state": line_by_row[r.id].reported_other_fee_state,
                    }
                    if r.id in line_by_row else None
                ),
            }
            for r in rows
        ],
        "truncated": truncated,
        "truncated_errors": truncated_errors,
        "errors": [
            {
                "import_row_id": str(e.import_row_id) if e.import_row_id else None,
                "field_name": e.field_name,
                "severity": e.severity,
                "error_code": e.error_code,
                "message": e.message,
                "raw_value": e.raw_value,
            }
            for e in errors
        ],
    }
