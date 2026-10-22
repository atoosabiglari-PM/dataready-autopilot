"""Cloud Run web entrypoint for DataReady Autopilot."""

from __future__ import annotations

import os
from hmac import compare_digest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, Header, HTTPException

from app.core.policy import DatasetPolicy, RepairAction
from app.services.autopilot import run_autopilot
from app.services.candidate_selection import recommend_candidate_batch
from app.services.nebius_candidates import MODEL as NEBIUS_MODEL
from app.services.nebius_candidates import NebiusCandidateError, propose_nebius_candidates
from app.tools.audit import audit_csv
from app.tools.fingerprint import calculate_sha256

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


DEMO_ACCESS_ENV = "DATAREADY_DEMO_ACCESS_TOKEN"
DEMO_HEADER = "X-DataReady-Demo-Token"


def _require_nebius_demo_access(provided_token: str | None) -> None:
    """Deny all paid inference when the separate demo credential is absent."""
    configured_token = os.getenv(DEMO_ACCESS_ENV, "")
    if len(configured_token) < 32:
        raise HTTPException(
            status_code=503, detail="NVIDIA demo access is not configured."
        )
    if not provided_token or not compare_digest(provided_token, configured_token):
        raise HTTPException(status_code=401, detail="Unauthorized demo request.")


@app.post("/demo/nebius-candidates")
def demo_nebius_candidates(
    demo_token: str | None = Header(default=None, alias=DEMO_HEADER),
) -> dict[str, object]:
    """NVIDIA model proposes; DataReady evaluates synthetic demo data only.

    A distinct access token is required BEFORE initiating the billable inference.
    This route does not accept uploads, release data, or authorize production writes.
    """
    _require_nebius_demo_access(demo_token)

    source_path = Path(__file__).resolve().parents[1] / "demo_data" / "01_safe_repair.csv"
    if not source_path.is_file():
        raise HTTPException(status_code=503, detail="Demo dataset is unavailable.")

    audit_before = audit_csv(source_path)
    if audit_before.status != "QUARANTINED" or not audit_before.fingerprint_sha256:
        raise HTTPException(
            status_code=422, detail="Demo dataset is not eligible for repair planning."
        )

    original_hash = calculate_sha256(source_path)
    if original_hash != audit_before.fingerprint_sha256:
        raise HTTPException(status_code=409, detail="Demo source integrity mismatch.")

    try:
        candidates = propose_nebius_candidates(audit_before)
    except NebiusCandidateError:
        raise HTTPException(
            status_code=502, detail="Nebius candidate generation failed safely."
        ) from None

    policy = DatasetPolicy(
        policy_id="nebius-synthetic-demo-permitted-actions",
        allowed_actions={
            RepairAction.REMOVE_EXACT_DUPLICATES,
            RepairAction.TRIM_OUTER_WHITESPACE,
            RepairAction.STANDARDIZE_MISSING_MARKERS,
        },
    )

    try:
        recommendation = recommend_candidate_batch(
            source_path, candidates, dataset_policy=policy
        )
    except Exception:
        raise HTTPException(
            status_code=409, detail="Independent candidate evaluation failed safely."
        ) from None

    if calculate_sha256(source_path) != original_hash:
        raise HTTPException(
            status_code=409, detail="Demo source integrity changed during evaluation."
        )

    candidate_results: list[dict[str, object]] = []
    for row, evaluation in zip(
        recommendation.rows, recommendation.evaluations, strict=True
    ):
        trial = evaluation.trial
        after = trial.audit_after
        candidate_results.append(
            {
                "candidate_id": row.candidate_id,
                "actions": [
                    {
                        "action": action.action.value,
                        "columns": action.columns,
                    }
                    for action in trial.assessment.candidate.repair_plan.actions
                ],
                "policy_status": trial.assessment.policy_decision.status,
                "evaluation_status": row.evaluation_status,
                "disposition": row.disposition,
                "reason": row.reason,
                "audit_after": (
                    {
                        "status": after.status,
                        "quality_score": after.quality_score,
                        "row_count": after.row_count,
                        "issues": [
                            {"code": issue.code, "severity": issue.severity,
                             "count": issue.count}
                            for issue in after.issues
                        ],
                    }
                    if after is not None else None
                ),
                "regressions": (
                    [finding.code for finding in evaluation.regression.findings]
                    if evaluation.regression is not None else []
                ),
            }
        )

    return {
        "service": "DataReady Autopilot",
        "provider": "Nebius Token Factory",
        "model": NEBIUS_MODEL,
        "source": "bundled_synthetic_demo",
        "source_fingerprint_sha256": original_hash,
        "audit_before": {
            "status": audit_before.status,
            "quality_score": audit_before.quality_score,
            "row_count": audit_before.row_count,
            "issues": [
                {"code": issue.code, "severity": issue.severity, "count": issue.count}
                for issue in audit_before.issues
            ],
        },
        "candidate_count": len(candidates.candidates),
        "candidate_results": candidate_results,
        "recommendation": {
            "status": recommendation.status,
            "recommended_candidate_id": recommendation.recommended_candidate_id,
            "shortlisted_candidate_ids": list(recommendation.shortlisted_candidate_ids),
            "message": recommendation.message,
        },
        "source_preserved": True,
        "release_authorized": False,
        "production_changes_applied": False,
    }
