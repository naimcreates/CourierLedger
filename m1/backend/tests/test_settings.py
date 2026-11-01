"""Workspace settings tests (M1 Milestone 9, part 2) -- SQLite
fallback suite. Live-PostgreSQL coverage (RLS + runtime role +
tolerance flow over HTTP) lives in tests/test_settings_live_pg.py.

Covers: GET returns the effective defaults (tolerance 0, locale
"en_bn") when no settings row exists and never creates one; PATCH
creates the row (Owner/Admin only, CSRF); any active member may
read; non-members get 404 and never learn the workspace exists;
StrictInt validation (floats / strings / bools -> 422); explicit
JSON null is rejected with 422, never silently converted to a
default; and the tolerance actually flows into runs -- a run
created after PATCHing tolerance to 500 records tolerance 500
and a 200-poisha payout variance that was `mismatch` at tolerance
0 becomes `matched_with_tolerance`.

All fixtures are SYNTHETIC EXAMPLES of a generic statement layout --
no courier's real CSV template or rate is claimed anywhere here.
"""

import hashlib
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.auth_models import MerchantMembership, User
from app.db.models import (
    Courier,
    CourierRuleVersion,
    Merchant,
    MerchantCourierAccount,
    Order,
    ReconciliationFinding,
    ReconciliationResult,
    ReconciliationRun,
    Shipment,
    StatementLine,
    WorkspaceSettings,
)
from app.db.session import SessionLocal
from app.main import app
from app.services import csv_import, run_service
from tests.conftest import get_csrf, login, post, register

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

# One delivered shipment, COD 100000 poisha. Expected payout under
# the seeded rule (full_cod, 100bp, delivery 7000): 92000. Reported
# payout is 91800 -- 200 short -- so S = V_cond = V_total = -200
# with C = 0 and V_R = 0: `mismatch` at tolerance 0,
# `matched_with_tolerance` at tolerance 500.
TOL_CSV = (
    "Tracking ID,Order Ref,Collected (poisha),Payout (poisha),Delivery Fee (poisha),"
    "COD Fee (poisha),Return Fee (poisha),Other Fee (poisha),Status,Statement Date\n"
    "TRK-TOL,ORD-TOL,100000,91800,7000,1000,0,0,delivered,2026-10-15\n"
).encode()


@pytest.fixture()
def db(monkeypatch, tmp_path):
    # Keep test file writes out of the real backend/var/ tree.
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _client_for(email: str) -> TestClient:
    client = TestClient(app)
    register(client, email)
    login(client, email)
    return client


def _create_workspace(client: TestClient, name: str = "Settings Shop") -> str:
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _user_id(db, email: str) -> uuid.UUID:
    return db.execute(select(User.id).where(User.email == email)).scalar_one()


def _add_member(db, merchant_id: str, email: str, role: str) -> TestClient:
    """Register a second user over HTTP, then give them the requested
    role by direct insert (there is no member-invite API yet)."""
    client = _client_for(email)
    db.add(
        MerchantMembership(
            merchant_id=uuid.UUID(merchant_id),
            user_id=_user_id(db, email),
            role=role,
            status="active",
            invited_by_user_id=_user_id(db, "owner-settings@example.com"),
        )
    )
    db.commit()
    return client


def _patch(client: TestClient, path: str, payload: dict, csrf: bool = True):
    headers = {}
    if csrf:
        headers["X-CSRF-Token"] = get_csrf(client)
    return client.patch(path, json=payload, headers=headers)


def _settings_path(workspace_id: str) -> str:
    return f"/api/workspaces/{workspace_id}/settings"


# ---------------------------------------------------------------------------
# GET defaults / read access
# ---------------------------------------------------------------------------


def test_get_returns_effective_defaults_without_creating_a_row(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    resp = owner.get(_settings_path(workspace_id))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "workspace_id": workspace_id,
        "tolerance_poisha": 0,
        "locale": "en_bn",
        "is_default": True,
        "updated_by_user_id": None,
        "updated_at": None,
    }
    # Reading is a pure read: still no settings row afterwards.
    assert (
        db.execute(
            select(WorkspaceSettings).where(
                WorkspaceSettings.merchant_id == uuid.UUID(workspace_id)
            )
        ).scalar_one_or_none()
        is None
    )


