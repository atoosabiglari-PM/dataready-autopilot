"""Customer Excel layout selection and summary safety tests.

These tests use synthetic spreadsheets only. No external model calls occur.
"""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app import live_product
from app.services import excel_layout

client = TestClient(live_product.app)


def workbook_bytes() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    cover = workbook.active
    cover.title = "Instructions"
    cover.append(["This first sheet is not the hotel data."])
    cover.append(["Follow the table sheet."])

    hotels = workbook.create_sheet("Hotel Data")
    hotels.append(["Customer outreach worksheet", None])
    hotels.append(["Select row 3 for headers", None])
    hotels.append(["item", "category"])
    hotels.append(["A", "tools"])
    hotels.append(["A", "tools"])
    hotels.append(["B", "parts"])
    result = BytesIO()
    workbook.save(result)
    return result.getvalue()


def test_excel_layout_preview_lists_sheets_and_row_numbers_without_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Inspecting the workbook must never call a model.")

    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post(
        "/api/workbook-layout",
        files={"file": ("test.xlsx", workbook_bytes())},
    )
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    info = response.json()
    assert info["no_model_call"] is True
    assert info["default_sheet"] == "Instructions"
    assert [sheet["name"] for sheet in info["sheets"]] == [
        "Instructions", "Hotel Data",
    ]
    header = next(row for row in info["sheets"][1]["preview_rows"]
                  if row["row_number"] == 3)
    assert header["values"] == ["item", "category"]


def test_selecting_real_table_row_removes_fake_unnamed_header_findings():
    response = client.post(
        "/api/audit",
        files={"file": ("hotel.xlsx", workbook_bytes())},
        data={"sheet_name": "Hotel Data", "header_row": "3"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    audit = result["original_audit"]
    assert audit["row_count"] == 3
    assert audit["column_count"] == 2
    assert audit["duplicate_row_count"] == 1
    assert audit["quality_score"] == 90
    assert audit["status"] == "QUARANTINED"
    assert all(issue["code"] != "SUSPICIOUS_COLUMN_NAME" for issue in audit["issues"])
    assert any("header at row 3" in note for note in result["import_warnings"])


def test_missing_sheet_selection_is_rejected_not_silently_replaced():
    response = client.post(
        "/api/audit",
        files={"file": ("test.xlsx", workbook_bytes())},
        data={"sheet_name": "Unknown", "header_row": "3"},
    )
    assert response.status_code == 422
    assert "not found" in response.json()["detail"]


def test_unsupported_header_row_is_rejected():
    response = client.post(
        "/api/audit",
        files={"file": ("test.xlsx", workbook_bytes())},
        data={"sheet_name": "Hotel Data", "header_row": "0"},
    )
    assert response.status_code == 422


def test_csv_must_not_accept_excel_sheet_settings():
    response = client.post(
        "/api/audit",
        files={"file": ("simple.csv", b"item,category\nA,tools\n")},
        data={"sheet_name": "Hotel Data", "header_row": "3"},
    )
    assert response.status_code == 422


def test_layout_preview_redacts_customer_email_and_phone():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Hotel", "Phone", "Email"])
    ws.append(["Acme", "650-555-0199", "person@example.com"])
    buf = BytesIO()
    wb.save(buf)
    response = client.post(
        "/api/workbook-layout",
        files={"file": ("contacts.xlsx", buf.getvalue())},
    )
    assert response.status_code == 200, response.text
    serialized = response.text
    assert "650-555-0199" not in serialized
    assert "person@example.com" not in serialized
    assert "[sensitive value]" in serialized


def test_live_nvidia_analyzes_selected_excel_table_and_export_rechecks_it(
    monkeypatch,
):
    from app.core.candidates import CandidateBatch, RepairCandidate
    from app.core.policy import ProposedRepair, RepairAction, RepairPlan

    sign = "fixed_signing_key_for_excel_demo_tests_123456789"
    monkeypatch.setenv(live_product.SIGNING_ENV, sign)
    monkeypatch.delenv("K_SERVICE", raising=False)
    client.cookies.clear()
    assert client.get("/").status_code == 200
    monkeypatch.setattr(live_product, "reserve_public_inference", lambda _guest: None)

    def candidates(report):
        assert report.row_count == 3
        assert report.quality_score == 90
        h = report.fingerprint_sha256
        return CandidateBatch(
            source_fingerprint_sha256=h,
            candidates=[RepairCandidate(
                candidate_id="dedupe",
                repair_plan=RepairPlan(
                    source_fingerprint_sha256=h,
                    summary="Remove duplicate hotel inventory rows.",
                    actions=[ProposedRepair(
                        action=RepairAction.REMOVE_EXACT_DUPLICATES,
                        justification="The selected table has an exact duplicate.",
                        columns=[],
                    )],
                ),
            )],
        )

    monkeypatch.setattr(live_product, "propose_nebius_candidates", candidates)
    raw = workbook_bytes()
    request = {"file": ("hotel.xlsx", raw)}
    selection = {"sheet_name": "Hotel Data", "header_row": "3"}
    result = client.post("/api/analyze", files=request, data=selection)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["original_audit"]["row_count"] == 3
    assert data["recommendation"]["candidate_id"] == "dedupe"
    assert data["candidates"][0]["audit_after"]["row_count"] == 2
    approval = data["approval_token"]

    restored = client.post(
        "/api/export",
        files=request,
        data={**selection, "approval_token": approval, "consent": "approve"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.content == b"item,category\nA,tools\nB,parts\n"

    # Approval is bound to the normalized table, not to a different
    # worksheet/header mapping of the same uploaded Excel workbook.
    other = client.post(
        "/api/export",
        files=request,
        data={
            "sheet_name": "Hotel Data", "header_row": "1",
            "approval_token": approval, "consent": "approve",
        },
    )
    assert other.status_code == 403


def test_customer_interface_has_layout_picker_and_grouped_finding_controls():
    result = client.get("/")
    assert result.status_code == 200
    html = result.text
    assert 'id="excel-sheet"' in html
    assert 'id="excel-header-row"' in html
    assert 'id="findings-groups"' in html
    assert "detailed findings" in html
    assert "SHALL NOT" not in html


def test_invalid_workbook_layout_request_never_invokes_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not contact NVIDIA for layout inspection.")

    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post(
        "/api/workbook-layout",
        files={"file": ("file.csv", b"a,b\n1,2\n")},
    )
    assert response.status_code == 422
