"""CSV import framework tests (M1 Milestone 4) -- SQLite fallback suite.

Covers: parsing, configurable mapping, immutable mapping snapshots,
file preservation, duplicate detection, ambiguous matches, weak
fallback (human confirmation only), amount-only rejection, data
quality states (absent / blank / invalid / explicit zero), idempotent
replay, and access control. Live-PostgreSQL coverage (RLS + runtime
role + HTTP upload) lives in tests/test_imports_live_pg.py.

All fixtures are SYNTHETIC EXAMPLES of a generic statement layout --
no courier's real CSV template or rate is claimed anywhere here.
"""
import hashlib
import json
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.auth_models import Merchant, MerchantMembership, User
from app.db.models import (
    Courier,
    FileObject,
    ImportBatch,
    ImportError,
    ImportRow,
    MerchantCourierAccount,
    Order,
    Shipment,
    ShipmentMatchCandidate,
    StatementLine,
)
from app.db.session import SessionLocal
from app.services import csv_import

DATA = Path(__file__).parent / "data"
FIXTURE = DATA / "generic_statement_fixture.csv"
ALT_FIXTURE = DATA / "generic_statement_fixture_alt_headers.csv"

MAPPING = {
    "tracking_id": "Tracking ID",
    "merchant_order_ref": "Order Ref",
    "reported_collected": "Collected (poisha)",
    "reported_payout": "Payout (poisha)",
    "delivery_fee": "Delivery Fee (poisha)",
    "cod_fee": "COD Fee (poisha)",
    "return_fee": "Return Fee (poisha)",
    "other_fee": "Other Fee (poisha)",
    "shipment_status": "Status",
    "statement_date": "Statement Date",
}
ALT_MAPPING = {
    "tracking_id": "TRK",
    "merchant_order_ref": "ORD",
    "reported_collected": "Collected",
    "reported_payout": "Payout",
    "delivery_fee": "DelFee",
    "cod_fee": "CODFee",
    "return_fee": "RetFee",
    "other_fee": "OtherFee",
    "shipment_status": "ShipStatus",
    "statement_date": "StmtDate",
}


@pytest.fixture()
def db(monkeypatch, tmp_path):
    # Keep test file writes out of the real backend/var/ tree.
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def env(db):
    user = User(email="m4-user@example.com", password_hash="x", status="active")
    db.add(user)
    db.flush()
    merchant = Merchant(name="M4 Shop", created_by_user_id=user.id)
    db.add(merchant)
    db.flush()
    db.add(MerchantMembership(merchant_id=merchant.id, user_id=user.id, role="owner", status="active"))
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.flush()

    def _order(ref, tracking=None, shipped_on=None):
        order = Order(merchant_id=merchant.id, merchant_order_ref=ref, order_cod_poisha=100000)
        db.add(order)
        db.flush()
        shipment = Shipment(
            merchant_id=merchant.id,
            order_id=order.id,
            merchant_courier_account_id=account.id,
            tracking_id_raw=tracking,
            tracking_id_normalised=tracking,
            outcome="delivered",
            expected_collectible_cod_poisha=100000,
            shipped_on=shipped_on,
        )
        db.add(shipment)
        db.flush()
        return order, shipment

    shipments = {}
    for ref, tracking in [
        ("ORD-1001", "TRK-1001"),
        ("ORD-1002", "TRK-1002"),
        ("ORD-1003", "TRK-DUP"),
        ("ORD-1007", "TRK-1007X"),
        ("ORD-1008", "TRK-1008"),
        ("ORD-1009", "TRK-1009"),
        ("ORD-1010", "TRK-1010X"),
    ]:
        _, s = _order(ref, tracking, shipped_on=date(2026, 10, 10))
        shipments[ref] = s
    # Ambiguous: two active shipments share one tracking id.
    _order("ORD-1005a", "TRK-AMB", shipped_on=date(2026, 10, 10))
    _order("ORD-1005b", "TRK-AMB", shipped_on=date(2026, 10, 10))
    db.commit()
    return {"user": user, "merchant": merchant, "account": account, "shipments": shipments}


