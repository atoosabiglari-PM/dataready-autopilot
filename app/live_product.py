"""Live DataReady customer application.

Uploads are real user-supplied tabular datasets; no fixed demo data is used.
Nemotron inference requires a separate demo credential. The model never sees
raw rows; policy, isolated trials, and re-audit remain independent controls.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import ValidationError

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, RepairAction, validate_repair_plan
from app.services.candidate_selection import recommend_candidate_batch
from app.services.executor import execute_repair_plan, prepare_execution_context
from app.services.nebius_candidates import (
    MODEL as NVIDIA_MODEL,
    NebiusCandidateError,
    propose_nebius_candidates,
)
from app.services.user_import import (
    CustomerUploadError,
    ImportDiagnostics,
    receive_customer_dataset,
)
from app.services.public_quota import (
    PublicQuotaExceeded,
    PublicQuotaUnavailable,
    reserve_public_inference,
)
from app.tools.audit import AuditReport, audit_csv
from app.tools.fingerprint import calculate_sha256
from app.tools.preflight import MAX_FILE_SIZE_BYTES

SESSION_COOKIE = "dataready_guest_session"
SIGNING_ENV = "DATAREADY_APPROVAL_SIGNING_KEY"
TOKEN_LIFETIME_SECONDS = 15 * 60
MAX_REQUEST_SIZE = MAX_FILE_SIZE_BYTES + (2 * 1024 * 1024)
_EPHEMERAL_SIGNING_KEY = secrets.token_bytes(32)
ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "static" / "live.html"

DEMO_POLICY = DatasetPolicy(
    policy_id="customer-live-demo-conservative",
    allowed_actions={
        RepairAction.REMOVE_EXACT_DUPLICATES,
        RepairAction.TRIM_OUTER_WHITESPACE,
        RepairAction.STANDARDIZE_MISSING_MARKERS,
    },
)

app = FastAPI(
    title="DataReady — Live Governed AI Data Readiness",
    description=(
        "Upload your dataset, run a deterministic audit, request live NVIDIA "
        "Nemotron repair candidates through Nebius, and inspect independent evidence."
    ),
    version="2.0.0-demo",
)


@app.middleware("http")
async def reject_oversized_request(request, call_next):
    if request.url.path.startswith("/api/"):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                return Response(status_code=400, content="Invalid request size.")
            if size > MAX_REQUEST_SIZE:
                return Response(status_code=413, content="Request exceeds upload limits.")
    return await call_next(request)


def _signing_key() -> bytes:
    value = os.getenv(SIGNING_ENV, "")
    if len(value) >= 32:
        return value.encode("utf-8")
    if os.getenv("K_SERVICE"):
        raise HTTPException(
            status_code=503,
            detail="The server's secure session signing key is not configured.",
        )
    return _EPHEMERAL_SIGNING_KEY


def _new_guest_session() -> str:
    """Issue a signed anonymous browser session, not a customer-entered code."""
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(
        _signing_key(), ("session:" + nonce).encode("ascii"), hashlib.sha256
    ).hexdigest()
    return nonce + "." + signature


def _verified_guest(cookie: str | None) -> str | None:
    if not cookie or len(cookie) > 150 or "." not in cookie:
        return None
    nonce, signature = cookie.rsplit(".", 1)
    if len(nonce) < 24 or len(signature) != 64:
        return None
    expected = hmac.new(
        _signing_key(), ("session:" + nonce).encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return nonce


def _require_guest(request: Request) -> str:
    guest = _verified_guest(request.cookies.get(SESSION_COOKIE))
    if guest is None:
        raise HTTPException(
            status_code=403,
            detail="Open DataReady in your browser before starting live NVIDIA analysis.",
        )
    return guest


def _encode_token(candidate: RepairCandidate, source_hash: str) -> str:
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "sha256": source_hash,
        "expires_at": int(time.time()) + TOKEN_LIFETIME_SECONDS,
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    text = base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")
    signature = hmac.new(_signing_key(), text.encode("ascii"), hashlib.sha256).hexdigest()
    return text + "." + signature


def _decode_token(token: str, source_hash: str) -> RepairCandidate:
    """Bind a recommendation to uploaded bytes, its expiry, and server signature."""
    if len(token) > 20000 or "." not in token:
        raise HTTPException(status_code=403, detail="Invalid or expired approval token.")
    encoded, claimed_signature = token.rsplit(".", 1)
    expected = hmac.new(
        _signing_key(), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(claimed_signature, expected):
        raise HTTPException(status_code=403, detail="Invalid or expired approval token.")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if payload["sha256"] != source_hash or int(payload["expires_at"]) < int(time.time()):
            raise ValueError("The approval is expired or linked to another dataset.")
        candidate = RepairCandidate.model_validate(payload["candidate"])
        if candidate.repair_plan.source_fingerprint_sha256 != source_hash:
            raise ValueError("The approval does not match the uploaded dataset.")
        return candidate
    except (KeyError, ValueError, TypeError, binascii.Error, UnicodeError, ValidationError):
        raise HTTPException(
            status_code=403, detail="Invalid or expired approval token."
        ) from None


def _audit_public(report: AuditReport) -> dict[str, object]:
    return {
        "status": report.status,
        "quality_score": report.quality_score,
        "row_count": report.row_count,
        "column_count": report.column_count,
        "duplicate_row_count": report.duplicate_row_count,
        "fingerprint_sha256": report.fingerprint_sha256,
        "risk_flags": report.preflight.risk_flags,
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "column": issue.column,
                "count": issue.count,
            }
            for issue in report.issues
        ],
    }


def _prepare_audit(source: Path) -> AuditReport:
    result = audit_csv(source)
    if not result.fingerprint_sha256 or result.fingerprint_sha256 != calculate_sha256(source):
        raise HTTPException(status_code=409, detail="Dataset integrity check failed.")
    return result


def _evaluate_uploaded_data(source: Path, audit_before: AuditReport) -> dict[str, object]:
    """Real Nebius inference and isolated trials for a permitted uploaded dataset."""
    source_hash = audit_before.fingerprint_sha256
    assert source_hash is not None

    try:
        batch = propose_nebius_candidates(audit_before)
    except NebiusCandidateError:
        raise HTTPException(
            status_code=502, detail="NVIDIA generation failed safely. Please try again."
        ) from None

    try:
        recommendation = recommend_candidate_batch(
            source, batch, dataset_policy=DEMO_POLICY
        )
    except Exception:
        raise HTTPException(
            status_code=409, detail="Candidate evaluation could not be verified."
        ) from None

    if calculate_sha256(source) != source_hash:
        raise HTTPException(status_code=409, detail="Source preservation check failed.")

    entries: list[dict[str, object]] = []
    for row, evaluation in zip(
        recommendation.rows, recommendation.evaluations, strict=True
    ):
        trial = evaluation.trial
        after = trial.audit_after
        entries.append({
            "candidate_id": row.candidate_id,
            "actions": [
                {"action": action.action.value, "columns": action.columns}
                for action in trial.assessment.candidate.repair_plan.actions
            ],
            "policy_status": trial.assessment.policy_decision.status,
            "evaluation_status": row.evaluation_status,
            "disposition": row.disposition,
            "reason": row.reason,
            "audit_after": _audit_public(after) if after is not None else None,
            "regressions": (
                [finding.code for finding in evaluation.regression.findings]
                if evaluation.regression is not None else []
            ),
        })

    review_token: str | None = None
    if recommendation.status == "RECOMMENDED" and recommendation.recommended_candidate_id:
        winner = next(
            c for c in batch.candidates
            if c.candidate_id == recommendation.recommended_candidate_id
        )
        review_token = _encode_token(winner, source_hash)

    return {
        "mode": "LIVE_NVIDIA",
        "model_invoked": True,
        "provider": "Nebius Token Factory",
        "model": NVIDIA_MODEL,
        "original_audit": _audit_public(audit_before),
        "candidate_count": len(batch.candidates),
        "candidates": entries,
        "recommendation": {
            "status": recommendation.status,
            "candidate_id": recommendation.recommended_candidate_id,
            "shortlist": list(recommendation.shortlisted_candidate_ids),
            "message": recommendation.message,
        },
        "approval_token": review_token,
        "source_preserved": True,
        "production_changes_applied": False,
        "original_rows_sent_to_ai": False,
    }


@app.get("/")
def homepage(request: Request) -> FileResponse:
    if not HTML.is_file():
        raise HTTPException(status_code=503, detail="DataReady interface is unavailable.")
    response = FileResponse(
        HTML,
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
    if _verified_guest(request.cookies.get(SESSION_COOKIE)) is None:
        response.set_cookie(
            key=SESSION_COOKIE,
            value=_new_guest_session(),
            max_age=86400,
            path="/",
            secure=bool(os.getenv("K_SERVICE")),
            httponly=True,
            samesite="lax",
        )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy", "service": "dataready-live"}


@app.post("/api/audit")
async def audit_upload(file: UploadFile = File(...)) -> dict[str, object]:
    """Any supported tabular dataset can be inspected without a model call."""
    try:
        with TemporaryDirectory(prefix="dataready-upload-") as temporary:
            import_diagnostics = ImportDiagnostics()
            source = await receive_customer_dataset(
                file, Path(temporary), diagnostics=import_diagnostics
            )
            before = await run_in_threadpool(_prepare_audit, source)
            return {
                "mode": "LIVE_AUDIT",
                "model_invoked": False,
                "original_audit": _audit_public(before),
                "import_warnings": import_diagnostics.warnings,
                "source_preserved": True,
                "production_changes_applied": False,
            }
    except CustomerUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/analyze")
async def analyze_upload(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, object]:
    """Live NVIDIA analysis with server-side quotas, not customer access codes."""
    try:
        with TemporaryDirectory(prefix="dataready-upload-") as temporary:
            import_diagnostics = ImportDiagnostics()
            source = await receive_customer_dataset(
                file, Path(temporary), diagnostics=import_diagnostics
            )
            before = await run_in_threadpool(_prepare_audit, source)

            if import_diagnostics.uncached_formulas:
                return {
                    "mode": "LIVE_AUDIT",
                    "model_invoked": False,
                    "original_audit": _audit_public(before),
                    "import_warnings": import_diagnostics.warnings,
                    "candidate_count": 0,
                    "candidates": [],
                    "recommendation": {
                        "status": "REQUIRES_REVIEW",
                        "candidate_id": None,
                        "shortlist": [],
                        "message": (
                            "This workbook has formula cells without saved results. "
                            "The available rows were audited, but NVIDIA analysis "
                            "and repaired export are disabled until the workbook "
                            "has been recalculated and saved."
                        ),
                    },
                    "approval_token": None,
                    "source_preserved": True,
                    "production_changes_applied": False,
                }

            if before.status != "QUARANTINED":
                return {
                    "mode": "LIVE_AUDIT",
                    "model_invoked": False,
                    "original_audit": _audit_public(before),
                    "import_warnings": import_diagnostics.warnings,
                    "candidate_count": 0,
                    "candidates": [],
                    "recommendation": {
                        "status": (
                            "ALREADY_READY" if before.status == "READY"
                            else "BLOCKED"
                        ),
                        "candidate_id": None,
                        "shortlist": [],
                        "message": (
                            "No repair is needed." if before.status == "READY"
                            else "Preflight rejected this dataset; AI was not called."
                        ),
                    },
                    "approval_token": None,
                    "source_preserved": True,
                    "production_changes_applied": False,
                }

            if any(issue.severity == "CRITICAL" for issue in before.issues):
                return {
                    "mode": "LIVE_AUDIT",
                    "model_invoked": False,
                    "original_audit": _audit_public(before),
                    "import_warnings": import_diagnostics.warnings,
                    "candidate_count": 0,
                    "candidates": [],
                    "recommendation": {
                        "status": "REQUIRES_REVIEW",
                        "candidate_id": None,
                        "shortlist": [],
                        "message": "Critical audit findings require human review before AI planning.",
                    },
                    "approval_token": None,
                    "source_preserved": True,
                    "production_changes_applied": False,
                }

            guest = _require_guest(request)
            try:
                await run_in_threadpool(reserve_public_inference, guest)
            except PublicQuotaExceeded as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from None
            except PublicQuotaUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None

            result = await run_in_threadpool(
                _evaluate_uploaded_data, source, before
            )
            result["import_warnings"] = import_diagnostics.warnings
            return result
    except CustomerUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _make_approved_csv(
    source: Path, candidate: RepairCandidate, original: AuditReport
) -> bytes:
    """Revalidate a signed recommendation; execute only a customer-approved copy."""
    source_hash = original.fingerprint_sha256
    assert source_hash is not None
    plan = candidate.repair_plan
    policy = validate_repair_plan(original, plan, DEMO_POLICY)
    if policy.status != "APPROVED" or not policy.can_execute:
        raise HTTPException(status_code=409, detail="Policy does not permit this export.")

    batch = CandidateBatch(
        source_fingerprint_sha256=source_hash, candidates=[candidate]
    )
    try:
        checked = recommend_candidate_batch(
            source, batch, dataset_policy=DEMO_POLICY
        )
        if checked.status != "RECOMMENDED" or checked.recommended_candidate_id != candidate.candidate_id:
            raise HTTPException(
                status_code=409, detail="This repair lacks verified improvement."
            )
        output = source.parent / "dataready-output.csv"
        context = prepare_execution_context(source, output, plan, policy)
        execute_repair_plan(context)
        verified = audit_csv(output)
        if verified.status == "BLOCKED" or verified.fingerprint_sha256 != calculate_sha256(output):
            raise HTTPException(status_code=409, detail="Repaired output failed verification.")
        if calculate_sha256(source) != source_hash:
            raise HTTPException(status_code=409, detail="Source preservation check failed.")
        return output.read_bytes()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=409, detail="Approved export failed verification."
        ) from None


@app.post("/api/export")
async def export_approved_copy(
    file: UploadFile = File(...),
    approval_token: str = Form(...),
    consent: str = Form(...),
) -> Response:
    """No customer codes; signed approval and fresh policy checks gate export."""
    if consent != "approve":
        raise HTTPException(
            status_code=403, detail="Explicit customer approval is required."
        )
    try:
        with TemporaryDirectory(prefix="dataready-approval-") as temporary:
            import_diagnostics = ImportDiagnostics()
            source = await receive_customer_dataset(
                file, Path(temporary), diagnostics=import_diagnostics
            )
            if import_diagnostics.uncached_formulas:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot export a repair while formula results are missing.",
                )
            report = await run_in_threadpool(_prepare_audit, source)
            source_hash = report.fingerprint_sha256
            assert source_hash is not None
            candidate = _decode_token(approval_token, source_hash)
            output = await run_in_threadpool(_make_approved_csv, source, candidate, report)
            return Response(
                content=output,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": 'attachment; filename="dataready-repaired.csv"',
                    "Cache-Control": "no-store, private",
                    "X-Content-Type-Options": "nosniff",
                },
            )
    except CustomerUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
