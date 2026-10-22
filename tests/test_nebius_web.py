"""Guarded NVIDIA web demo tests. Model calls are always mocked."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import web
from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import ProposedRepair, RepairAction, RepairPlan
from app.services.nebius_candidates import NebiusCandidateError
from app.tools.audit import audit_csv
from app.tools.fingerprint import calculate_sha256

TOKEN = "local_demo_token_not_an_api_key_123456789"
client = TestClient(web.app)
ENDPOINT = "/demo/nebius-candidates"


@pytest.fixture
def demo_auth(monkeypatch):
    monkeypatch.setenv(web.DEMO_ACCESS_ENV, TOKEN)
    return {web.DEMO_HEADER: TOKEN}


def fake_candidates(report, *, include_trim=True):
    fingerprint = report.fingerprint_sha256
    proposals = [
        RepairCandidate(
            candidate_id="dedupe",
            repair_plan=RepairPlan(
                source_fingerprint_sha256=fingerprint,
                summary="Remove exact duplicates from synthetic data.",
                actions=[
                    ProposedRepair(
                        action=RepairAction.REMOVE_EXACT_DUPLICATES,
                        justification="Audit confirms duplicate rows.",
                        columns=[],
                    )
                ],
            ),
        ),
    ]
    if include_trim:
        proposals.append(
            RepairCandidate(
                candidate_id="trim",
                repair_plan=RepairPlan(
                    source_fingerprint_sha256=fingerprint,
                    summary="Trim category whitespace.",
                    actions=[
                        ProposedRepair(
                            action=RepairAction.TRIM_OUTER_WHITESPACE,
                            justification="Compare a second supported strategy.",
                            columns=["category"],
                        )
                    ],
                ),
            )
        )
    return CandidateBatch(
        source_fingerprint_sha256=fingerprint,
        candidates=proposals,
    )


def forbidden(*args, **kwargs):
    raise AssertionError("No Nebius inference should run for this request.")


def test_successful_demo_has_no_release_and_no_source_mutation(
    monkeypatch, demo_auth
):
    source = Path(web.__file__).resolve().parents[1] / "demo_data" / "01_safe_repair.csv"
    fingerprint = calculate_sha256(source)
    calls = []

    def generate(report):
        calls.append(report.status)
        return fake_candidates(report)

    monkeypatch.setattr(web, "propose_nebius_candidates", generate)
    response = client.post(ENDPOINT, headers=demo_auth)

    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == ["QUARANTINED"]
    assert data["model"] == web.NEBIUS_MODEL
    assert data["candidate_count"] == 2
    assert data["recommendation"]["status"] == "RECOMMENDED"
    assert data["recommendation"]["recommended_candidate_id"] == "dedupe"
    assert [row["disposition"] for row in data["candidate_results"]] == [
        "IMPROVING", "NO_MEASURED_IMPROVEMENT"
    ]
    assert data["candidate_results"][0]["policy_status"] == "APPROVED"
    assert data["candidate_results"][0]["audit_after"]["quality_score"] == 100
    assert data["source_preserved"] is True
    assert data["release_authorized"] is False
    assert data["production_changes_applied"] is False
    assert calculate_sha256(source) == fingerprint
    assert TOKEN not in response.text
    assert "B200,Home,low_stock" not in response.text


@pytest.mark.parametrize("configured", ["", "short"])
def test_missing_or_weak_demo_token_disables_endpoint(
    monkeypatch, configured
):
    monkeypatch.setenv(web.DEMO_ACCESS_ENV, configured)
    monkeypatch.setattr(web, "propose_nebius_candidates", forbidden)
    response = client.post(ENDPOINT, headers={web.DEMO_HEADER: configured})
    assert response.status_code == 503


@pytest.mark.parametrize("headers", [
    {},
    {web.DEMO_HEADER: "wrong_token_0123456789012345678901234567"},
])
def test_unauthorized_caller_cannot_trigger_billable_inference(
    monkeypatch, demo_auth, headers
):
    monkeypatch.setattr(web, "propose_nebius_candidates", forbidden)
    response = client.post(ENDPOINT, headers=headers)
    assert response.status_code == 401


def test_missing_demo_header_is_not_enough_even_with_enabled_access(
    monkeypatch, demo_auth
):
    monkeypatch.setattr(web, "propose_nebius_candidates", forbidden)
    response = client.post(ENDPOINT)
    assert response.status_code == 401


def test_provider_failure_is_generic_and_does_not_leak_details(
    monkeypatch, demo_auth
):
    def failed(_report):
        raise NebiusCandidateError("SENSITIVE_REMOTE_DETAILS")

    monkeypatch.setattr(web, "propose_nebius_candidates", failed)
    response = client.post(ENDPOINT, headers=demo_auth)
    assert response.status_code == 502
    assert "SENSITIVE_REMOTE_DETAILS" not in response.text


def test_empty_model_batch_does_not_force_recommendation(
    monkeypatch, demo_auth
):
    monkeypatch.setattr(
        web, "propose_nebius_candidates",
        lambda report: CandidateBatch(
            source_fingerprint_sha256=report.fingerprint_sha256, candidates=[]
        )
    )
    response = client.post(ENDPOINT, headers=demo_auth)
    assert response.status_code == 200
    result = response.json()
    assert result["candidate_count"] == 0
    assert result["candidate_results"] == []
    assert result["recommendation"]["status"] == "NO_IMPROVING_CANDIDATE"
    assert result["recommendation"]["recommended_candidate_id"] is None


def test_policy_blocks_unsupported_proposal_from_execution(
    monkeypatch, demo_auth
):
    def generate(report):
        return CandidateBatch(
            source_fingerprint_sha256=report.fingerprint_sha256,
            candidates=[
                RepairCandidate(
                    candidate_id="drop_rows",
                    repair_plan=RepairPlan(
                        source_fingerprint_sha256=report.fingerprint_sha256,
                        summary="Unsafe synthetic proposal.",
                        actions=[ProposedRepair(
                            action=RepairAction.DROP_ROWS,
                            justification="Test that no approval is granted.",
                            columns=[],
                        )],
                    ),
                )
            ],
        )
    monkeypatch.setattr(web, "propose_nebius_candidates", generate)
    response = client.post(ENDPOINT, headers=demo_auth)
    assert response.status_code == 200
    row = response.json()["candidate_results"][0]
    assert row["policy_status"] == "REQUIRES_REVIEW"
    assert row["disposition"] == "EXCLUDED"
    assert row["audit_after"] is None
    assert response.json()["production_changes_applied"] is False


@pytest.mark.parametrize("content", [
    "item,category\nA,tools,unexpected\n",
    "item,category\nA,tools\nB,tools\n",
])
def test_blocked_or_ready_datasets_never_reach_nebius(
    tmp_path, monkeypatch, demo_auth, content
):
    source = tmp_path / "test.csv"
    source.write_text(content, encoding="utf-8")
    true_audit = audit_csv(source)
    assert true_audit.status in {"READY", "BLOCKED"}
    monkeypatch.setattr(web, "audit_csv", lambda _: true_audit)
    monkeypatch.setattr(web, "propose_nebius_candidates", forbidden)
    response = client.post(ENDPOINT, headers=demo_auth)
    assert response.status_code == 422


def test_candidate_evaluation_failure_does_not_leak_details(
    monkeypatch, demo_auth
):
    monkeypatch.setattr(web, "propose_nebius_candidates", fake_candidates)

    def failed(*args, **kwargs):
        raise RuntimeError("SECRET_INTERNAL_MESSAGE")

    monkeypatch.setattr(web, "recommend_candidate_batch", failed)
    response = client.post(ENDPOINT, headers=demo_auth)
    assert response.status_code == 409
    assert "SECRET_INTERNAL_MESSAGE" not in response.text


def test_existing_gemini_and_health_routes_untouched():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    # The existing Gemini demo route remains defined, without making a model call.
    assert any(
        route.path == "/demo/repair" for route in web.app.routes
    )
