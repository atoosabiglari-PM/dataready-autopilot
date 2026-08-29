"""Web entry point for DataReady Autopilot."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__


class HealthResponse(BaseModel):
    """Structured response returned by the health endpoint."""

    status: Literal["healthy"]
    service: str
    version: str


app = FastAPI(
    title="DataReady Autopilot",
    description="An autonomous, evidence-backed trust gate for AI data pipelines.",
    version=__version__,
)


@app.get("/", tags=["system"])
def read_root() -> dict[str, str]:
    """Return basic service information."""

    return {
        "name": "DataReady Autopilot",
        "message": "The governed data-readiness service is running.",
    }


@app.get("/health", response_model=HealthResponse, tags=["system"])
def read_health() -> HealthResponse:
    """Return machine-readable service health information."""

    return HealthResponse(
        status="healthy",
        service="dataready-autopilot",
        version=__version__,
    )