def _run(db, env, data: bytes, mapping=None, key=None):
    return csv_import.run_import(
        db,
        user=env["user"],
        merchant_id=env["merchant"].id,
        merchant_courier_account_id=env["account"].id,
        filename="statement.csv",
        data=data,
        mapping=dict(MAPPING) if mapping is None else mapping,
        idempotency_key=key or f"key-{uuid.uuid4().hex}",
    )


def _lines(db, batch_id):
    rows = db.execute(
        select(ImportRow).where(ImportRow.import_batch_id == batch_id).order_by(ImportRow.row_number)
    ).scalars().all()
    lines = {
        line.import_row_id: line
        for line in db.execute(
            select(StatementLine).where(StatementLine.import_batch_id == batch_id)
        ).scalars().all()
    }
    return rows, lines


def _candidates(db, line_id):
    return db.execute(
        select(ShipmentMatchCandidate).where(ShipmentMatchCandidate.statement_line_id == line_id)
    ).scalars().all()


def _errors(db, batch_id, code=None):
    q = select(ImportError).where(ImportError.import_batch_id == batch_id)
    if code:
        q = q.where(ImportError.error_code == code)
    return db.execute(q).scalars().all()


# ---------------------------------------------------------------------------
# End-to-end fixture import
# ---------------------------------------------------------------------------

def test_fixture_import_end_to_end(db, env):
    data = FIXTURE.read_bytes()
    summary = _run(db, env, data, key="m4-e2e")
    assert summary["idempotency_outcome"] == "claimed"
    assert summary["status"] == "parsed"  # error rows exist, so not 'ready'
    assert summary["row_count"] == 12  # incl. blank line + field-count row
    assert summary["parsed_row_count"] == 10
    assert summary["error_row_count"] == 7
    assert summary["mapping_snapshot"] == MAPPING

    batch_id = uuid.UUID(summary["batch_id"])
    batch = db.get(ImportBatch, batch_id)
    assert batch.mapping_snapshot == MAPPING

    # Original file preserved, content-addressed, hash matches.
    file_object = db.get(FileObject, batch.file_object_id)
    assert file_object.sha256 == hashlib.sha256(data).hexdigest()
    assert file_object.size_bytes == len(data)
    stored = Path(csv_import.VAR_IMPORTS_DIR) / str(env["merchant"].id) / f"{file_object.sha256}.csv"
    assert stored.read_bytes() == data

    rows, lines = _lines(db, batch_id)
    assert [r.row_number for r in rows] == list(range(1, 13))
    assert [r.parse_status for r in rows] == [
        "parsed", "parsed", "parsed_with_warnings", "parsed_with_warnings",
        "parsed_with_warnings", "parsed", "parsed_with_warnings",
        "parsed_with_warnings", "parsed", "skipped_blank",
        "parsed_with_warnings", "failed",
    ]
    # Raw values preserved verbatim (row 1).
    assert rows[0].raw_values["Tracking ID"] == "TRK-1001"
    assert rows[0].raw_values["Collected (poisha)"] == "100000"

    by_row = {r.row_number: lines.get(r.id) for r in rows}
    # Rows 1-2: strong unique tracking match, shipment linked.
    assert by_row[1].match_status == "strong_matched"
    assert by_row[1].shipment_id == env["shipments"]["ORD-1001"].id
    assert by_row[2].match_status == "strong_matched"
    assert len(_candidates(db, by_row[1].id)) == 1
    assert _candidates(db, by_row[1].id)[0].match_basis == "strong_tracking"
    # Rows 3-4: duplicate tracking inside the batch.
    assert by_row[3].match_status == "duplicate"
    assert by_row[4].match_status == "duplicate"
    assert by_row[3].shipment_id is None and by_row[4].shipment_id is None
    assert _candidates(db, by_row[3].id) == [] and _candidates(db, by_row[4].id) == []
    # Row 5: repeated tracking across shipments -> ambiguous, 2 candidates.
    assert by_row[5].match_status == "ambiguous"
    assert by_row[5].shipment_id is None
    assert len(_candidates(db, by_row[5].id)) == 2
    # Row 6: amount-only trap -- same amounts as row 1, no shipment.
    assert by_row[6].match_status == "unmatched"
    assert _candidates(db, by_row[6].id) == []
    # Row 7: blank tracking, weak fallback on order ref -> ambiguous,
    # candidate recorded, shipment NOT linked (human confirmation only).
    assert by_row[7].match_status == "ambiguous"
    assert by_row[7].shipment_id is None
    cands = _candidates(db, by_row[7].id)
    assert len(cands) == 1 and cands[0].match_basis == "weak_order_ref"
    assert cands[0].shipment_id == env["shipments"]["ORD-1007"].id
    # Row 8: data-quality states -- blank collected, invalid payout,
    # blank COD fee, blank other fee; delivery fee valid.
    assert by_row[8].reported_collected_state == "blank"
    assert by_row[8].reported_collected_poisha is None
    assert by_row[8].reported_payout_state == "invalid"
    assert by_row[8].reported_payout_poisha is None
    assert by_row[8].reported_cod_fee_state == "blank"
    assert by_row[8].reported_delivery_fee_state == "present_valid"
    assert by_row[8].reported_delivery_fee_poisha == 7000
    assert by_row[8].match_status == "strong_matched"
    # Row 9: explicit zeros are verified 0, not "missing".
    assert by_row[9].reported_collected_state == "explicit_zero"
    assert by_row[9].reported_collected_poisha == 0
    assert by_row[9].reported_payout_state == "explicit_zero"
    # Row 10 (blank line) has no statement line; row 11: weak fallback
    # window missed (15 days) -> unmatched; row 12: failed, no line.
    assert by_row[10] is None
    assert by_row[11].match_status == "unmatched"
    assert by_row[12] is None

    assert len(_errors(db, batch_id, "DUPLICATE_TRACKING")) == 2
    assert len(_errors(db, batch_id, "AMBIGUOUS_MATCH")) == 2  # rows 5 and 7
    assert len(_errors(db, batch_id, "VALUE_INVALID")) == 1  # row 8 payout
    assert len(_errors(db, batch_id, "VALUE_BLANK")) >= 4
    assert len(_errors(db, batch_id, "VALUE_UNPARSABLE")) == 1  # row 12


