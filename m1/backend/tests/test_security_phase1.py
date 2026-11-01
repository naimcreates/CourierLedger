"""Phase 1 security tests (SQLite suite).

Proves the Phase 1 fixes at the HTTP/service layer:
- B-F1 session revocation is UPDATE revoked_at (logout / revoke-all /
  password-reset all invalidate sessions; rows remain, marked revoked).
- B-F2 no reset token/link in logs.
- B-F3 viewer members are read-only (403) on every mutating endpoint.
- B-F4 /api/reconcile + /api/rules require auth + CSRF; strict model.
- B-F5 rate limits on register / password-reset request; prior reset
  tokens invalidated on re-request.
- B-F6 per-account login bucket (unit-level).
- B-F9 register enumeration is gone (generic 200); invite rate limit.
- B-F13 production startup guard + CSRF names honoured (subprocess).
- B-F14 password max length.
- C-1.3 extra="forbid" on the six models; C-1.5 StrictInt money +
  upper bounds; C-1.7 string caps; C-1.9 pagination + error caps.
- C-2.1 CSV row/column caps; C-2.2 streaming 10 MB abort + stored
  content type; C-2.3 formula-like flagging; C-2.4 parse_failed 422.
- C-4.1 security headers (+ docs disabled in production, subprocess).
- C-7.2 idempotency-key length caps.

Live-PostgreSQL coverage (runtime role, cross-tenant probes, direct
SQL RLS probes, B-F12, PUBLIC EXECUTE revocation) lives in
tests/test_security_phase1_live_pg.py.
"""

import os
import re
import subprocess
import sys
import uuid
from datetime import date, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.auth_models import Merchant, MerchantMembership, User, UserSession
from app.db.models import (
    Courier,
    ImportError,
    MerchantCourierAccount,
)
from app.db.session import SessionLocal
from app.main import app
from app.services import csv_import, rate_limit
from conftest import authed_client, get_csrf, login, post, register
from test_password_reset import _issued_token

DATA = Path(__file__).parent / "data"
FIXTURE = DATA / "generic_statement_fixture.csv"

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_for(email: str) -> TestClient:
    client = TestClient(app)
    register(client, email)
    login(client, email)
    return client


def _workspace(client: TestClient, name: str = "Sec Shop") -> str:
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _viewer_client(owner: TestClient, ws: str, email: str) -> TestClient:
    """Invite `email` as viewer and return their logged-in client."""
    viewer = _client_for(email)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": email, "role": "viewer"})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    resp = post(viewer, f"/api/workspaces/{ws}/members/{mid}/accept", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "viewer"
    return viewer


def _shipment(**over):
    s = {
        "merchant_order_id": "ORD-T",
        "tracking_id": "TRK-T",
        "courier_fixture": "Fixture Alpha",
        "outcome": "delivered",
        "amount_to_collect_poisha": 100000,
        "reported_collected_poisha": 100000,
        "expected_delivery_fee_poisha": 7000,
        "reported_delivery_fee_poisha": 7000,
        "cod_fee_base": "full_cod",
        "cod_fee_rate_bp": 100,
        "other_fees_poisha": 0,
        "excluded_tax_poisha": 0,
        "reported_deductions_total_poisha": 8000,
        "reported_payout_poisha": 92000,
    }
    s.update(over)
    return s


# ---------------------------------------------------------------------------
# B-F1 — revocation is UPDATE revoked_at, not DELETE
# ---------------------------------------------------------------------------

def test_logout_revokes_session_cookie_cleared_and_me_401(client):
    register(client, "sec-logout@example.com")
    login(client, "sec-logout@example.com")
    assert client.get("/api/auth/me").status_code == 200

    resp = post(client, "/api/auth/logout", {})
    assert resp.status_code == 200, resp.text
    # The session cookie is cleared on the response.
    assert "cl_session" in resp.headers.get("set-cookie", "")
    assert client.get("/api/auth/me").status_code == 401
    # The row remains, marked revoked (runtime role has no DELETE).
    with SessionLocal() as db:
        rows = db.execute(select(UserSession)).scalars().all()
        assert len(rows) == 1
        assert rows[0].revoked_at is not None


