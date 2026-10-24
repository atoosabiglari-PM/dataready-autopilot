"""Cloud Run public entrypoint excludes all billable inference routes."""

from fastapi.testclient import TestClient

from app.public_showcase import app
from app import web
from app.tools.fingerprint import calculate_sha256
from app.showcase import SHOWCASE_SOURCE

client = TestClient(app)


def test_public_root_redirects_to_competition_showcase():
    result = client.get("/", follow_redirects=False)
    assert result.status_code == 307
    assert result.headers["location"] == "/showcase"


def test_public_showcase_runs_isolated_evidence():
    source_sha = calculate_sha256(SHOWCASE_SOURCE)
    html = client.get("/showcase")
    evidence = client.get("/showcase/evidence")
    assert html.status_code == 200
    assert evidence.status_code == 200
    assert "DataReady" in html.text
    assert evidence.json()["recommendation"]["candidate_id"] == "candidate_2"
    assert evidence.json()["live_inference_performed"] is False
    assert evidence.json()["production_changes_applied"] is False
    assert calculate_sha256(SHOWCASE_SOURCE) == source_sha


def test_public_cloud_run_entrypoint_has_no_billable_routes():
    # Verify actual HTTP dispatch, not FastAPI's internal router structure.
    assert client.get("/showcase").status_code == 200
    assert client.get("/showcase/evidence").status_code == 200
    assert client.post("/demo/repair").status_code == 404
    assert client.post("/demo/nebius-candidates").status_code == 404
    schema_response = client.get("/openapi.json")
    assert schema_response.status_code == 200
    exposed = schema_response.json()["paths"]
    assert "/demo/repair" not in exposed
    assert "/demo/nebius-candidates" not in exposed


def test_public_invalid_posts_never_reach_a_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Inference must be unreachable on public service.")

    monkeypatch.setattr(web, "propose_nebius_candidates", forbidden)
    assert client.post("/demo/nebius-candidates").status_code == 404
    assert client.post("/demo/repair").status_code == 404
    assert client.get("/showcase/evidence").status_code == 200


def test_public_health_needs_no_credentials():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "dataready-showcase",
    }


def test_main_app_still_has_old_and_new_routes():
    # Inspect the schema for protected routes without making model calls.
    main_client = TestClient(web.app)
    response = main_client.get("/openapi.json")
    assert response.status_code == 200
    exposed = response.json()["paths"]
    assert "/demo/repair" in exposed
    assert "/demo/nebius-candidates" in exposed
    assert main_client.get("/showcase").status_code == 200
