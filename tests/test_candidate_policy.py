"""Policy tests using synthetic evidence; no model calls or CSV execution."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import (
    DatasetPolicy,
    ProposedRepair,
    RepairAction,
    RepairPlan,
    validate_repair_plan,
)
from app.services.candidate_policy import assess_candidate_policies
from app.tools.audit import AuditIssue, AuditReport
from app.tools.preflight import PreflightReport

FINGERPRINT = "a" * 64


def make_report(status="QUARANTINED", fingerprint=FINGERPRINT, critical_code=None):
    issues = [] if status == "READY" else [
        AuditIssue(code="DUPLICATE_ROWS", severity="WARNING", message="Fixture.", count=1)
    ]
    if critical_code:
        issues.append(AuditIssue(
            code=critical_code, severity="CRITICAL", message="Review required.", count=1
        ))
    return AuditReport(
        status=status, file_name="fixture.csv", fingerprint_sha256=fingerprint,
        row_count=3, column_count=1, duplicate_row_count=0 if status == "READY" else 1,
        quality_score=100 if status == "READY" else 90,
        preflight=PreflightReport(
            status="BLOCKED" if status == "BLOCKED" else "ACCEPTED",
            file_name="fixture.csv", file_size_bytes=100,
        ),
        issues=issues,
    )


def make_candidate(candidate_id, action):
    actions = [] if action is None else [ProposedRepair(
        action=action, justification="Synthetic test proposal.",
        columns=[] if action == RepairAction.REMOVE_EXACT_DUPLICATES else ["item"],
    )]
    return RepairCandidate(candidate_id=candidate_id, repair_plan=RepairPlan(
        source_fingerprint_sha256=FINGERPRINT, summary="Proposed only.", actions=actions
    ))


def make_batch():
    return CandidateBatch(source_fingerprint_sha256=FINGERPRINT, candidates=[
        make_candidate("dedupe", RepairAction.REMOVE_EXACT_DUPLICATES),
        make_candidate("trim", RepairAction.TRIM_OUTER_WHITESPACE),
    ])


def allow_both():
    return DatasetPolicy(policy_id="test-explicit-policy", allowed_actions={
        RepairAction.REMOVE_EXACT_DUPLICATES, RepairAction.TRIM_OUTER_WHITESPACE,
    })


def test_no_explicit_permission_requires_review():
    results = assess_candidate_policies(make_report(), make_batch())
    assert all(r.policy_decision.status == "REQUIRES_REVIEW" for r in results)
    assert all(not r.policy_decision.can_execute for r in results)


def test_explicit_policy_approves_each_candidate():
    results = assess_candidate_policies(make_report(), make_batch(), dataset_policy=allow_both())
    assert [r.candidate.candidate_id for r in results] == ["dedupe", "trim"]
    assert all(r.policy_decision.status == "APPROVED" for r in results)
    assert all(r.policy_decision.can_execute for r in results)
    assert all(r.policy_decision.policy_id == "test-explicit-policy" for r in results)


def test_one_candidates_permission_does_not_approve_another():
    policy = DatasetPolicy(allowed_actions={RepairAction.REMOVE_EXACT_DUPLICATES})
    results = assess_candidate_policies(make_report(), make_batch(), dataset_policy=policy)
    assert [r.policy_decision.status for r in results] == ["APPROVED", "REQUIRES_REVIEW"]
    assert [r.policy_decision.can_execute for r in results] == [True, False]


@pytest.mark.parametrize("status", ["READY", "QUARANTINED", "BLOCKED"])
def test_preserves_existing_policy_decisions_exactly(status):
    report, batch, policy = make_report(status=status), make_batch(), allow_both()
    results = assess_candidate_policies(report, batch, dataset_policy=policy)
    for candidate, result in zip(batch.candidates, results, strict=True):
        assert result.policy_decision == validate_repair_plan(
            report, candidate.repair_plan, policy
        )
    if status == "BLOCKED":
        assert all(r.policy_decision.status == "DENIED" for r in results)
        assert all(not r.policy_decision.can_execute for r in results)


@pytest.mark.parametrize("fingerprint", [None, "b" * 64])
def test_missing_or_mismatched_audit_fingerprint_denies_all(fingerprint):
    results = assess_candidate_policies(
        make_report(fingerprint=fingerprint), make_batch(), dataset_policy=allow_both()
    )
    assert all(r.policy_decision.status == "DENIED" for r in results)
    assert all(not r.policy_decision.can_execute for r in results)


@pytest.mark.parametrize("code", [
    "AMBIGUOUS_COLUMN_NAMES", "PII_COLUMN_NAME", "PII_VALUE_PATTERN",
    "PROMPT_INJECTION_PATTERN", "OTHER_CRITICAL_FINDING",
])
def test_critical_findings_keep_all_candidates_in_review(code):
    results = assess_candidate_policies(
        make_report(critical_code=code), make_batch(), dataset_policy=allow_both()
    )
    assert all(r.policy_decision.status == "REQUIRES_REVIEW" for r in results)
    assert all(not r.policy_decision.can_execute for r in results)


@pytest.mark.parametrize("status,expected", [
    ("READY", "APPROVED"), ("QUARANTINED", "REQUIRES_REVIEW"),
])
def test_no_action_proposals_keep_existing_policy_behavior(status, expected):
    batch = CandidateBatch(source_fingerprint_sha256=FINGERPRINT, candidates=[
        make_candidate("no-change", None)
    ])
    result = assess_candidate_policies(make_report(status=status), batch)[0]
    assert result.policy_decision.status == expected


def test_policy_approval_does_not_claim_executor_support():
    # DROP_ROWS is known to policy but not implemented by the current executor.
    # Keep the original policy result; do not claim execution or verification.
    batch = CandidateBatch(source_fingerprint_sha256=FINGERPRINT, candidates=[
        make_candidate("drop", RepairAction.DROP_ROWS)
    ])
    policy = DatasetPolicy(allowed_actions={RepairAction.DROP_ROWS})
    result = assess_candidate_policies(make_report(), batch, dataset_policy=policy)[0]
    assert result.policy_decision.status == "APPROVED"
    assert not hasattr(result, "output_path")
    assert not hasattr(result, "verified")


def test_empty_batch_returns_no_assessments():
    batch = CandidateBatch(source_fingerprint_sha256=FINGERPRINT)
    assert assess_candidate_policies(make_report(), batch) == ()


def test_inputs_are_not_modified():
    report, batch, policy = make_report(), make_batch(), allow_both()
    before = [obj.model_dump(mode="python") for obj in (report, batch, policy)]
    assess_candidate_policies(report, batch, dataset_policy=policy)
    assert [obj.model_dump(mode="python") for obj in (report, batch, policy)] == before


def test_returned_candidate_is_detached_from_input():
    batch = make_batch()
    results = assess_candidate_policies(make_report(), batch, dataset_policy=allow_both())
    results[0].candidate.repair_plan.actions.clear()
    assert len(batch.candidates[0].repair_plan.actions) == 1
    assert len(results[1].candidate.repair_plan.actions) == 1


def test_mutated_plan_is_revalidated():
    batch = make_batch()
    batch.candidates[0].repair_plan.source_fingerprint_sha256 = "invalid"
    with pytest.raises(ValidationError):
        assess_candidate_policies(make_report(), batch)


def test_mutated_duplicate_candidate_ids_are_rejected():
    batch = make_batch()
    batch.candidates[1].candidate_id = "dedupe"
    with pytest.raises(ValidationError, match="IDs must be unique"):
        assess_candidate_policies(make_report(), batch)


def test_mutated_audit_is_revalidated():
    report = make_report()
    report.row_count = -1
    with pytest.raises(ValidationError):
        assess_candidate_policies(report, make_batch())


def test_mutated_policy_is_revalidated():
    policy = allow_both()
    policy.policy_id = ""
    with pytest.raises(ValidationError):
        assess_candidate_policies(make_report(), make_batch(), dataset_policy=policy)


def test_policy_assessment_does_not_open_dataset_files(monkeypatch):
    report, batch, policy = make_report(), make_batch(), allow_both()

    def forbid_open(*args, **kwargs):
        raise AssertionError("Policy assessment must not open files.")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbid_open)
        patch.setattr(Path, "open", forbid_open)
        results = assess_candidate_policies(report, batch, dataset_policy=policy)
    assert len(results) == 2
