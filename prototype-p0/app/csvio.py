"""CSV upload, parsing and column mapping for the P0 prototype.

The documented schema is in README.md. Headers in an uploaded file are
mapped to canonical fields; unmapped or missing columns become
data-quality warnings, never silent defaults. Money columns are kept as
raw text until the engine parses them, so blank / invalid states survive.
"""
from __future__ import annotations

import csv
import io

# Canonical field -> accepted header spellings (case-insensitive, trimmed).
CANONICAL_FIELDS = {
    "merchant_order_id": ["merchant_order_id", "order_id", "merchant order id"],
    "tracking_id": ["tracking_id", "tracking id", "tracking"],
    "courier_fixture": ["courier_fixture", "courier", "courier_fixture_name"],
    "outcome": ["outcome", "shipment_outcome", "status"],
    "amount_to_collect_poisha": ["amount_to_collect_poisha", "amount_to_collect",
                                  "order_cod_poisha", "original_cod_poisha"],
    "documented_partial_poisha": ["documented_partial_poisha", "partial_amount_poisha"],
    "reported_collected_poisha": ["reported_collected_poisha", "reported_collected",
                                   "collected_poisha"],
    "expected_delivery_fee_poisha": ["expected_delivery_fee_poisha", "expected_delivery_fee"],
    "reported_delivery_fee_poisha": ["reported_delivery_fee_poisha", "reported_delivery_fee"],
    "contractual_delivery_fee_poisha": ["contractual_delivery_fee_poisha",
                                         "contractual_delivery_fee"],
    "contractual_delivery_fee_confirmed": ["contractual_delivery_fee_confirmed",
                                            "d_c_confirmed"],
    "contractual_delivery_fee_evidence": ["contractual_delivery_fee_evidence",
                                           "d_c_evidence",
                                           "contract_confirmation_evidence"],
    "cod_fee_base": ["cod_fee_base", "fee_base"],
    "cod_fee_rate_bp": ["cod_fee_rate_bp", "cod_fee_rate_bps", "rate_bp"],
    "other_fees_poisha": ["other_fees_poisha", "other_fees"],
    "return_fee_poisha": ["return_fee_poisha", "return_fee"],
    "excluded_tax_poisha": ["excluded_tax_poisha", "excluded_tax"],
    "reported_excluded_tax_poisha": ["reported_excluded_tax_poisha",
                                      "reported_excluded_tax"],
    "reported_deductions_total_poisha": ["reported_deductions_total_poisha",
                                          "reported_deductions_total",
                                          "total_deductions_poisha"],
    "reported_cod_fee_poisha": ["reported_cod_fee_poisha", "reported_cod_fee"],
    "reported_other_fee_poisha": ["reported_other_fee_poisha", "reported_other_fee"],
    "reported_return_fee_poisha": ["reported_return_fee_poisha", "reported_return_fee"],
    "reported_payout_poisha": ["reported_payout_poisha", "reported_payout", "payout_poisha"],
    "vat_mode": ["vat_mode", "tax_mode"],
    "vat_formula": ["vat_formula", "included_tax_formula"],
    "vat_rate_bp": ["vat_rate_bp", "vat_rate_bps"],
    "note": ["note", "notes"],
}

# Fee-line columns: canonical money field -> fee type for the engine.
FEE_LINE_COLUMNS = {
    "reported_delivery_fee_poisha": "delivery",
    "reported_cod_fee_poisha": "cod_fee",
    "reported_return_fee_poisha": "return_fee",
    "reported_other_fee_poisha": "other",
}

INT_FIELDS = {
    "amount_to_collect_poisha", "documented_partial_poisha",
    "reported_collected_poisha", "expected_delivery_fee_poisha",
    "reported_delivery_fee_poisha", "contractual_delivery_fee_poisha",
    "cod_fee_rate_bp", "other_fees_poisha", "return_fee_poisha",
    "excluded_tax_poisha", "reported_excluded_tax_poisha",
    "reported_deductions_total_poisha", "reported_cod_fee_poisha",
    "reported_other_fee_poisha", "reported_return_fee_poisha",
    "reported_payout_poisha", "vat_rate_bp",
}