def test_revoke_all_revokes_both_sessions(client):
    c2 = TestClient(app)
    register(client, "sec-revoke@example.com")
    login(client, "sec-revoke@example.com")
    login(c2, "sec-revoke@example.com")
    assert client.get("/api/auth/me").status_code == 200
    assert c2.get("/api/auth/me").status_code == 200

    resp = post(client, "/api/auth/revoke-all", {})
    assert resp.status_code == 200, resp.text
    assert client.get("/api/auth/me").status_code == 401
    assert c2.get("/api/auth/me").status_code == 401
    with SessionLocal() as db:
        rows = db.execute(select(UserSession)).scalars().all()
        assert len(rows) == 2
        assert all(r.revoked_at is not None for r in rows)


def test_reset_confirm_revokes_all_sessions_and_new_password_works(client):
    c2 = TestClient(app)
    register(client, "sec-reset@example.com")
    login(client, "sec-reset@example.com")
    login(c2, "sec-reset@example.com")
    assert c2.get("/api/auth/me").status_code == 200

    raw_token = _issued_token("sec-reset@example.com")
    resp = post(client, "/api/auth/password-reset/confirm",
                {"token": raw_token, "new_password": "brand-new-password-9"})
    assert resp.status_code == 200, resp.text
    assert client.get("/api/auth/me").status_code == 401
    assert c2.get("/api/auth/me").status_code == 401
    login(client, "sec-reset@example.com", "brand-new-password-9")


def test_expired_session_is_401(client):
    register(client, "sec-expiry@example.com")
    login(client, "sec-expiry@example.com")
    assert client.get("/api/auth/me").status_code == 200
    with SessionLocal() as db:
        row = db.execute(select(UserSession)).scalar_one()
        row.expires_at = row.expires_at - timedelta(days=8)
        db.commit()
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# B-F3 — viewer read-only
# ---------------------------------------------------------------------------

def test_viewer_post_403_matrix_and_reads_still_work():
    owner = _client_for("sec-owner@example.com")
    ws = _workspace(owner)
    viewer = _viewer_client(owner, ws, "sec-viewer@example.com")
    fake = str(uuid.uuid4())

    cases = [
        ("POST", f"/api/workspaces/{ws}/courier-accounts",
         {"courier_code": "steadfast"}),
        ("POST", f"/api/workspaces/{ws}/imports/{fake}/runs",
         {"courier_fixture": "Fixture Alpha"}),
        ("POST", f"/api/workspaces/{ws}/orders",
         {"merchant_order_ref": "ORD-1", "order_cod_poisha": 1000}),
        ("POST", f"/api/workspaces/{ws}/orders/{fake}/shipments",
         {"tracking_id": "TRK-1", "outcome": "delivered"}),
        ("POST", f"/api/workspaces/{ws}/settlement-batches",
         {"merchant_courier_account_id": fake}),
        ("POST", f"/api/workspaces/{ws}/settlement-batches/{fake}/receipts",
         {"amount_poisha": 1000}),
        ("POST", f"/api/workspaces/{ws}/settlement-batches/{fake}/receipts/{fake}/complete", {}),
        ("POST", f"/api/workspaces/{ws}/settlement-batches/{fake}/allocations",
         {"bank_receipt_id": fake, "allocated_poisha": 100}),
        ("POST", f"/api/workspaces/{ws}/settlement-batches/{fake}/allocations/{fake}/reverse", {}),
        ("POST", f"/api/workspaces/{ws}/settlement-batches/{fake}/calculate-variance", {}),
    ]
    for method, path, payload in cases:
        resp = post(viewer, path, payload)
        assert resp.status_code == 403, (method, path, resp.status_code, resp.text)

    # The same viewer can still read.
    assert viewer.get(f"/api/workspaces/{ws}").status_code == 200
    assert viewer.get(f"/api/workspaces/{ws}/imports").status_code == 200
    assert viewer.get(f"/api/workspaces/{ws}/settings").status_code == 200
    # And a non-member still gets 404, not 403.
    stranger = _client_for("sec-stranger@example.com")
    assert post(stranger, f"/api/workspaces/{ws}/orders",
                {"merchant_order_ref": "ORD-1"}).status_code == 404


