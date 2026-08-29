"""Tests for the DataReady Autopilot web endpoints."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_endpoint() -> None:
    """The root endpoint should identify the running service."""

    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "DataReady Autopilot",
        "message": "The governed data-readiness service is running.",
    }


def test_health_endpoint() -> None:
    """The health endpoint should return validated service status."""

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "dataready-autopilot",
        "version": "0.1.0",
    }
