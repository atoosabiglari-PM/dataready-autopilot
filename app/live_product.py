"""Live DataReady customer application.

Uploads are real user-supplied tabular datasets; no fixed demo data is used.
Nemotron inference is budgeted entirely on the server, without customer codes. The model never sees
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
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from app.core.audit_review import AuditReview, ReviewChoices
from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, RepairAction, validate_repair_plan
from app.services.candidate_selection import recommend_candidate_batch
from app.services.excel_layout import inspect_customer_workbook
from app.services.executor import execute_repair_plan, prepare_execution_context
from app.services.nebius_candidates import (
    MODEL as NVIDIA_MODEL,
)
from app.services.nebius_candidates import (
    NebiusCandidateError,
    propose_nebius_candidates,
)
from app.services.public_quota import (
    PublicQuotaExceeded,
    PublicQuotaUnavailable,
    reserve_public_inference,
)
from app.services.user_import import (
    CustomerUploadError,
    ImportDiagnostics,
    receive_customer_dataset,
)
from app.tools.audit import AuditReport, audit_csv, public_contact_kind
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


def _review_digest(review: AuditReview | None) -> str | None:
    if review is None:
        return None
    canonical = json.dumps(review.model_dump(mode="json"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def _policy_for(review: AuditReview | None) -> DatasetPolicy:
    return DEMO_POLICY.model_copy(update={"audit_review": review})


def _encode_token(
    candidate: RepairCandidate, source_hash: str, review: AuditReview | None = None,
) -> str:
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "sha256": source_hash,
        "expires_at": int(time.time()) + TOKEN_LIFETIME_SECONDS,
        "review_digest": _review_digest(review),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    text = base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")
    signature = hmac.new(_signing_key(), text.encode("ascii"), hashlib.sha256).hexdigest()
    return text + "." + signature


def _decode_token(
    token: str, source_hash: str, review: AuditReview | None = None,
) -> RepairCandidate:
    """Bind a recommendation to uploaded bytes, its expiry, and server signature."""
    if len(token) > 20000 or "." not in token:
        raise HTTPException(status_code=403, detail="Invalid or expired approval token.")
    try:
        encoded, claimed_signature = token.rsplit(".", 1)
        expected = hmac.new(
            _signing_key(), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(claimed_signature, expected):
            raise ValueError("Invalid signature.")
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if payload["sha256"] != source_hash or int(payload["expires_at"]) < int(time.time()):
            raise ValueError("The approval is expired or linked to another dataset.")
        if payload.get("review_digest") != _review_digest(review):
            raise ValueError("The review differs from the approved trial.")
        candidate = RepairCandidate.model_validate(payload["candidate"])
        if candidate.repair_plan.source_fingerprint_sha256 != source_hash:
            raise ValueError("The approval does not match the uploaded dataset.")
        return candidate
    except (KeyError, ValueError, TypeError, binascii.Error, UnicodeError, ValidationError):
        raise HTTPException(
            status_code=403, detail="Invalid or expired approval token."
        ) from None


def _review_binding(source: Path, info: ImportDiagnostics) -> dict[str, object]:
    incoming = next(source.parent.glob("original.*"))
    return {
        "sha256": calculate_sha256(source),
        "upload_sha256": calculate_sha256(incoming),
        "sheet_name": info.selected_sheet,
        "header_row": info.selected_header_row,
    }


def _encode_review(review: AuditReview, binding: dict[str, object]) -> str:
    payload = {
        "purpose": "audit-review", "binding": binding,
        "review": review.model_dump(mode="json"),
        "expires_at": int(time.time()) + TOKEN_LIFETIME_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(_signing_key(), ("review:" + encoded).encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _decode_review(token: str | None, binding: dict[str, object]) -> AuditReview | None:
    if token is None:
        return None
    try:
        if len(token) > 100000 or "." not in token:
            raise ValueError("Malformed review.")
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(
            _signing_key(), ("review:" + encoded).encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid review signature.")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (
            payload["purpose"] != "audit-review" or payload["binding"] != binding
            or int(payload["expires_at"]) < int(time.time())
        ):
            raise ValueError("Review is stale or belongs to a different file/table.")
        return AuditReview.model_validate(payload["review"])
    except (KeyError, ValueError, TypeError, binascii.Error, UnicodeError, ValidationError):
        raise HTTPException(status_code=403, detail="Review expired or file/table changed. Audit and review again.") from None


def _audit_public(report: AuditReport) -> dict[str, object]:
    return {
        "status": report.status,
        "quality_score": report.quality_score,
        "row_count": report.row_count,
        "column_count": report.column_count,
        "duplicate_row_count": report.duplicate_row_count,
        "fingerprint_sha256": report.fingerprint_sha256,
        "risk_flags": report.preflight.risk_flags,
        "reviewed_findings": [issue.model_dump() for issue in report.reviewed_findings],
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


def _review_details(report: AuditReport) -> dict[str, object]:
    findings = [*report.issues, *report.reviewed_findings]
    return {
        "status": "APPLIED" if report.review_context else "NOT_REVIEWED",
        "decisions": report.review_context.model_dump(mode="json") if report.review_context else None,
        "options": {
            "public_contact_columns": sorted({
                i.column for i in findings if i.column and public_contact_kind(i.column)
                and i.code in {"PII_COLUMN_NAME", "PII_VALUE_PATTERN"}
            }),
            "optional_columns": sorted({i.column for i in findings if i.column and i.code == "MISSING_VALUES"}),
        },
        "accepted_findings": [i.model_dump() for i in report.reviewed_findings],
        "blocking_findings": [i.model_dump() for i in report.issues if i.severity == "CRITICAL"],
        "source_fingerprint_sha256": report.fingerprint_sha256,
        "data_values_changed": False,
        "meaning": "Reviewer declarations supply business context; they do not repair values or certify correctness.",
    }


def _import_layout_public(info: ImportDiagnostics) -> dict[str, object] | None:
    """Return structural import metadata, never raw worksheet values."""
    if info.selected_sheet is None:
        return None
    return {
        "sheet": info.selected_sheet,
        "header_row": info.selected_header_row,
        "leading_note_rows_skipped": max(0, info.selected_header_row - 1),
        "automatically_detected": info.layout_auto_selected,
        "confidence": info.layout_confidence,
    }


def _prepare_audit(source: Path, review: AuditReview | None = None) -> AuditReport:
    try:
        result = audit_csv(source, review_context=review) if review else audit_csv(source)
    except ValueError:
        raise HTTPException(status_code=422, detail="Review choices do not match this file or contain unsupported contact patterns. Correct the file and audit again.") from None
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
            source, batch, dataset_policy=_policy_for(audit_before.review_context)
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
        review_token = _encode_token(winner, source_hash, audit_before.review_context)

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


@app.post("/api/workbook-layout")
async def workbook_layout(
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    """Provide a small redacted Excel header-row preview without NVIDIA calls."""
    try:
        with TemporaryDirectory(prefix="dataready-layout-") as temporary:
            result = await inspect_customer_workbook(file, Path(temporary))
            return JSONResponse(
                content=result,
                headers={"Cache-Control": "no-store, private"},
            )
    except CustomerUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/audit")
async def audit_upload(
    file: Annotated[UploadFile, File()],
    sheet_name: str | None = Form(None),
    header_row: int | None = Form(None),
    review_token: str | None = Form(None),
) -> dict[str, object]:
    """Any supported tabular dataset can be inspected without a model call."""
    try:
        with TemporaryDirectory(prefix="dataready-upload-") as temporary:
            import_diagnostics = ImportDiagnostics()
            source = await receive_customer_dataset(
                file, Path(temporary), diagnostics=import_diagnostics,
                sheet_name=sheet_name, header_row=header_row,
            )
            review = _decode_review(review_token, _review_binding(source, import_diagnostics))
            before = await run_in_threadpool(_prepare_audit, source, review)
            return {
                "mode": "LIVE_AUDIT",
                "model_invoked": False,
                "original_audit": _audit_public(before),
                "import_warnings": import_diagnostics.warnings,
                "import_layout": _import_layout_public(import_diagnostics),
                "source_preserved": True,
                "production_changes_applied": False,
                "review": _review_details(before),
                "review_token": review_token,
            }
    except CustomerUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/review")
async def review_upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    choices: str = Form(...),
    reviewer_reference: str = Form(...),
    review_reason: str = Form(...),
    source_fingerprint: str = Form(...),
    consent: str = Form(...),
    sheet_name: str | None = Form(None),
    header_row: int | None = Form(None),
) -> dict[str, object]:
    """Record explicit context, re-audit unchanged bytes, and sign the scope."""
    _require_guest(request)
    if consent != "confirm-review":
        raise HTTPException(status_code=403, detail="Confirm the review declarations before applying them.")
    try:
        if len(choices) > 50000:
            raise ValueError("Review too large.")
        selected = ReviewChoices.model_validate_json(choices)
        review = AuditReview(
            **selected.model_dump(), reviewer_reference=reviewer_reference.strip(),
            reason=review_reason.strip(), reviewed_at=int(time.time()),
        )
    except (ValueError, ValidationError):
        raise HTTPException(status_code=422, detail="Provide valid column choices, a reviewer reference, and a reason of 10–500 characters.") from None
    try:
        with TemporaryDirectory(prefix="dataready-review-") as temporary:
            info = ImportDiagnostics()
            source = await receive_customer_dataset(
                file, Path(temporary), diagnostics=info, sheet_name=sheet_name, header_row=header_row,
            )
            raw = await run_in_threadpool(_prepare_audit, source)
            if raw.fingerprint_sha256 != source_fingerprint:
                raise HTTPException(status_code=409, detail="File or table changed. Run a fresh audit before reviewing.")
            if raw.status == "BLOCKED" or info.uncached_formulas:
                raise HTTPException(status_code=422, detail="Correct structural issues or recalculate missing formula results, then upload and audit again.")
            available = _review_details(raw)["options"]
            if (
                not (selected.public_contact_columns or selected.optional_columns)
                or not set(selected.public_contact_columns) <= set(available["public_contact_columns"])
                or not set(selected.optional_columns) <= set(available["optional_columns"])
            ):
                raise HTTPException(status_code=422, detail="Select only review choices offered for this audit.")
            checked = await run_in_threadpool(_prepare_audit, source, review)
            blocked = any(i.severity == "CRITICAL" for i in checked.issues)
            token = _encode_review(review, _review_binding(source, info))
            return {
                "mode": "HUMAN_REVIEW", "model_invoked": False,
                "original_audit": _audit_public(checked),
                "unreviewed_audit": _audit_public(raw),
                "review": _review_details(checked), "review_token": token,
                "import_warnings": info.warnings, "import_layout": _import_layout_public(info),
                "candidate_count": 0, "candidates": [], "approval_token": None,
                "recommendation": {
                    "status": "REQUIRES_REVIEW" if blocked else "REVIEW_COMPLETE",
                    "candidate_id": None, "shortlist": [],
                    "message": (
                        "Review recorded. Critical findings remain; correct them in the source and upload it again. AI planning stays paused."
                        if blocked else
                        "Review complete. No unresolved audit findings under your declared context. No values changed and no AI repair was needed."
                        if checked.status == "READY" else
                        "Review recorded and file rechecked. Remaining findings are visible; you can now request live NVIDIA analysis."
                    ),
                },
                "source_preserved": True, "production_changes_applied": False,
                "original_rows_sent_to_ai": False,
            }
    except CustomerUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/analyze")
async def analyze_upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    sheet_name: str | None = Form(None),
    header_row: int | None = Form(None),
    review_token: str | None = Form(None),
) -> dict[str, object]:
    """Live NVIDIA analysis with server-side quotas, not customer access codes."""
    try:
        with TemporaryDirectory(prefix="dataready-upload-") as temporary:
            import_diagnostics = ImportDiagnostics()
            source = await receive_customer_dataset(
                file, Path(temporary), diagnostics=import_diagnostics,
                sheet_name=sheet_name, header_row=header_row,
            )
            review = _decode_review(review_token, _review_binding(source, import_diagnostics))
            before = await run_in_threadpool(_prepare_audit, source, review)
            review_fields = {"review": _review_details(before), "review_token": review_token}

            if import_diagnostics.uncached_formulas:
                return {
                    **review_fields,
                    "mode": "LIVE_AUDIT",
                    "model_invoked": False,
                    "original_audit": _audit_public(before),
                    "import_warnings": import_diagnostics.warnings,
                    "import_layout": _import_layout_public(import_diagnostics),
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
                    **review_fields,
                    "mode": "LIVE_AUDIT",
                    "model_invoked": False,
                    "original_audit": _audit_public(before),
                    "import_warnings": import_diagnostics.warnings,
                    "import_layout": _import_layout_public(import_diagnostics),
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
                    **review_fields,
                    "mode": "LIVE_AUDIT",
                    "model_invoked": False,
                    "original_audit": _audit_public(before),
                    "import_warnings": import_diagnostics.warnings,
                    "import_layout": _import_layout_public(import_diagnostics),
                    "candidate_count": 0,
                    "candidates": [],
                    "recommendation": {
                        "status": "REQUIRES_REVIEW",
                        "candidate_id": None,
                        "shortlist": [],
                        "message": "Critical audit findings require human review before AI planning. Use Review findings below, or correct the source and upload it again.",
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
            result["import_layout"] = _import_layout_public(import_diagnostics)
            result.update(review_fields)
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
    dataset_policy = _policy_for(original.review_context)
    policy = validate_repair_plan(original, plan, dataset_policy)
    if policy.status != "APPROVED" or not policy.can_execute:
        raise HTTPException(status_code=409, detail="Policy does not permit this export.")

    batch = CandidateBatch(
        source_fingerprint_sha256=source_hash, candidates=[candidate]
    )
    try:
        checked = recommend_candidate_batch(
            source, batch, dataset_policy=dataset_policy
        )
        if checked.status != "RECOMMENDED" or checked.recommended_candidate_id != candidate.candidate_id:
            raise HTTPException(
                status_code=409, detail="This repair lacks verified improvement."
            )
        output = source.parent / "dataready-output.csv"
        context = prepare_execution_context(source, output, plan, policy)
        execute_repair_plan(context)
        verified = _prepare_audit(output, original.review_context)
        trial_after = checked.evaluations[0].trial.audit_after
        if (
            verified.status == "BLOCKED" or trial_after is None
            or verified.fingerprint_sha256 != calculate_sha256(output)
            or verified.fingerprint_sha256 != trial_after.fingerprint_sha256
        ):
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
    file: Annotated[UploadFile, File()],
    approval_token: str = Form(...),
    consent: str = Form(...),
    sheet_name: str | None = Form(None),
    header_row: int | None = Form(None),
    review_token: str | None = Form(None),
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
                file, Path(temporary), diagnostics=import_diagnostics,
                sheet_name=sheet_name, header_row=header_row,
            )
            if import_diagnostics.uncached_formulas:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot export a repair while formula results are missing.",
                )
            review = _decode_review(review_token, _review_binding(source, import_diagnostics))
            report = await run_in_threadpool(_prepare_audit, source, review)
            source_hash = report.fingerprint_sha256
            assert source_hash is not None
            candidate = _decode_token(approval_token, source_hash, review)
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