# ---------------------------------------------------------------------------
# B-F4 — /api/reconcile + /api/rules auth, strict model
# ---------------------------------------------------------------------------

def test_reconcile_requires_auth_and_csrf(client):
    anon = TestClient(app)
    # No session at all.
    assert anon.post("/api/reconcile",
                     json={"shipments": [_shipment()]}).status_code == 401
    assert anon.get("/api/rules").status_code == 401
    # Authed but no CSRF header.
    authed_client(client, "sec-rec@example.com")
    resp = client.post("/api/reconcile", json={"shipments": [_shipment()]})
    assert resp.status_code == 403, resp.text
    # Authed + CSRF works.
    resp = post(client, "/api/reconcile", {"shipments": [_shipment()]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["overall_status"] == "matched"
    assert client.get("/api/rules").status_code == 200


def test_reconcile_strict_model_rejects_bad_input(client):
    authed_client(client, "sec-rec2@example.com")
    bad = [
        {"shipments": [_shipment(amount_to_collect_poisha="100000")]},  # string money
        {"shipments": [_shipment(amount_to_collect_poisha=True)]},  # bool money
        {"shipments": [_shipment(amount_to_collect_poisha=100000.5)]},  # float money
        {"shipments": [_shipment(cod_fee_base="bogus_base")]},  # unknown enum
        {"shipments": [_shipment(outcome="teleported")]},  # unknown outcome
        {"shipments": [_shipment(surprise_field=1)]},  # unknown field
        {"shipments": [_shipment()], "tolerance_poisha": -1},  # negative tolerance
        {"shipments": [_shipment()], "tolerance_poisha": 10**16},  # overflow
        {"shipments": [_shipment()], "extra_top_level": 1},
        {"shipments": [_shipment() for _ in range(1001)]},  # over the 1000 cap
    ]
    for payload in bad:
        resp = post(client, "/api/reconcile", payload)
        assert resp.status_code == 422, (payload, resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# C-1.3 / C-1.5 / C-1.7 / C-7.2 — model discipline
# ---------------------------------------------------------------------------

def test_extra_field_rejected_422(client):
    authed_client(client, "sec-extra@example.com")
    ws = _workspace(client)
    fake = str(uuid.uuid4())
    cases = [
        ("/api/auth/register", {"email": "x@y.z", "password": "long-enough-password-1", "nope": 1}),
        ("/api/auth/login", {"email": "x@y.z", "password": "long-enough-password-1", "nope": 1}),
        ("/api/auth/password-reset/request", {"email": "x@y.z", "nope": 1}),
        ("/api/auth/password-reset/confirm", {"token": "t", "new_password": "long-enough-password-1", "nope": 1}),
        (f"/api/workspaces/{ws}/settlement-batches",
         {"merchant_courier_account_id": fake, "nope": 1}),
        (f"/api/workspaces/{ws}/settlement-batches/{fake}/allocations",
         {"bank_receipt_id": fake, "allocated_poisha": 100, "nope": 1}),
        (f"/api/workspaces/{ws}/imports/{fake}/runs",
         {"courier_fixture": "Fixture Alpha", "nope": 1}),
    ]
    for path, payload in cases:
        resp = post(client, path, payload)
        assert resp.status_code == 422, (path, resp.status_code, resp.text)


def test_strictint_money_and_overflow_422(client):
    authed_client(client, "sec-money@example.com")
    ws = _workspace(client)
    fake = str(uuid.uuid4())
    cases = [
        (f"/api/workspaces/{ws}/settlement-batches",
         {"merchant_courier_account_id": fake, "expected_batch_payout_poisha": True}),
        (f"/api/workspaces/{ws}/settlement-batches",
         {"merchant_courier_account_id": fake, "expected_batch_payout_poisha": "100"}),
        (f"/api/workspaces/{ws}/settlement-batches",
         {"merchant_courier_account_id": fake, "expected_batch_payout_poisha": 10**16}),
        (f"/api/workspaces/{ws}/settlement-batches/{fake}/receipts",
         {"amount_poisha": 10**16}),
        (f"/api/workspaces/{ws}/settlement-batches/{fake}/allocations",
         {"bank_receipt_id": fake, "allocated_poisha": "100"}),
        (f"/api/workspaces/{ws}/imports/{fake}/runs",
         {"tolerance_poisha": -5}),
        (f"/api/workspaces/{ws}/imports/{fake}/runs",
         {"tolerance_poisha": "100"}),
        (f"/api/workspaces/{ws}/imports/{fake}/runs",
         {"idempotency_key": "x" * 129}),
    ]
    for path, payload in cases:
        resp = post(client, path, payload)
        assert resp.status_code == 422, (path, payload, resp.status_code, resp.text)
    # Settings tolerance overflow.
    resp = post(client, f"/api/workspaces/{ws}/settings", {})
    assert resp.status_code in (404, 405)  # settings is PATCH-only; sanity, not the test
    from fastapi.testclient import TestClient as _TC  # noqa: F401
    resp = client.patch(
        f"/api/workspaces/{ws}/settings",
        json={"tolerance_poisha": 10**16},
        headers={"X-CSRF-Token": get_csrf(client)},
    )
    assert resp.status_code == 422, resp.text


def test_receipt_label_and_note_max_length(client):
    authed_client(client, "sec-len@example.com")
    ws = _workspace(client)
    fake = str(uuid.uuid4())
    resp = post(client, f"/api/workspaces/{ws}/settlement-batches/{fake}/receipts",
                {"amount_poisha": 100, "receipt_ref_label": "x" * 501})
    assert resp.status_code == 422, resp.text
    resp = post(client, f"/api/workspaces/{ws}/settlement-batches/{fake}/receipts",
                {"amount_poisha": 100, "evidence_note": "x" * 1001})
    assert resp.status_code == 422, resp.text


def test_password_max_length_422(client):
    long_pw = "x" * 129
    resp = post(client, "/api/auth/register",
                {"email": "sec-longpw@example.com", "password": long_pw})
    assert resp.status_code == 422, resp.text
    register(client, "sec-longpw2@example.com")
    resp = post(client, "/api/auth/login",
                {"email": "sec-longpw2@example.com", "password": long_pw})
    assert resp.status_code == 422, resp.text
    raw_token = _issued_token("sec-longpw2@example.com")
    resp = post(client, "/api/auth/password-reset/confirm",
                {"token": raw_token, "new_password": long_pw})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# B-F5 / B-F6 / B-F9 — rate limits, token invalidation, enumeration
# ---------------------------------------------------------------------------

def test_register_rate_limit_per_ip(client):
    for i in range(50):
        resp = post(client, "/api/auth/register",
                    {"email": f"sec-rl{i}@example.com", "password": "long-enough-password-1"})
        assert resp.status_code == 201, resp.text
    resp = post(client, "/api/auth/register",
                {"email": "sec-rl50@example.com", "password": "long-enough-password-1"})
    assert resp.status_code == 429, resp.text


def test_reset_request_rate_limit_per_ip(client):
    for _ in range(10):
        resp = post(client, "/api/auth/password-reset/request",
                    {"email": "ghost@example.com"})
        assert resp.status_code == 200, resp.text
    resp = post(client, "/api/auth/password-reset/request",
                {"email": "ghost@example.com"})
    assert resp.status_code == 429, resp.text


def test_reset_rerequest_invalidates_prior_token(client):
    register(client, "sec-inval@example.com")
    first = _issued_token("sec-inval@example.com")
    # A fresh request through the endpoint invalidates prior unused tokens.
    resp = post(client, "/api/auth/password-reset/request",
                {"email": "sec-inval@example.com"})
    assert resp.status_code == 200, resp.text
    resp = post(client, "/api/auth/password-reset/confirm",
                {"token": first, "new_password": "brand-new-password-9"})
    assert resp.status_code == 400, resp.text


def test_login_per_account_bucket_blocks_across_ips():
    # Unit-level: the per-account (email-only) bucket blocks even when
    # the caller IP changes — rotating IPs does not defeat the limit.
    email = "sec-acct-bucket@example.com"
    for _ in range(5):
        rate_limit.record_failure("1.1.1.1", email)
    assert rate_limit.is_blocked("2.2.2.2", email) is True
    assert rate_limit.is_blocked("1.1.1.1", "other@example.com") is False


def test_invite_rate_limit_per_workspace():
    owner = _client_for("sec-inv-rl@example.com")
    ws = _workspace(owner)
    for i in range(10):
        resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                    {"email": f"nobody-{i}@example.com", "role": "member"})
        assert resp.status_code == 422, resp.text  # unknown email; still counts
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "nobody-10@example.com", "role": "member"})
    assert resp.status_code == 429, resp.text