def test_get_requires_membership_and_auth(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    outsider = _client_for("outsider-settings@example.com")
    assert outsider.get(_settings_path(workspace_id)).status_code == 404
    assert TestClient(app).get(_settings_path(workspace_id)).status_code == 401


def test_get_readable_by_any_active_member(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    member = _add_member(db, workspace_id, "member-settings@example.com", "member")
    resp = member.get(_settings_path(workspace_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["tolerance_poisha"] == 0
    assert resp.json()["is_default"] is True


# ---------------------------------------------------------------------------
# PATCH: who can write, and what is stored
# ---------------------------------------------------------------------------


def test_patch_owner_creates_row_and_get_reflects_it(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    resp = _patch(owner, _settings_path(workspace_id), {"tolerance_poisha": 500})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tolerance_poisha"] == 500
    assert body["locale"] == "en_bn"
    assert body["is_default"] is False
    assert body["updated_by_user_id"] == str(_user_id(db, "owner-settings@example.com"))
    assert body["updated_at"] is not None
    assert owner.get(_settings_path(workspace_id)).json()["tolerance_poisha"] == 500


def test_patch_admin_allowed_member_and_viewer_forbidden(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    admin = _add_member(db, workspace_id, "admin-settings@example.com", "admin")
    resp = _patch(admin, _settings_path(workspace_id), {"tolerance_poisha": 250})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tolerance_poisha"] == 250

    for email, role in [
        ("member-settings@example.com", "member"),
        ("viewer-settings@example.com", "viewer"),
    ]:
        client = _add_member(db, workspace_id, email, role)
        resp = _patch(client, _settings_path(workspace_id), {"tolerance_poisha": 1})
        assert resp.status_code == 403, (role, resp.text)
    # The forbidden attempts changed nothing.
    assert owner.get(_settings_path(workspace_id)).json()["tolerance_poisha"] == 250


def test_patch_nonmember_404_and_requires_csrf(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    outsider = _client_for("outsider-settings@example.com")
    assert _patch(outsider, _settings_path(workspace_id), {"tolerance_poisha": 1}).status_code == 404
    assert (
        _patch(owner, _settings_path(workspace_id), {"tolerance_poisha": 1}, csrf=False).status_code
        == 403
    )


def test_patch_locale_and_explicit_zero(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    resp = _patch(
        owner, _settings_path(workspace_id), {"tolerance_poisha": 500, "locale": "en"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "workspace_id": workspace_id,
        "tolerance_poisha": 500,
        "locale": "en",
        "is_default": False,
        "updated_by_user_id": str(_user_id(db, "owner-settings@example.com")),
        "updated_at": resp.json()["updated_at"],
    }
    # Sending 0 restores the default VALUE explicitly; the row still
    # exists (is_default stays False -- the row is the record).
    resp = _patch(owner, _settings_path(workspace_id), {"tolerance_poisha": 0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tolerance_poisha"] == 0
    assert resp.json()["is_default"] is False


def test_patch_empty_body_is_a_noop(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    resp = _patch(owner, _settings_path(workspace_id), {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_default"] is True
    assert (
        db.execute(
            select(WorkspaceSettings).where(
                WorkspaceSettings.merchant_id == uuid.UUID(workspace_id)
            )
        ).scalar_one_or_none()
        is None
    )


# ---------------------------------------------------------------------------
# PATCH validation: StrictInt, null rejection, unknown fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"tolerance_poisha": -1},
        {"tolerance_poisha": 1.5},
        {"tolerance_poisha": "500"},
        {"tolerance_poisha": True},
        {"tolerance_poisha": None},
        {"locale": "fr"},
        {"locale": ""},
        {"locale": None},
        {"tolerance_poisha": 100, "merchant_id": "00000000-0000-0000-0000-000000000000"},
        {"unknown_field": 1},
    ],
)
def test_patch_validation_rejects_bad_bodies(payload):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    resp = _patch(owner, _settings_path(workspace_id), payload)
    assert resp.status_code == 422, (payload, resp.status_code, resp.text)
    # Nothing was stored by a rejected PATCH.
    assert owner.get(_settings_path(workspace_id)).json()["is_default"] is True


def test_patch_partial_update_leaves_other_fields_unchanged(db):
    owner = _client_for("owner-settings@example.com")
    workspace_id = _create_workspace(owner)
    _patch(owner, _settings_path(workspace_id), {"tolerance_poisha": 500, "locale": "en"})
    resp = _patch(owner, _settings_path(workspace_id), {"locale": "en_bn"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tolerance_poisha"] == 500
    assert resp.json()["locale"] == "en_bn"


# ---------------------------------------------------------------------------
# Tolerance flows into runs
# ---------------------------------------------------------------------------


def _seed_tolerance_env(db, workspace_id: str, owner_email: str):
    """Seed the courier account, one delivered order/shipment and a
    merchant-confirmed rule version (full_cod, 100bp, delivery 7000)
    for the HTTP-created workspace. Returns (user, account, rule)."""
    user = db.execute(select(User).where(User.email == owner_email)).scalar_one()
    merchant = db.get(Merchant, uuid.UUID(workspace_id))
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.flush()
    order = Order(merchant_id=merchant.id, merchant_order_ref="ORD-TOL", order_cod_poisha=100000)
    db.add(order)
    db.flush()
    db.add(
        Shipment(
            merchant_id=merchant.id,
            order_id=order.id,
            merchant_courier_account_id=account.id,
            tracking_id_raw="TRK-TOL",
            tracking_id_normalised="TRK-TOL",
            outcome="delivered",
            expected_collectible_cod_poisha=100000,
            shipped_on=date(2026, 10, 10),
        )
    )
    rule = CourierRuleVersion(
        merchant_id=merchant.id,
        merchant_courier_account_id=account.id,
        version_no=1,
        status="draft",
        cod_fee_base="expected_collectible",
        cod_fee_rate_bps=100,
        delivery_fee_poisha=7000,
        return_fee_poisha=0,
        vat_mode="unknown",
        rule_definition={"other_fees_poisha": 0, "excluded_tax_poisha": 0},
        content_hash=hashlib.sha256(b"settings-tol-rule").hexdigest(),
        created_by_user_id=user.id,
    )
    db.add(rule)
    db.commit()
    return user, account, rule


def _result_for_tracking(db, run_id: str):
    run_id = uuid.UUID(run_id)
    result = db.execute(
        select(ReconciliationResult).where(ReconciliationResult.run_id == run_id)
    ).scalar_one()
    line = db.get(StatementLine, result.statement_line_id)
    assert line.tracking_id_normalised == "TRK-TOL"
    return result


def test_tolerance_flows_into_runs(db):
    owner_email = "owner-settings@example.com"
    owner = _client_for(owner_email)
    workspace_id = _create_workspace(owner)
    user, account, rule = _seed_tolerance_env(db, workspace_id, owner_email)

    batch = csv_import.run_import(
        db,
        user=user,
        merchant_id=uuid.UUID(workspace_id),
        merchant_courier_account_id=account.id,
        filename="statement.csv",
        data=TOL_CSV,
        mapping=dict(MAPPING),
        idempotency_key="settings-tol-imp",
    )
    assert batch["status"] in ("parsed", "ready"), batch

    # Run 1: no settings row yet, no explicit tolerance -> the
    # effective default 0. The 200-poisha payout variance is a
    # mismatch at tolerance 0.
    run1 = run_service.create_run_from_import(
        db,
        user=user,
        merchant_id=uuid.UUID(workspace_id),
        import_batch_id=batch["batch_id"],
        rule_version_id=rule.id,
        idempotency_key="settings-tol-run-1",
    )
    assert run1["status"] == "completed"
    assert run1["tolerance_poisha"] == 0
    assert run1["result_counts"] == {
        "matched": 0,
        "open": 0,
        "mismatch": 1,
        "needs_review": 0,
        "matched_with_tolerance": 0,
    }
    result1 = _result_for_tracking(db, run1["run_id"])
    assert result1.overall_status == "mismatch"
    assert result1.c_poisha == 0
    assert result1.s_poisha == -200
    assert result1.v_cond_poisha == -200
    assert result1.v_total_poisha == -200

    # PATCH the workspace tolerance to 500 over HTTP.
    resp = _patch(owner, _settings_path(workspace_id), {"tolerance_poisha": 500})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tolerance_poisha"] == 500

    # Run 2 from the same batch, no explicit tolerance: the run
    # picks up the workspace setting (500), stores it, and the same
    # 200-poisha variance is now within tolerance.
    run2 = run_service.create_run_from_import(
        db,
        user=user,
        merchant_id=uuid.UUID(workspace_id),
        import_batch_id=batch["batch_id"],
        rule_version_id=rule.id,
        idempotency_key="settings-tol-run-2",
    )
    assert run2["status"] == "completed"
    assert run2["tolerance_poisha"] == 500
    assert run2["result_counts"] == {
        "matched": 0,
        "open": 0,
        "mismatch": 0,
        "needs_review": 0,
        "matched_with_tolerance": 1,
    }
    stored_run2 = db.get(ReconciliationRun, uuid.UUID(run2["run_id"]))
    assert stored_run2.tolerance_poisha == 500
    result2 = _result_for_tracking(db, run2["run_id"])
    assert result2.overall_status == "matched_with_tolerance"
    assert result2.v_cond_poisha == -200  # gross difference still stored
    findings = db.execute(
        select(ReconciliationFinding).where(
            ReconciliationFinding.result_id == result2.id
        )
    ).scalars().all()
    assert findings, "findings are preserved even within tolerance"
    assert all(f.is_within_tolerance for f in findings)