def test_amount_only_never_matches(db, env):
    """A row whose amounts equal an existing shipment's amounts, but
    whose tracking id matches nothing, must stay unmatched with zero
    candidates -- matching is never done on amount alone."""
    data = FIXTURE.read_bytes()
    summary = _run(db, env, data, key="m4-amount-only")
    rows, lines = _lines(db, uuid.UUID(summary["batch_id"]))
    line = lines[rows[5].id]  # row 6: TRK-9999, collected 100000
    assert line.reported_collected_poisha == 100000
    assert line.match_status == "unmatched"
    assert line.shipment_id is None
    assert _candidates(db, line.id) == []


def test_alt_headers_fixture_proves_mapping_is_configurable(db, env):
    summary = _run(db, env, ALT_FIXTURE.read_bytes(), mapping=dict(ALT_MAPPING), key="m4-alt")
    assert summary["status"] == "parsed"
    assert summary["row_count"] == 10
    rows, lines = _lines(db, uuid.UUID(summary["batch_id"]))
    by_row = {r.row_number: lines.get(r.id) for r in rows}
    assert by_row[1].match_status == "strong_matched"
    assert by_row[1].reported_collected_poisha == 100000
    assert by_row[5].match_status == "ambiguous"
    assert by_row[6].match_status == "unmatched"


# ---------------------------------------------------------------------------
# Mapping snapshots, files, idempotency
# ---------------------------------------------------------------------------

def test_mapping_snapshot_immutable_and_file_reused(db, env):
    data = FIXTURE.read_bytes()
    first = _run(db, env, data, mapping=dict(MAPPING), key="m4-snap-1")
    other_mapping = dict(MAPPING)
    other_mapping["tracking_id"] = "Tracking"  # valid mapping, header absent from the file
    second = _run(db, env, data, mapping=other_mapping, key="m4-snap-2")
    assert first["batch_id"] != second["batch_id"]
    assert second["mapping_snapshot"] == other_mapping
    # The first batch's snapshot was not touched by the second import.
    db.expire_all()
    assert db.get(ImportBatch, uuid.UUID(first["batch_id"])).mapping_snapshot == MAPPING
    # Identical bytes for the same merchant reuse the one file row.
    assert db.execute(select(FileObject).where(FileObject.merchant_id == env["merchant"].id)).scalars().all().__len__() == 1