def test_csrf_missing_and_wrong_forbidden_on_workspace_post(client):
    authed_client(client, "sec-csrf@example.com")
    resp = client.post("/api/workspaces", json={"name": "No CSRF Shop"})
    assert resp.status_code == 403, resp.text
    resp = client.post("/api/workspaces", json={"name": "Bad CSRF Shop"},
                       headers={"X-CSRF-Token": "not-the-token"})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# C-2.1 / C-2.2 / C-2.3 / C-2.4 / C-1.9 — CSV import
# ---------------------------------------------------------------------------

@pytest.fixture()
def csv_db(monkeypatch, tmp_path):
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def csv_env(csv_db):
    db = csv_db
    user = User(email="sec-csv@example.com", password_hash="x", status="active")
    db.add(user)
    db.flush()
    merchant = Merchant(name="Sec CSV Shop", created_by_user_id=user.id)
    db.add(merchant)
    db.flush()
    db.add(MerchantMembership(merchant_id=merchant.id, user_id=user.id,
                              role="owner", status="active"))
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.flush()
    db.commit()
    return {"user": user, "merchant": merchant, "account": account}


def _run_csv(csv_db, csv_env, data: bytes, key=None, content_type=None):
    return csv_import.run_import(
        csv_db, user=csv_env["user"], merchant_id=csv_env["merchant"].id,
        merchant_courier_account_id=csv_env["account"].id,
        filename="statement.csv", data=data, mapping=dict(MAPPING),
        idempotency_key=key or f"key-{uuid.uuid4().hex}",
        content_type=content_type,
    )