def _norm(header: str) -> str:
    return (header or "").strip().lower()


def build_mapping(headers, mapping: dict | None = None):
    """Map uploaded headers to canonical fields.

    mapping (optional, from the UI) is {uploaded_header: canonical_field}.
    Otherwise auto-map by accepted spellings. Returns
    (header_to_canonical, unmapped_headers, missing_canonical_fields).
    """
    header_to_canonical = {}
    lookup = {}
    for canon, spellings in CANONICAL_FIELDS.items():
        for sp in spellings:
            lookup[sp] = canon
    for h in headers:
        key = mapping.get(h) if mapping else None
        if key is None and mapping:
            key = mapping.get(h.strip())
        if key and key in CANONICAL_FIELDS:
            header_to_canonical[h] = key
        elif _norm(h) in lookup:
            header_to_canonical[h] = lookup[_norm(h)]
    unmapped = [h for h in headers if h not in header_to_canonical]
    present = set(header_to_canonical.values())
    missing = [c for c in CANONICAL_FIELDS if c not in present]
    return header_to_canonical, unmapped, missing


def _parse_int_field(raw):
    """CSV int field: blank -> None (missing, not zero); else int or None.

    Non-integer text is returned as the raw string so the engine can
    record it as invalid_value instead of inventing a number.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    compact = text.replace(",", "").replace(" ", "")
    try:
        return int(compact)
    except ValueError:
        return text  # invalid: engine/shipment layer flags it


def _parse_bool(raw):
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("yes", "true", "1", "y"):
        return True
    if text in ("no", "false", "0", "n"):
        return False
    return None


def parse_csv(text: str, mapping: dict | None = None):
    """Parse CSV text into (shipments, report).

    Each shipment dict keeps fee-line raw values in `fee_lines` and the
    raw text of money fields where parsing failed, so nothing is lost.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], {"headers": [], "unmapped_headers": [],
                    "missing_fields": list(CANONICAL_FIELDS),
                    "row_count": 0, "errors": ["Empty CSV: no header row."]}
    headers = [h for h in reader.fieldnames if h is not None]
    if len(headers) != len(set(headers)):
        dupes = sorted({h for h in headers if headers.count(h) > 1})
        return [], {"headers": headers, "unmapped_headers": [],
                    "missing_fields": list(CANONICAL_FIELDS), "row_count": 0,
                    "errors": [f"Duplicate header(s): {', '.join(dupes)}"]}
    header_to_canonical, unmapped, missing = build_mapping(headers, mapping)
    shipments = []
    errors = []
    for row_no, row in enumerate(reader, start=2):
        if all((v is None or str(v).strip() == "") for v in row.values()):
            continue  # skip blank rows
        canon: dict = {}
        for header, value in row.items():
            if header is None or header not in header_to_canonical:
                continue
            field = header_to_canonical[header]
            canon[field] = value
        shipment: dict = {}
        for field, value in canon.items():
            if field in INT_FIELDS:
                shipment[field] = _parse_int_field(value)
            elif field == "contractual_delivery_fee_confirmed":
                shipment[field] = _parse_bool(value)
            else:
                shipment[field] = str(value).strip() if value is not None else None
        # Fee lines: every fee-line column present in the header becomes a
        # line with its raw text and column_present=True. A column absent
        # from the header is NOT invented here; the caller may add it as
        # missing_column lines if the template requires it.
        fee_lines = []
        for field, fee_type in FEE_LINE_COLUMNS.items():
            header = next((h for h, c in header_to_canonical.items() if c == field), None)
            if header is not None:
                fee_lines.append({"fee_type": fee_type,
                                  "raw": canon.get(field),
                                  "column_present": True})
        if fee_lines:
            shipment["fee_lines"] = fee_lines
        # Invalid INT fields stay as raw strings in the shipment; the
        # engine's _clean_int turns them into NULL + DQ_INVALID_VALUE.
        shipments.append(shipment)
    return shipments, {"headers": headers, "unmapped_headers": unmapped,
                       "missing_fields": missing, "row_count": len(shipments),
                       "errors": errors}