def test_idempotent_replay_returns_same_batch(db, env):
    data = FIXTURE.read_bytes()
    first = _run(db, env, data, key="m4-replay")
    second = _run(db, env, data, key="m4-replay")
    assert second["idempotency_outcome"] == "replay"
    assert second["batch_id"] == first["batch_id"]
    batch_id = uuid.UUID(first["batch_id"])
    assert db.execute(select(ImportRow).where(ImportRow.import_batch_id == batch_id)).scalars().all().__len__() == 12


def test_idempotent_hash_mismatch_conflicts(db, env):
    data = FIXTURE.read_bytes()
    _run(db, env, data, key="m4-conflict")
    other_mapping = dict(MAPPING)
    other_mapping["tracking_id"] = "Tracking"  # valid mapping, different request hash
    with pytest.raises(csv_import.ImportFailedError):
        _run(db, env, data, mapping=other_mapping, key="m4-conflict")


def test_mapping_validation(db, env):
    data = FIXTURE.read_bytes()
    with pytest.raises(csv_import.MappingError):
        _run(db, env, data, mapping={"not_a_field": "Tracking ID"})
    with pytest.raises(csv_import.MappingError):
        _run(db, env, data, mapping={"tracking_id": "Tracking ID", "merchant_order_ref": "Tracking ID"})
    with pytest.raises(csv_import.MappingError):
        _run(db, env, data, mapping={})
    with pytest.raises(csv_import.MappingError):
        _run(db, env, data, mapping={"tracking_id": "   "})


def test_missing_column_is_column_absent_not_zero(db, env):
    """A mapped column that does not exist in the file gives
    column_absent + NULL for every row -- never a silent zero."""
    data = b"Tracking ID,Collected (poisha)\nTRK-1001,100000\n"
    summary = _run(db, env, data, key="m4-absent")
    assert summary["status"] == "parsed"
    rows, lines = _lines(db, uuid.UUID(summary["batch_id"]))
    line = lines[rows[0].id]
    assert line.reported_payout_state == "column_absent"
    assert line.reported_payout_poisha is None
    assert line.reported_collected_state == "present_valid"
    assert line.reported_collected_poisha == 100000
    assert any(e.error_code == "COLUMN_ABSENT" for e in _errors(db, uuid.UUID(summary["batch_id"])))


def test_no_membership_is_refused(db, env):
    outsider = User(email="outsider@example.com", password_hash="x", status="active")
    db.add(outsider)
    db.commit()
    with pytest.raises(csv_import.ImportAccessError):
        csv_import.run_import(
            db,
            user=outsider,
            merchant_id=env["merchant"].id,
            merchant_courier_account_id=env["account"].id,
            filename="x.csv",
            data=b"Tracking ID\nTRK-1\n",
            mapping={"tracking_id": "Tracking ID"},
            idempotency_key="m4-outsider",
        )


# ---------------------------------------------------------------------------
# Money parsing units
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,present,expected",
    [
        ("100000", True, (100000, "present_valid")),
        ("1,000,000", True, (1000000, "present_valid")),
        ("  7000 ", True, (7000, "present_valid")),
        ("0", True, (0, "explicit_zero")),
        ("000", True, (0, "explicit_zero")),
        ("-0", True, (0, "explicit_zero")),
        ("", True, (None, "blank")),
        ("   ", True, (None, "blank")),
        (None, True, (None, "blank")),
        ("1,5x0", True, (None, "invalid")),
        ("1200.50", True, (None, "invalid")),
        ("-500", True, (None, "invalid")),
        ("abc", True, (None, "invalid")),
        ("100000", False, (None, "column_absent")),
    ],
)
def test_parse_money(raw, present, expected):
    assert csv_import.parse_money(raw, column_present=present) == expected


def test_parse_money_negative_payout_only_with_permission():
    assert csv_import.parse_money("-500", column_present=True, allow_negative=True) == (-500, "present_valid")