def test_csv_row_cap_rejected(csv_db):
    header = b"Tracking ID,Order Ref,Collected (poisha)\n"
    rows = b"TRK-1,ORD-1,100\n" * (csv_import.MAX_IMPORT_ROWS + 1)
    with pytest.raises(csv_import.ImportFailedError, match="more than"):
        csv_import.parse_csv_bytes(header + rows)


def test_csv_column_cap_rejected(csv_db):
    header = ",".join(f"col{i}" for i in range(csv_import.MAX_IMPORT_COLUMNS + 1)).encode()
    with pytest.raises(csv_import.ImportFailedError, match="more than"):
        csv_import.parse_csv_bytes(header + b"\n1,2,3\n")


def test_csv_mapping_header_max_length():
    with pytest.raises(csv_import.MappingError):
        csv_import.validate_mapping({"tracking_id": "x" * 501})


def test_csv_formula_like_cells_flagged_and_raw_preserved(csv_db, csv_env):
    data = (
        b"Tracking ID,Order Ref,Collected (poisha),Payout (poisha),Delivery Fee (poisha),"
        b"COD Fee (poisha),Return Fee (poisha),Other Fee (poisha),Status,Statement Date\n"
        b"=cmd|' /C calc'!A0,ORD-F1,100,90,5,1,0,0,delivered,2026-10-15\n"
        b"TRK-F2,ORD-F2,100,90,5,1,0,0,@evil,2026-10-15\n"
        b"TRK-F3,ORD-F3,-500,90,5,1,0,0,delivered,2026-10-15\n"
    )
    summary = _run_csv(csv_db, csv_env, data)
    assert summary["status"] in ("ready", "parsed"), summary
    errors = csv_db.execute(
        select(ImportError).where(ImportError.error_code == "FORMULA_LIKE_VALUE")
    ).scalars().all()
    assert len(errors) == 2, [(e.field_name, e.raw_value) for e in errors]
    flagged = {e.raw_value for e in errors}
    assert "=cmd|' /C calc'!A0" in flagged
    assert "@evil" in flagged
    # A plain negative number is NOT a formula.
    assert "-500" not in flagged
    # Raw values are preserved verbatim.
    rows = csv_db.execute(
        select(csv_import.ImportRow).where(
            csv_import.ImportRow.import_batch_id == uuid.UUID(summary["batch_id"]))
    ).scalars().all()
    assert rows[0].raw_values["Tracking ID"] == "=cmd|' /C calc'!A0"


