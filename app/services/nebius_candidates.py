"""Nebius/NVIDIA candidate generation; models never approve or execute repairs."""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from app.core.candidates import CandidateBatch
from app.services.planner import (
    ENV_FILE,
    PlannerResponseError,
    _restore_column_names,
    prepare_planner_input,
)
from app.tools.audit import AuditReport

MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"


class NebiusCandidateError(RuntimeError):
    """Inference, authentication, or schema validation failed safely."""


def propose_nebius_candidates(report: AuditReport) -> CandidateBatch:
    """Produce untrusted repair proposals from sanitized audit evidence only.

    No source rows, real column names, authorizations, or execution capabilities
    are sent to or accepted from the model. The batch must still pass policy.
    """
    prepared = prepare_planner_input(report)
    load_dotenv(ENV_FILE, override=False)
    key = os.getenv("NEBIUS_API_KEY", "").strip()
    if not key:
        raise NebiusCandidateError("NEBIUS_API_KEY is not configured.")

    instruction = (
        "You propose conservative CSV repair strategies from sanitized audit evidence. "
        "Return ONLY one JSON object, no Markdown, keys exactly: "
        "source_fingerprint_sha256 and candidates. "
        "candidates is a list of 0 to 3 objects with candidate_id and repair_plan. "
        "Each repair_plan has source_fingerprint_sha256, summary, actions. "
        "Each action has action, justification, columns. "
        "Copy the supplied SHA-256 exactly at both levels. "
        "Use column aliases (column_001 etc.) only; never guess real names. "
        "Allowed proposed actions: REMOVE_EXACT_DUPLICATES (columns=[]), "
        "TRIM_OUTER_WHITESPACE, STANDARDIZE_MISSING_MARKERS (explicit aliases). "
        "Do not propose actions without direct evidence. "
        "Avoid duplicate action sequences and duplicate candidate IDs. "
        "If no safe action is justified, return candidates=[]. "
        "Do not include decisions, approvals, scores, runnable code, or raw data."
    )
    payload = json.dumps(prepared.evidence.model_dump(mode="json"), sort_keys=True)
    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=45.0, max_retries=0)

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": payload},
            ],
            temperature=0,
            max_tokens=2400,
        )
    except Exception as exc:
        raise NebiusCandidateError(f"Nebius inference failed ({type(exc).__name__}).") from None

    if not response.choices or response.choices[0].finish_reason == "length":
        raise NebiusCandidateError("Nebius returned no complete response.")
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise NebiusCandidateError("Nebius returned no candidate JSON.")

    try:
        proposed = CandidateBatch.model_validate_json(content.strip())
        if proposed.source_fingerprint_sha256 != prepared.evidence.source_fingerprint_sha256:
            raise NebiusCandidateError("Candidate batch fingerprint does not match the audit.")
        restored = {
            "source_fingerprint_sha256": proposed.source_fingerprint_sha256,
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "repair_plan": _restore_column_names(
                        candidate.repair_plan, prepared.alias_to_column
                    ).model_dump(mode="json"),
                }
                for candidate in proposed.candidates
            ],
        }
        return CandidateBatch.model_validate(restored)
    except (ValidationError, PlannerResponseError, ValueError) as exc:
        raise NebiusCandidateError(
            f"Nebius proposal rejected ({type(exc).__name__}); no repair executed."
        ) from None
