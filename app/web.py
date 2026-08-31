"""Cloud Run web entrypoint for DataReady Autopilot."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException

from app.core.policy import DatasetPolicy, RepairAction
from app.services.autopilot import run_autopilot

app = FastAPI(
    title="DataReady Autopilot",
    description=("Governed data readiness before enterprise data enters AI workflows."),
    version="1.0.0",
)


@app.get("/")
def root() -> dict[str, object]:
    """Return basic service information."""

    return {
        "service": "DataReady Autopilot",
        "status": "running",
        "platform": "Google Cloud Run",
        "gemini_role": "constrained repair planner",
        "governance": ("Gemini reasons; deterministic policy authorizes and executes."),
        "endpoints": {
            "health": "/health",
            "live_governed_demo": "POST /demo/repair",
        },
    }


@app.get("/health")
def health() -> dict[str, str]:
    """Cloud Run health endpoint."""

    return {
        "status": "healthy",
    }


@app.post("/demo/repair")
async def demo_repair() -> dict[str, object]:
    """Run the governed Gemini repair workflow on the safe demo dataset."""

    project_root = Path(__file__).resolve().parents[1]
    source_path = project_root / "demo_data" / "01_safe_repair.csv"

    if not source_path.is_file():
        raise HTTPException(
            status_code=500,
            detail="The bundled safe-repair demo dataset is unavailable.",
        )

    dataset_policy = DatasetPolicy(
        policy_id="cloud-run-demo-safe-auto-repairs",
        allowed_actions={
            RepairAction.TRIM_OUTER_WHITESPACE,
            RepairAction.REMOVE_EXACT_DUPLICATES,
            RepairAction.STANDARDIZE_MISSING_MARKERS,
        },
    )

    try:
        with TemporaryDirectory(
            prefix="dataready-autopilot-",
        ) as temporary_directory:
            output_path = Path(temporary_directory) / "safe-output.csv"

            result = await run_autopilot(
                source_path,
                output_path,
                dataset_policy=dataset_policy,
            )

            repair_actions = []

            if result.repair_plan is not None:
                repair_actions = [
                    {
                        "action": action.action.value,
                        "columns": action.columns,
                        "justification": action.justification,
                    }
                    for action in result.repair_plan.actions
                ]

            response: dict[str, object] = {
                "service": "DataReady Autopilot",
                "platform": "Google Cloud Run",
                "status": result.status,
                "message": result.message,
                "source_file": result.audit_report.file_name,
                "source_fingerprint_sha256": (result.audit_report.fingerprint_sha256),
                "initial_readiness": result.audit_report.status,
                "initial_quality_score": (result.audit_report.quality_score),
                "gemini_repair_actions": repair_actions,
                "policy_status": (
                    result.policy_decision.status if result.policy_decision is not None else None
                ),
                "execution_authorized": (
                    result.policy_decision.can_execute
                    if result.policy_decision is not None
                    else False
                ),
            }

            if result.post_repair_audit is not None:
                response["post_repair_readiness"] = result.post_repair_audit.status
                response["post_repair_quality_score"] = result.post_repair_audit.quality_score

            if result.readiness_comparison is not None:
                response["readiness_comparison"] = result.readiness_comparison.model_dump(
                    mode="json",
                )

            if result.lineage_evidence is not None:
                response["lineage_evidence"] = result.lineage_evidence.model_dump(
                    mode="json",
                )

            if result.machine_readable_report is not None:
                response["evidence_report"] = result.machine_readable_report.model_dump(
                    mode="json",
                )

            return response

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(f"The governed DataReady workflow failed safely: {type(exc).__name__}: {exc}"),
        ) from exc