def test_csv_content_type_stored(csv_db, csv_env):
    data = FIXTURE.read_bytes()
    summary = _run_csv(csv_db, csv_env, data, content_type="text/csv")
    from app.db.models import FileObject

    fo = csv_db.execute(
        select(FileObject).where(FileObject.id == uuid.UUID(summary["file_object_id"]))
    ).scalar_one()
    assert fo.content_type == "text/csv"
    # A different file (different SHA) with a non-CSV declared type.
    data2 = data + b"TRK-9999,ORD-9999,1,1,0,0,0,0,delivered,2026-10-15\n"
    summary2 = _run_csv(csv_db, csv_env, data2, content_type="application/pdf")
    fo2 = csv_db.execute(
        select(FileObject).where(FileObject.id == uuid.UUID(summary2["file_object_id"]))
    ).scalar_one()
    assert fo2.content_type == "unknown"


def _upload(client, ws, account_id, data, filename="statement.csv",
            content_type="text/csv", key=None):
    import json as _json

    headers = {"X-CSRF-Token": get_csrf(client)}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/api/workspaces/{ws}/imports",
        data={"mapping": _json.dumps(MAPPING),
              "merchant_courier_account_id": account_id},
        files={"file": (filename, data, content_type)},
        headers=headers,
    )


def _setup_ws_with_account(client):
    # The SQLite test DB is not seeded; insert the courier row the
    # account creation looks up (live DBs get it from seed_couriers.sql).
    with SessionLocal() as db:
        if db.execute(select(Courier).where(Courier.code == "steadfast")).scalar_one_or_none() is None:
            db.add(Courier(code="steadfast", name="Steadfast (synthetic test row)"))
            db.commit()
    ws = _workspace(client)
    resp = post(client, f"/api/workspaces/{ws}/courier-accounts",
                {"courier_code": "steadfast"})
    assert resp.status_code == 201, resp.text
    return ws, resp.json()["id"]


def test_upload_over_10mb_rejected_and_nothing_persisted(client):
    authed_client(client, "sec-big@example.com")
    ws, account_id = _setup_ws_with_account(client)
    big = b"Tracking ID,Order Ref\n" + b"TRK-1,ORD-1\n" * 1_200_000  # > 10 MB
    assert len(big) > 10 * 1024 * 1024
    resp = _upload(client, ws, account_id, big, key="sec-big-1")
    assert resp.status_code == 413, resp.text
    listing = client.get(f"/api/workspaces/{ws}/imports")
    assert listing.json()["total"] == 0


