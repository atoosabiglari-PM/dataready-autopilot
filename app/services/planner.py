"""Safe boundary between deterministic audit evidence and the Gemini planner."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from app.agents.dataready_planner.agent import root_agent
from app.core.policy import ProposedRepair, RepairPlan
from app.tools.audit import AuditReport

APP_NAME = "dataready_autopilot"
USER_ID = "local_dataready_user"
REQUEST_TIMEOUT_SECONDS = 120
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class PlannerInputError(ValueError):
    """Raised when an audit report is unsafe or incomplete for AI planning."""


class PlannerConfigurationError(RuntimeError):
    """Raised when Gemini Developer API configuration is unavailable."""


class PlannerResponseError(RuntimeError):
    """Raised when Gemini returns an unusable or unsafe repair proposal."""


class SanitizedAuditIssue(BaseModel):
    """Minimal deterministic issue evidence allowed to reach Gemini."""

    code: str = Field(min_length=1, max_length=100)
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    column_ref: str | None = None
    count: int = Field(ge=0)


class SanitizedAuditEvidence(BaseModel):
    """Audit evidence with dataset-derived names and values removed."""

    status: Literal["READY", "QUARANTINED"]
    source_fingerprint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    duplicate_row_count: int = Field(ge=0)
    quality_score: int = Field(ge=0, le=100)
    issues: list[SanitizedAuditIssue] = Field(
        default_factory=list,
        max_length=500,
    )


@dataclass(frozen=True, slots=True)
class PreparedPlannerInput:
    """Sanitized model evidence plus a local-only alias map."""

    evidence: SanitizedAuditEvidence
    alias_to_column: dict[str, str]


def prepare_planner_input(report: AuditReport) -> PreparedPlannerInput:
    """Minimize an audit report and replace real column names with aliases."""
    if report.status == "BLOCKED":
        raise PlannerInputError("BLOCKED audit reports must not be sent to Gemini.")

    if report.fingerprint_sha256 is None:
        raise PlannerInputError("Audit report is missing its source fingerprint.")

    real_columns = sorted({issue.column for issue in report.issues if issue.column is not None})
    alias_to_column = {
        f"column_{index:03d}": column for index, column in enumerate(real_columns, start=1)
    }
    column_to_alias = {column: alias for alias, column in alias_to_column.items()}

    sanitized_issues = [
        SanitizedAuditIssue(
            code=issue.code,
            severity=issue.severity,
            column_ref=(column_to_alias[issue.column] if issue.column is not None else None),
            count=issue.count,
        )
        for issue in report.issues
    ]

    evidence = SanitizedAuditEvidence(
        status=report.status,
        source_fingerprint_sha256=report.fingerprint_sha256,
        row_count=report.row_count,
        column_count=report.column_count,
        duplicate_row_count=report.duplicate_row_count,
        quality_score=report.quality_score,
        issues=sanitized_issues,
    )

    return PreparedPlannerInput(
        evidence=evidence,
        alias_to_column=alias_to_column,
    )


def build_planner_prompt(
    evidence: SanitizedAuditEvidence,
) -> str:
    """Serialize only the explicitly approved sanitized evidence."""
    payload = json.dumps(
        evidence.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "Analyze only this sanitized deterministic audit evidence. "
        "Column references are opaque aliases. Return a RepairPlan matching "
        "the required schema. Evidence JSON follows:\n"
        f"{payload}"
    )


def _load_gemini_configuration() -> None:
    """Load local secrets without logging or returning their values."""
    load_dotenv(
        dotenv_path=ENV_FILE,
        override=False,
    )

    developer_mode = os.getenv(
        "GOOGLE_GENAI_USE_VERTEXAI",
        "",
    ).upper()

    if developer_mode != "FALSE":
        raise PlannerConfigurationError(
            "GOOGLE_GENAI_USE_VERTEXAI must be FALSE for Developer API mode."
        )

    if not os.getenv("GOOGLE_API_KEY"):
        raise PlannerConfigurationError("GOOGLE_API_KEY is not configured.")


def _extract_final_text(
    event: object,
) -> str | None:
    """Extract text only from an ADK final-response event."""
    is_final_response = getattr(
        event,
        "is_final_response",
        None,
    )

    if not callable(is_final_response) or not is_final_response():
        return None

    content = getattr(
        event,
        "content",
        None,
    )
    parts = getattr(
        content,
        "parts",
        None,
    )

    if not parts:
        return None

    text_parts = [part.text for part in parts if getattr(part, "text", None)]

    return "".join(text_parts) or None


def _restore_column_names(
    plan: RepairPlan,
    alias_to_column: dict[str, str],
) -> RepairPlan:
    """Resolve model-visible aliases using the local-only deterministic map."""
    restored_actions: list[ProposedRepair] = []

    for action in plan.actions:
        restored_columns: list[str] = []

        for column_ref in action.columns:
            if column_ref not in alias_to_column:
                raise PlannerResponseError(f"Planner returned unknown column alias: {column_ref}")

            restored_columns.append(alias_to_column[column_ref])

        restored_actions.append(
            action.model_copy(
                update={
                    "columns": restored_columns,
                }
            )
        )

    return plan.model_copy(
        update={
            "actions": restored_actions,
        }
    )


async def propose_repair_plan(
    report: AuditReport,
) -> RepairPlan:
    """Request and validate one structured Gemini repair proposal."""
    prepared = prepare_planner_input(report)
    _load_gemini_configuration()

    session_service = InMemorySessionService()
    session_id = f"plan-{uuid4().hex}"

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )

    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=build_planner_prompt(prepared.evidence))],
    )

    final_text: str | None = None

    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            async for event in runner.run_async(
                user_id=USER_ID,
                session_id=session_id,
                new_message=message,
            ):
                candidate = _extract_final_text(event)

                if candidate is not None:
                    final_text = candidate

    except TimeoutError as error:
        raise PlannerResponseError("Gemini planning request timed out.") from error

    finally:
        await runner.close()

    if final_text is None:
        raise PlannerResponseError("Gemini did not return a final structured response.")

    try:
        plan = RepairPlan.model_validate_json(final_text)

    except ValidationError as error:
        raise PlannerResponseError(
            "Gemini response did not match the RepairPlan schema."
        ) from error

    if plan.source_fingerprint_sha256 != prepared.evidence.source_fingerprint_sha256:
        raise PlannerResponseError("Gemini response fingerprint does not match the audited source.")

    return _restore_column_names(
        plan,
        prepared.alias_to_column,
    )
