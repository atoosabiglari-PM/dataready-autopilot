"""Public showcase tests. No paid inference; real deterministic trials."""

from fastapi.testclient import TestClient

from app import showcase, web
from app.tools.fingerprint import calculate_sha256

client = TestClient(web.app)


def forbid_model(*args, **kwargs):
    raise AssertionError("Public showcase must never call Nebius or Gemini.")


def test_showcase_page_is_public_and_clear_about_replay(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "unused")
    monkeypatch.setattr(
        "app.services.nebius_candidates.propose_nebius_candidates", forbid_model
    )
    response = client.get("/showcase")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "DataReady" in response.text
    assert "PUBLIC REPLAY MODE" in response.text
    assert "No live model call or billable API request" in response.text
    assert "/showcase/evidence" in response.text
    assert "no-store" in response.headers["cache-control"]


def test_public_evidence_is_reproducible_and_does_not_call_model(monkeypatch):
    monkeypatch.setattr(
        "app.services.nebius_candidates.propose_nebius_candidates", forbid_model
    )
    original_hash = calculate_sha256(showcase.SHOWCASE_SOURCE)
    first = client.get("/showcase/evidence")
    second = client.get("/showcase/evidence")
    assert first.status_code == second.status_code == 200
    payload = first.json()
    assert payload == second.json()
    assert payload["product"] == "DataReady"
    assert payload["mode"] == "recorded_strategy_replay"
    assert payload["live_inference_performed"] is False
    assert payload["model"] == showcase.NVIDIA_MODEL
    assert payload["source_sha256"] == original_hash
    assert payload["source_preserved"] is True
    assert payload["release_authorized"] is False
    assert payload["production_changes_applied"] is False
    assert calculate_sha256(showcase.SHOWCASE_SOURCE) == original_hash


def test_demo_replays_the_observed_nvidia_competing_repair_outcome():
    response = client.get("/showcase/evidence")
    assert response.status_code == 200
    payload = response.json()
    assert payload["before"]["status"] == "QUARANTINED"
    assert payload["before"]["score"] == 80
    assert payload["before"]["rows"] == 4
    assert payload["before"]["duplicate_rows"] == 1
    assert payload["recommendation"]["status"] == "RECOMMENDED"
    assert payload["recommendation"]["candidate_id"] == "candidate_2"
    assert payload["after_recommended"]["score"] == 90
    assert payload["after_recommended"]["rows"] == 3
    assert payload["after_recommended"]["duplicate_rows"] == 0
    assert payload["after_recommended"]["status"] == "QUARANTINED"
    assert payload["after_recommended"]["issues"] == [
        {"code": "MISSING_VALUES", "count": 1}
    ]

    one, two = payload["candidates"]
    assert one["id"] == "candidate_1"
    assert one["actions"] == ["STANDARDIZE_MISSING_MARKERS"]
    assert one["evaluation"] == "NO_AUDIT_REGRESSION"
    assert one["disposition"] == "NO_MEASURED_IMPROVEMENT"
    assert one["after_score"] == 80
    assert two["id"] == "candidate_2"
    assert two["actions"] == ["REMOVE_EXACT_DUPLICATES"]
    assert two["policy"] == "APPROVED"
    assert two["evaluation"] == "NO_AUDIT_REGRESSION"
    assert two["disposition"] == "IMPROVING"


def test_public_showcase_does_not_expose_secrets_or_raw_rows(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "SENTINEL_NEBIUS_SECRET")
    monkeypatch.setenv("GOOGLE_API_KEY", "SENTINEL_GEMINI_SECRET")
    response = client.get("/showcase/evidence")
    assert response.status_code == 200
    assert "SENTINEL_NEBIUS_SECRET" not in response.text
    assert "SENTINEL_GEMINI_SECRET" not in response.text
    assert "A,tools" not in response.text
    assert response.json()["live_endpoint_authentication_required"] is True


def test_blocked_synthetic_source_is_rejected_without_calling_model(
    monkeypatch, tmp_path
):
    source = tmp_path / "tampered.csv"
    source.write_text("item,category\nA,tools,EXTRA\n", encoding="utf-8")
    monkeypatch.setattr(showcase, "SHOWCASE_SOURCE", source)
    monkeypatch.setattr(
        "app.services.nebius_candidates.propose_nebius_candidates", forbid_model
    )
    response = client.get("/showcase/evidence")
    assert response.status_code == 503
    assert "tampered.csv" not in response.text


def test_missing_source_returns_generic_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(showcase, "SHOWCASE_SOURCE", tmp_path / "missing.csv")
    response = client.get("/showcase/evidence")
    assert response.status_code == 503
    assert "missing.csv" not in response.text


def test_symlink_source_is_rejected(monkeypatch, tmp_path):
    link = tmp_path / "input.csv"
    link.symlink_to(showcase.SHOWCASE_SOURCE)
    monkeypatch.setattr(showcase, "SHOWCASE_SOURCE", link)
    response = client.get("/showcase/evidence")
    assert response.status_code == 503


def test_deterministic_evaluation_error_has_no_internal_details(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_DIAGNOSTIC_INFORMATION")

    monkeypatch.setattr(showcase, "recommend_candidate_batch", fail)
    response = client.get("/showcase/evidence")
    assert response.status_code == 503
    assert "PRIVATE_DIAGNOSTIC_INFORMATION" not in response.text


def test_replay_does_not_unlock_the_protected_live_nebius_api(monkeypatch):
    monkeypatch.delenv("DATAREADY_DEMO_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(web, "propose_nebius_candidates", forbid_model)
    public = client.get("/showcase/evidence")
    protected = client.post("/demo/nebius-candidates")
    assert public.status_code == 200
    assert protected.status_code == 503


def test_existing_health_and_gemini_route_remain_available():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert any(getattr(route, "path", None) == "/demo/repair" for route in web.app.routes)


def test_missing_html_page_is_not_served(monkeypatch, tmp_path):
    monkeypatch.setattr(showcase, "SHOWCASE_PAGE", tmp_path / "missing.html")
    response = client.get("/showcase")
    assert response.status_code == 503