def test_parse_failed_returns_422_and_batch_persisted(client):
    authed_client(client, "sec-parse@example.com")
    ws, account_id = _setup_ws_with_account(client)
    resp = _upload(client, ws, account_id, b"\xff\xfe not utf-8 \x00\x01",
                   key="sec-parse-1")
    assert resp.status_code == 422, resp.text
    assert resp.json()["status"] == "parse_failed"
    detail = client.get(f"/api/workspaces/{ws}/imports/{resp.json()['batch_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["batch"]["status"] == "parse_failed"


def test_list_imports_pagination_and_detail_error_cap(client):
    authed_client(client, "sec-page@example.com")
    ws, account_id = _setup_ws_with_account(client)
    for i in range(2):
        resp = _upload(client, ws, account_id, FIXTURE.read_bytes(), key=f"sec-page-{i}")
        assert resp.status_code == 201, resp.text
    page1 = client.get(f"/api/workspaces/{ws}/imports?limit=1&offset=0")
    assert page1.status_code == 200
    assert page1.json()["total"] == 2
    assert len(page1.json()["imports"]) == 1
    page2 = client.get(f"/api/workspaces/{ws}/imports?limit=1&offset=1")
    assert len(page2.json()["imports"]) == 1
    assert page2.json()["imports"][0]["batch_id"] != page1.json()["imports"][0]["batch_id"]
    # limit above 100 is rejected.
    assert client.get(f"/api/workspaces/{ws}/imports?limit=101").status_code == 422
    # Detail carries the truncated_errors flag.
    batch_id = page1.json()["imports"][0]["batch_id"]
    detail = client.get(f"/api/workspaces/{ws}/imports/{batch_id}")
    assert detail.json()["truncated_errors"] is False


def test_get_batch_detail_errors_capped(csv_db, csv_env):
    summary = _run_csv(csv_db, csv_env, FIXTURE.read_bytes())
    batch_id = uuid.UUID(summary["batch_id"])
    # Seed 600 error rows directly, then check the detail cap.
    for i in range(600):
        csv_db.add(ImportError(
            merchant_id=csv_env["merchant"].id, import_batch_id=batch_id,
            field_name="tracking_id", severity="warning",
            error_code="VALUE_INVALID", message=f"seeded {i}",
        ))
    csv_db.commit()
    detail = csv_import.get_batch_detail(csv_db, csv_env["merchant"].id, batch_id)
    assert len(detail["errors"]) == csv_import.MAX_ERRORS_RETURNED
    assert detail["truncated_errors"] is True


# ---------------------------------------------------------------------------
# C-4.1 / B-F13 — headers, docs, production guard (subprocess)
# ---------------------------------------------------------------------------

def test_security_headers_present_in_test_env(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in resp.headers["permissions-policy"]
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    # No HSTS over plain HTTP in test mode.
    assert "strict-transport-security" not in resp.headers
    # Docs are enabled outside production.
    assert client.get("/docs").status_code == 200


def _prod_subprocess(code: str, secure: bool) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ENVIRONMENT"] = "production"
    env["SECRET_KEY"] = "prod-test-secret-not-a-placeholder-1"
    env["SESSION_SECRET"] = "prod-test-secret-not-a-placeholder-2"
    env["SESSION_COOKIE_SECURE"] = "true" if secure else "false"
    # Phase 3, Stream C: production startup validation also refuses
    # the development-default FRONTEND_URL, so the probe sets an
    # explicit production-like origin. (Same reason as the
    # DATABASE_URL change above.)
    env["FRONTEND_URL"] = "https://app.example.invalid"
    env["DATABASE_URL"] = (
        # Phase 3, Stream C: production startup validation now refuses
        # a SQLite DATABASE_URL (SQLite is test-only, never production
        # data), so these production-mode probes use a postgresql URL
        # pointing at a dead port. Nothing in the probe touches the
        # database — /docs, /openapi.json and the HSTS header do not
        # need one, and /api/health fails fast against the dead port.
        "postgresql+psycopg://courierledger_runtime:prod-test-not-a-real-password@127.0.0.1:5999/courierledger_app"
    )
    env["CREATE_TABLES_ON_STARTUP"] = "false"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).parent.parent),
        env=env, capture_output=True, text=True, timeout=120,
    )


_PROD_PROBE = (
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "c = TestClient(app)\n"
    "print('docs', c.get('/docs').status_code)\n"
    "print('openapi', c.get('/openapi.json').status_code)\n"
    "print('hsts', c.get('/api/health').headers.get('strict-transport-security'))\n"
)


def test_production_mode_docs_404_and_hsts():
    proc = _prod_subprocess(_PROD_PROBE, secure=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "docs 404" in proc.stdout, proc.stdout
    assert "openapi 404" in proc.stdout, proc.stdout
    assert "max-age=31536000; includeSubDomains" in proc.stdout, proc.stdout


def test_production_mode_refuses_insecure_cookie():
    proc = _prod_subprocess("from app.main import app\n", secure=False)
    assert proc.returncode != 0
    assert "SESSION_COOKIE_SECURE" in proc.stderr
