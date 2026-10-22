"""Public, model-call-free showcase for a reproducible DataReady trial.

The reference candidate action types were observed in a genuine Nebius/NVIDIA
test. This public demonstration reconstructs those strategies against a fixed
synthetic dataset and independently re-evaluates them on every request.
No inference request, production modification, approval, or release occurs.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, ProposedRepair, RepairAction, RepairPlan
from app.services.candidate_selection import recommend_candidate_batch
from app.services.nebius_candidates import MODEL as NVIDIA_MODEL
from app.tools.audit import audit_csv
from app.tools.fingerprint import calculate_sha256

router = APIRouter(tags=["DataReady Showcase"])
ROOT = Path(__file__).resolve().parents[1]
SHOWCASE_SOURCE = ROOT / "demo_data" / "02_multi_issue.csv"
SHOWCASE_PAGE = ROOT / "app" / "static" / "showcase.html"

DEMO_POLICY = DatasetPolicy(
    policy_id="synthetic-reference-demo",
    allowed_actions={
        RepairAction.REMOVE_EXACT_DUPLICATES,
        RepairAction.STANDARDIZE_MISSING_MARKERS,
    },
)


def _reference_candidates(source_sha256: str) -> CandidateBatch:
    """Reconstruct action types actually observed in the live NVIDIA trial.

    Candidate summaries/justifications are authored for this replay, NOT
    verbatim text from the original model response.
    """
    return CandidateBatch(
        source_fingerprint_sha256=source_sha256,
        candidates=[
            RepairCandidate(
                candidate_id="candidate_1",
                repair_plan=RepairPlan(
                    source_fingerprint_sha256=source_sha256,
                    summary="Normalize missing-value markers in category.",
                    actions=[
                        ProposedRepair(
                            action=RepairAction.STANDARDIZE_MISSING_MARKERS,
                            justification="The audit found a missing category value.",
                            columns=["category"],
                        )
                    ],
                ),
            ),
            RepairCandidate(
                candidate_id="candidate_2",
                repair_plan=RepairPlan(
                    source_fingerprint_sha256=source_sha256,
                    summary="Remove exact duplicate records.",
                    actions=[
                        ProposedRepair(
                            action=RepairAction.REMOVE_EXACT_DUPLICATES,
                            justification="The audit found one exact duplicate row.",
                            columns=[],
                        )
                    ],
                ),
            ),
        ],
    )


def build_showcase_evidence() -> dict[str, object]:
    """Fresh deterministic evaluation of a fixed synthetic reference scenario."""
    if SHOWCASE_SOURCE.is_symlink() or not SHOWCASE_SOURCE.is_file():
        raise ValueError("The synthetic reference dataset is unavailable.")

    original_sha256 = calculate_sha256(SHOWCASE_SOURCE)
    before = audit_csv(SHOWCASE_SOURCE)
    if before.status != "QUARANTINED" or before.fingerprint_sha256 != original_sha256:
        raise ValueError("The synthetic reference dataset is not ready for evaluation.")

    batch = _reference_candidates(original_sha256)
    recommendation = recommend_candidate_batch(
        SHOWCASE_SOURCE,
        batch,
        dataset_policy=DEMO_POLICY,
    )

    if calculate_sha256(SHOWCASE_SOURCE) != original_sha256:
        raise ValueError("The source dataset changed during the showcase.")

    rows: list[dict[str, object]] = []
    for row, evaluation in zip(
        recommendation.rows, recommendation.evaluations, strict=True
    ):
        trial = evaluation.trial
        after = trial.audit_after
        rows.append(
            {
                "id": row.candidate_id,
                "actions": [
                    action.action.value
                    for action in trial.assessment.candidate.repair_plan.actions
                ],
                "policy": trial.assessment.policy_decision.status,
                "evaluation": row.evaluation_status,
                "disposition": row.disposition,
                "reason": row.reason,
                "before_score": before.quality_score,
                "after_score": after.quality_score if after is not None else None,
                "before_rows": before.row_count,
                "after_rows": after.row_count if after is not None else None,
                "before_duplicates": before.duplicate_row_count,
                "after_duplicates": (
                    after.duplicate_row_count if after is not None else None
                ),
                "remaining_findings": (
                    [
                        {"code": issue.code, "count": issue.count}
                        for issue in after.issues
                    ]
                    if after is not None else None
                ),
                "regression_codes": (
                    [finding.code for finding in evaluation.regression.findings]
                    if evaluation.regression is not None else []
                ),
            }
        )

    recommended_after = next(
        (
            evaluation.trial.audit_after
            for evaluation in recommendation.evaluations
            if evaluation.trial.assessment.candidate.candidate_id
            == recommendation.recommended_candidate_id
        ),
        None,
    )

    return {
        "product": "DataReady",
        "scenario": "Synthetic inventory CSV with a blank category and exact duplicate",
        "mode": "recorded_strategy_replay",
        "model": NVIDIA_MODEL,
        "live_inference_performed": False,
        "provenance": (
            "A live NVIDIA Nemotron call through Nebius previously returned these "
            "two action types for this synthetic scenario. This public view "
            "reconstructs—not verbatim reproduces—the proposals, then runs "
            "fresh deterministic policy checks, isolated trials, re-audits, "
            "and evidence-based recommendation. No model request occurs here."
        ),
        "before": {
            "status": before.status,
            "score": before.quality_score,
            "rows": before.row_count,
            "duplicate_rows": before.duplicate_row_count,
            "issues": [
                {"code": issue.code, "count": issue.count}
                for issue in before.issues
            ],
        },
        "after_recommended": (
            {
                "status": recommended_after.status,
                "score": recommended_after.quality_score,
                "rows": recommended_after.row_count,
                "duplicate_rows": recommended_after.duplicate_row_count,
                "issues": [
                    {"code": issue.code, "count": issue.count}
                    for issue in recommended_after.issues
                ],
            }
            if recommended_after is not None else None
        ),
        "candidates": rows,
        "recommendation": {
            "status": recommendation.status,
            "candidate_id": recommendation.recommended_candidate_id,
            "shortlist": list(recommendation.shortlisted_candidate_ids),
        },
        "source_sha256": original_sha256,
        "source_preserved": True,
        "release_authorized": False,
        "production_changes_applied": False,
        "live_endpoint": "/demo/nebius-candidates",
        "live_endpoint_authentication_required": True,
    }


@router.get("/showcase")
def showcase_page() -> FileResponse:
    """Human-readable public demo; never triggers billable inference."""
    if not SHOWCASE_PAGE.is_file():
        raise HTTPException(status_code=503, detail="Showcase page unavailable.")
    return FileResponse(
        SHOWCASE_PAGE,
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/showcase/evidence")
def showcase_evidence() -> dict[str, object]:
    """Only synthetic, re-computed measurements, no model request or user data."""
    try:
        return build_showcase_evidence()
    except Exception:
        raise HTTPException(
            status_code=503, detail="Synthetic showcase evidence is unavailable."
        ) from None
