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
    routes = {getattr(route, "path", None) for route in app.routes}
    assert "/showcase" in routes
    assert "/showcase/evidence" in routes
    assert "/demo/repair" not in routes
    assert "/demo/nebius-candidates" not in routes
    assert "/openapi.json" in routes
    schema = client.get("/openapi.json").json()
    assert "/demo/repair" not in schema["paths"]
    assert "/demo/nebius-candidates" not in schema["paths"]
    assert "/showcase/evidence" in schema["paths"]


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
    routes = {getattr(route, "path", None) for route in web.app.routes}
    assert "/demo/repair" in routes
    assert "/demo/nebius-candidates" in routes
    assert "/showcase" in routes
