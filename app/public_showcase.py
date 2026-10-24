"""Public competition showcase: deterministic replay, no model-call endpoints.

Deploy this module as its own Cloud Run service. Deliberately DO NOT import
app.web, register /demo/repair or /demo/nebius-candidates, or attach API secrets.
The existing Gemini Cloud Run service stays independent and unchanged.
"""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.showcase import router as showcase_router

app = FastAPI(
    title="DataReady | Evidence Before Execution",
    description="Public, reproducible audit evidence for the synthetic showcase.",
    version="1.0.0",
)

app.include_router(showcase_router)


@app.get("/", include_in_schema=False)
def homepage() -> RedirectResponse:
    """Route the public service URL directly to the judge-facing showcase."""
    return RedirectResponse("/showcase", status_code=307)


@app.get("/health")
def health() -> dict[str, str]:
    """Cheap health check without triggering audit or inference."""
    return {"status": "healthy", "service": "dataready-showcase"}
