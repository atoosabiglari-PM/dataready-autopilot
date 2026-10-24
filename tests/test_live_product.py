"""Real customer upload + live NVIDIA workflow: mocked model, real engine tests."""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app import live_product
from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import ProposedRepair, RepairAction, RepairPlan
from app.services.nebius_candidates import NebiusCandidateError

client = TestClient(live_product.app)
# Customer interaction requires no access code.
HEADERS = {}
CSV = b"item,category\nA,tools\nA,tools\nB,\nC,parts\n"


@pytest.fixture
def authorized(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", raising=False)
    monkeypatch.setenv(
        live_product.SIGNING_ENV,
        "independent_approval_signing_secret_9876543210",
    )
    client.cookies.clear()
    response = client.get("/")
    assert response.status_code == 200
    assert live_product.SESSION_COOKIE in client.cookies
    return HEADERS


def upload(content=CSV, name="customer.csv"):
    return {"file": (name, content, "application/octet-stream")}


def make_batch(report):
    sha = report.fingerprint_sha256
    assert sha
    return CandidateBatch(
        source_fingerprint_sha256=sha,
        candidates=[
            RepairCandidate(
                candidate_id="marker",
                repair_plan=RepairPlan(
                    source_fingerprint_sha256=sha,
                    summary="Normalize missing values.",
                    actions=[ProposedRepair(
                        action=RepairAction.STANDARDIZE_MISSING_MARKERS,
                        justification="The category is missing.",
                        columns=["category"],
                    )],
                ),
            ),
            RepairCandidate(
                candidate_id="dedupe",
                repair_plan=RepairPlan(
                    source_fingerprint_sha256=sha,
                    summary="Remove exact duplicate rows.",
                    actions=[ProposedRepair(
                        action=RepairAction.REMOVE_EXACT_DUPLICATES,
                        justification="A repeated inventory row was detected.",
                        columns=[],
                    )],
                ),
            ),
        ],
    )


def forbidden(*args, **kwargs):
    raise AssertionError("The model must not be called in this test.")


def test_customer_page_is_actual_upload_application():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Select your dataset" in response.text
    assert 'id="file-input"' in response.text
    assert "Run live NVIDIA analysis" in response.text
    assert "PUBLIC REPLAY MODE" not in response.text
    assert 'id="access-code"' not in response.text
    assert "X-DataReady-Demo-Token" not in response.text


def test_anonymous_browser_session_is_issued_without_login():
    fresh = TestClient(live_product.app)
    page = fresh.get("/")
    assert page.status_code == 200
    assert live_product.SESSION_COOKIE in fresh.cookies
    cookie = page.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert 'id="access-code"' not in page.text


def test_forged_guest_cookie_cannot_start_paid_inference(monkeypatch):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    fresh = TestClient(live_product.app)
    fresh.cookies.set(live_product.SESSION_COOKIE, "forged_cookie.invalid_signature")
    result = fresh.post("/api/analyze", files=upload())
    assert result.status_code == 403


def test_arbitrary_csv_audit_needs_no_model_token(monkeypatch):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post("/api/audit", files=upload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "LIVE_AUDIT"
    assert body["model_invoked"] is False
    assert body["original_audit"]["row_count"] == 4
    assert body["original_audit"]["quality_score"] == 80
    assert body["original_audit"]["duplicate_row_count"] == 1
    assert body["source_preserved"] is True


def test_live_nvidia_uses_uploaded_data_and_returns_verified_candidates(
    monkeypatch, authorized
):
    seen = []
    def fake_nvidia(report):
        seen.append((report.quality_score, report.row_count))
        return make_batch(report)
    monkeypatch.setattr(live_product, "propose_nebius_candidates", fake_nvidia)
    response = client.post("/api/analyze", files=upload(), headers=authorized)
    assert response.status_code == 200, response.text
    body = response.json()
    assert seen == [(80, 4)]
    assert body["mode"] == "LIVE_NVIDIA"
    assert body["model_invoked"] is True
    assert body["model"] == live_product.NVIDIA_MODEL
    assert body["original_rows_sent_to_ai"] is False
    assert body["candidate_count"] == 2
    assert body["original_audit"]["row_count"] == 4
    assert body["original_audit"]["quality_score"] == 80
    assert body["recommendation"]["status"] == "RECOMMENDED"
    assert body["recommendation"]["candidate_id"] == "dedupe"
    assert body["candidates"][0]["disposition"] == "NO_MEASURED_IMPROVEMENT"
    assert body["candidates"][1]["disposition"] == "IMPROVING"
    assert body["candidates"][1]["audit_after"]["quality_score"] == 90
    assert body["candidates"][1]["audit_after"]["row_count"] == 3
    assert body["approval_token"]
    assert body["production_changes_applied"] is False


def test_json_records_are_not_replaced_with_a_fixed_demo(monkeypatch):
    raw = b'[{"product":"K","availability":"yes"},{"product":"K","availability":"yes"}]'
    response = client.post("/api/audit", files=upload(raw, "customer.json"))
    assert response.status_code == 200, response.text
    original = response.json()["original_audit"]
    assert original["row_count"] == 2
    assert original["column_count"] == 2
    assert original["duplicate_row_count"] == 1
    assert original["quality_score"] == 90


def test_json_records_wrapper():
    raw = b'{"records":[{"item":"Z","qty":4},{"item":"Y","qty":2}]}'
    response = client.post("/api/audit", files=upload(raw, "data.json"))
    assert response.status_code == 200, response.text
    assert response.json()["original_audit"]["status"] == "READY"


@pytest.mark.parametrize("content", [
    b'[{"x":{"nested":"no"}}]',
    b'[{"item":"A","item":"B"}]',
    b'{"random":true}',
    b'[1,2,3]',
])
def test_invalid_or_nested_json_rejected(content):
    response = client.post("/api/audit", files=upload(content, "dataset.json"))
    assert response.status_code == 422


def test_xlsx_first_sheet_is_analyzed():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(["sku", "status"])
    sheet.append(["A", "ready"])
    sheet.append(["A", "ready"])
    buffer = BytesIO()
    wb.save(buffer)
    response = client.post("/api/audit", files=upload(buffer.getvalue(), "inventory.xlsx"))
    assert response.status_code == 200, response.text
    audit = response.json()["original_audit"]
    assert audit["row_count"] == 2
    assert audit["duplicate_row_count"] == 1


def test_excel_without_saved_formula_values_audits_but_requires_recalculation(
    monkeypatch, authorized
):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(["item", "quantity"])
    sheet.append(["A", "=2+2"])
    sheet.append(["A", "=2+2"])
    buffer = BytesIO()
    wb.save(buffer)

    def forbid_model(*args, **kwargs):
        raise AssertionError("Missing formula caches must not reach Nemotron.")

    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbid_model)
    audit = client.post("/api/audit", files=upload(buffer.getvalue(), "formula.xlsx"))
    assert audit.status_code == 200, audit.text
    body = audit.json()
    assert body["original_audit"]["row_count"] == 2
    assert body["original_audit"]["duplicate_row_count"] == 1
    assert len(body["import_warnings"]) == 1
    assert "no saved calculated value" in body["import_warnings"][0]

    response = client.post(
        "/api/analyze",
        files=upload(buffer.getvalue(), "formula.xlsx"),
        headers=authorized,
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["model_invoked"] is False
    assert report["recommendation"]["status"] == "REQUIRES_REVIEW"
    assert report["approval_token"] is None


def test_excel_saved_formula_values_are_analyzed_without_formula_execution():
    """Simulate an Excel-saved cached result, not an openpyxl-calculated value."""
    from zipfile import ZipFile
    import xml.etree.ElementTree as ET

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(["sku", "quantity"])
    sheet.append(["A", "=2+2"])
    sheet.append(["B", 4])
    original = BytesIO()
    wb.save(original)

    patched = BytesIO()
    with ZipFile(BytesIO(original.getvalue())) as source_archive:
        with ZipFile(patched, "w") as target_archive:
            for info in source_archive.infolist():
                data = source_archive.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    # Excel writers differ in XML whitespace and empty-value
                    # serialization. Patch the formula cache semantically.
                    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                    ET.register_namespace("", namespace)
                    document = ET.fromstring(data)
                    cell = document.find(
                        ".//{%s}c[@r='B2']" % namespace
                    )
                    assert cell is not None
                    formula = cell.find("{%s}f" % namespace)
                    assert formula is not None and formula.text == "2+2"
                    cached = cell.find("{%s}v" % namespace)
                    if cached is None:
                        cached = ET.SubElement(cell, "{%s}v" % namespace)
                    cached.text = "4"
                    data = ET.tostring(document, encoding="utf-8")
                target_archive.writestr(info, data)

    response = client.post(
        "/api/audit",
        files=upload(patched.getvalue(), "cached-formula.xlsx"),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["original_audit"]["status"] == "READY"
    assert result["original_audit"]["row_count"] == 2
    assert result["original_audit"]["duplicate_row_count"] == 0
    assert len(result["import_warnings"]) == 1
    assert "saved results" in result["import_warnings"][0]


def test_missing_formula_results_cannot_be_exported(monkeypatch, authorized):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["item", "qty"])
    ws.append(["A", "=1+2"])
    buffer = BytesIO()
    wb.save(buffer)
    response = client.post(
        "/api/export",
        files=upload(buffer.getvalue(), "missing-values.xlsx"),
        data={"approval_token": "invalid", "consent": "approve"},
        headers=authorized,
    )
    assert response.status_code == 422
    assert "formula results are missing" in response.json()["detail"]


@pytest.mark.parametrize("name", ["payload.exe", "data.zip", "data.xls"])
def test_unsupported_file_formats_fail_closed(name):
    response = client.post("/api/audit", files=upload(CSV, name))
    assert response.status_code == 422


def test_empty_csv_is_rejected():
    response = client.post("/api/audit", files=upload(b"", "blank.csv"))
    assert response.status_code == 422


def test_upload_size_limited():
    response = client.post(
        "/api/audit",
        files=upload(b"x" * (10 * 1024 * 1024 + 1)),
    )
    assert response.status_code in {413, 422}


def test_inference_requires_a_server_issued_browser_session(monkeypatch):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    fresh = TestClient(live_product.app)
    response = fresh.post("/api/analyze", files=upload())
    assert response.status_code == 403


def test_spending_limit_is_reported_without_calling_nebius(monkeypatch, authorized):
    def exhausted(_guest):
        raise live_product.PublicQuotaExceeded(
            "This browser has reached today's NVIDIA demo allowance."
        )

    monkeypatch.setattr(live_product, "reserve_public_inference", exhausted)
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post("/api/analyze", files=upload())
    assert response.status_code == 429
    assert "allowance" in response.json()["detail"]


def test_live_nvidia_reserves_one_server_quota_without_customer_code(
    monkeypatch, authorized
):
    reserved = []
    monkeypatch.setattr(
        live_product,
        "reserve_public_inference",
        lambda guest: reserved.append(guest),
    )
    monkeypatch.setattr(live_product, "propose_nebius_candidates", make_batch)
    response = client.post("/api/analyze", files=upload())
    assert response.status_code == 200, response.text
    assert len(reserved) == 1
    assert response.json()["recommendation"]["candidate_id"] == "dedupe"


def test_hosted_inference_disabled_until_quota_configured(monkeypatch, authorized):
    monkeypatch.setenv("K_SERVICE", "dataready-live")
    monkeypatch.delenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", raising=False)
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post("/api/analyze", files=upload())
    assert response.status_code == 503
    assert "SENSITIVE" not in response.text


def test_ready_input_skips_billable_model(monkeypatch, authorized):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post(
        "/api/analyze",
        files=upload(b"sku,status\nA,ready\nB,waiting\n"),
        headers=authorized,
    )
    assert response.status_code == 200
    assert response.json()["recommendation"]["status"] == "ALREADY_READY"
    assert response.json()["model_invoked"] is False


def test_sensitive_input_skips_billable_model_and_discloses_risk(
    monkeypatch, authorized
):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post(
        "/api/analyze",
        files=upload(b"email,status\nalice@example.com,ready\n"),
        headers=authorized,
    )
    assert response.status_code == 200
    assert response.json()["model_invoked"] is False
    assert response.json()["recommendation"]["status"] == "REQUIRES_REVIEW"


def test_blocked_input_does_not_reach_model(monkeypatch, authorized):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", forbidden)
    response = client.post(
        "/api/analyze",
        files=upload(b"sku,status\nA,ready,extra\n"),
        headers=authorized,
    )
    assert response.status_code == 200
    assert response.json()["recommendation"]["status"] == "BLOCKED"
    assert response.json()["model_invoked"] is False


def test_provider_error_does_not_leak_account_details(monkeypatch, authorized):
    def fail(_report):
        raise NebiusCandidateError("SENSITIVE_PROVIDER_DETAIL")
    monkeypatch.setattr(live_product, "propose_nebius_candidates", fail)
    response = client.post("/api/analyze", files=upload(), headers=authorized)
    assert response.status_code == 502
    assert "SENSITIVE_PROVIDER_DETAIL" not in response.text
    assert "NEBIUS_API_KEY" not in response.text


def run_and_get_token(monkeypatch, authorized):
    monkeypatch.setattr(live_product, "propose_nebius_candidates", make_batch)
    response = client.post("/api/analyze", files=upload(), headers=authorized)
    assert response.status_code == 200, response.text
    return response.json()["approval_token"]


def test_approved_csv_export_revalidates_and_preserves_input(
    monkeypatch, authorized
):
    token = run_and_get_token(monkeypatch, authorized)
    response = client.post(
        "/api/export",
        files=upload(),
        data={"approval_token": token, "consent": "approve"},
        headers=authorized,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content == b"item,category\nA,tools\nB,\nC,parts\n"
    assert b"A,tools\nA,tools" in CSV  # User's original input still exists.


def test_approval_token_is_bound_to_exact_source(monkeypatch, authorized):
    token = run_and_get_token(monkeypatch, authorized)
    response = client.post(
        "/api/export",
        files=upload(b"item,category\nX,tools\nX,tools\nB,\nC,parts\n"),
        data={"approval_token": token, "consent": "approve"},
        headers=authorized,
    )
    assert response.status_code == 403


def test_tampered_approval_token_is_rejected(monkeypatch, authorized):
    token = run_and_get_token(monkeypatch, authorized)
    corrupted = token[:-1] + ("a" if token[-1] != "a" else "b")
    response = client.post(
        "/api/export",
        files=upload(),
        data={"approval_token": corrupted, "consent": "approve"},
        headers=authorized,
    )
    assert response.status_code == 403


def test_approval_requires_explicit_user_consent(monkeypatch, authorized):
    token = run_and_get_token(monkeypatch, authorized)
    response = client.post(
        "/api/export",
        files=upload(),
        data={"approval_token": token, "consent": "decline"},
        headers=authorized,
    )
    assert response.status_code == 403


def test_approval_token_expires(monkeypatch, authorized):
    token = run_and_get_token(monkeypatch, authorized)
    current = live_product.time.time()
    monkeypatch.setattr(live_product.time, "time", lambda: current + 901)
    response = client.post(
        "/api/export",
        files=upload(),
        data={"approval_token": token, "consent": "approve"},
        headers=authorized,
    )
    assert response.status_code == 403


def test_replay_app_and_gemini_api_are_not_changed():
    from app import web, public_showcase
    assert client.get("/health").json()["service"] == "dataready-live"
    assert TestClient(web.app).get("/health").status_code == 200
    assert TestClient(public_showcase.app).get("/health").status_code == 200
    routes = client.get("/openapi.json").json()["paths"]
    assert "/api/analyze" in routes
    assert "/api/export" in routes
    assert "/demo/repair" not in routes
    assert "/demo/nebius-candidates" not in routes
