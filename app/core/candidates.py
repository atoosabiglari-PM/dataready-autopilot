"""Proposal-only contracts for DataReady's competing repair candidates.

Creating a candidate never authorizes, executes, ranks, or releases a repair.
Policy checks and independent evaluation remain separate steps.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.policy import ProposedRepair, RepairPlan

MAX_CANDIDATES = 5


def _validated_plan(value: object) -> RepairPlan:
    """Copy and validate the existing plan schema; reject unexpected fields."""
    if isinstance(value, RepairPlan):
        value = value.model_dump(mode="python")
    if not isinstance(value, dict):
        raise ValueError("repair_plan must be a RepairPlan or an object.")
    if set(value) - set(RepairPlan.model_fields):
        raise ValueError("Unexpected repair-plan fields are not allowed.")

    actions = value.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("Repair-plan actions must be a list.")

    checked_actions = []
    for action in actions:
        if isinstance(action, ProposedRepair):
            action = action.model_dump(mode="python")
        if not isinstance(action, dict):
            raise ValueError("Each repair action must be an object.")
        if set(action) - set(ProposedRepair.model_fields):
            raise ValueError("Unexpected repair-action fields are not allowed.")
        checked_actions.append(action)

    return RepairPlan.model_validate({**value, "actions": checked_actions})


def _action_signature(plan: RepairPlan) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compare ordered actions, ignoring labels, justifications and column order."""
    return tuple(
        (action.action.value, tuple(sorted(set(action.columns))))
        for action in plan.actions
    )


class RepairCandidate(BaseModel):
    """One untrusted proposal, not an approval or an evaluation result."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    candidate_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    repair_plan: RepairPlan

    @field_validator("repair_plan", mode="before")
    @classmethod
    def validate_plan(cls, value: object) -> RepairPlan:
        return _validated_plan(value)


class CandidateBatch(BaseModel):
    """Up to five proposals for one source; an empty batch means abstention.

    Validation does not read the source file or establish execution permission.
    The evaluator must revalidate inputs and check the actual source fingerprint.
    """

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    source_fingerprint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    candidates: list[RepairCandidate] = Field(
        default_factory=list,
        max_length=MAX_CANDIDATES,
    )

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        seen_ids: set[str] = set()
        seen_plans: set[tuple[tuple[str, tuple[str, ...]], ...]] = set()

        for candidate in self.candidates:
            if candidate.candidate_id in seen_ids:
                raise ValueError("Candidate IDs must be unique.")
            seen_ids.add(candidate.candidate_id)

            plan = candidate.repair_plan
            if plan.source_fingerprint_sha256 != self.source_fingerprint_sha256:
                raise ValueError("Every candidate must reference the same source fingerprint.")

            signature = _action_signature(plan)
            if signature in seen_plans:
                raise ValueError("Duplicate repair strategies are not allowed.")
            seen_plans.add(signature)

        return self
