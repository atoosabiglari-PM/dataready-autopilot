"""Integration tests for trials plus regression checks; no model calls."""

from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic import ValidationError

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, ProposedRepair, RepairAction, RepairPlan
from app.services import candidate_evaluation as evaluation
from app.services import candidate_trials as trials


def setup_batch(tmp_path, specifications=None, text=None):
    source = tmp_path / "input.csv"
    source.write_text(
        text if text is not None
        else "item,category\n A ,tools\n A ,tools\nA,tools\nB,parts\n",
        encoding="utf-8",
    )
    fingerprint = sha256(source.read_bytes()).hexdigest()
    specifications = specifications if specifications is not None else [
        ("dedupe", RepairAction.REMOVE_EXACT_DUPLICATES, []),
        ("trim", RepairAction.TRIM_OUTER_WHITESPACE, ["item"]),
    ]
    batch = CandidateBatch(
        source_fingerprint_sha256=fingerprint,
        candidates=[RepairCandidate(
            candidate_id=identifier,
            repair_plan=RepairPlan(
                source_fingerprint_sha256=fingerprint,
                summary="Test proposal.",
                actions=[ProposedRepair(
                    action=action, columns=columns, justification="Test."
                )],
            ),
        ) for identifier, action, columns in specifications],
    )
    policy = DatasetPolicy(allowed_actions={action for _, action, _ in specifications})
    return source, batch, policy


def evaluate(source, batch, policy):
    return evaluation.evaluate_candidate_batch(source, batch, dataset_policy=policy)


def completed_trials(tmp_path):
    source, batch, policy = setup_batch(tmp_path)
    results = trials.run_candidate_trials(source, batch, dataset_policy=policy)
    return source, batch, policy, results


def inject_trials(monkeypatch, results):
    monkeypatch.setattr(evaluation, "run_candidate_trials", lambda *a, **k: results)


def test_real_trials_feed_regression_checks_and_preserve_source(tmp_path):
    source, batch, policy = setup_batch(tmp_path)
    original = source.read_bytes()
    results = evaluate(source, batch, policy)
    assert [r.status for r in results] == [
        "NO_AUDIT_REGRESSION", "REGRESSION_DETECTED"
    ]
    assert results[0].trial.audit_after.row_count == 3
    assert results[1].trial.audit_after.row_count == 4
    assert results[1].trial.comparison.quality_score_delta == 0
    assert "DUPLICATES_INCREASED" in {
        f.code for f in results[1].regression.findings
    }
    assert source.read_bytes() == original


def test_review_required_candidates_do_not_reach_checker(tmp_path, monkeypatch):
    source, batch, _ = setup_batch(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("An unexecuted candidate must not reach regression checks.")

    monkeypatch.setattr(evaluation, "assess_audit_regressions", forbidden)
    results = evaluation.evaluate_candidate_batch(source, batch)
    assert all(r.status == "REQUIRES_REVIEW" and r.regression is None for r in results)


def test_partial_permission_is_preserved(tmp_path):
    source, batch, _ = setup_batch(tmp_path)
    policy = DatasetPolicy(allowed_actions={RepairAction.REMOVE_EXACT_DUPLICATES})
    assert [r.status for r in evaluate(source, batch, policy)] == [
        "NO_AUDIT_REGRESSION", "REQUIRES_REVIEW"
    ]


def test_execution_failure_is_not_a_regression_pass(tmp_path):
    source, batch, policy = setup_batch(tmp_path, [
        ("invalid", RepairAction.TRIM_OUTER_WHITESPACE, ["absent"])
    ])
    result = evaluate(source, batch, policy)[0]
    assert result.status == "TRIAL_FAILED"
    assert result.regression is None


def test_unsupported_action_stays_unsupported(tmp_path):
    source, batch, policy = setup_batch(tmp_path, [("drop", RepairAction.DROP_ROWS, [])])
    assert evaluate(source, batch, policy)[0].status == "UNSUPPORTED"


@pytest.mark.parametrize("replacement", [
    "item,category\nchanged,tools\n", "item,category\nA,tools,extra\n"
])
def test_changed_or_blocked_source_is_denied(tmp_path, replacement):
    source, batch, policy = setup_batch(tmp_path)
    source.write_text(replacement, encoding="utf-8")
    assert all(r.status == "DENIED" for r in evaluate(source, batch, policy))


def test_blocked_output_stays_blocked(tmp_path, monkeypatch):
    source, batch, policy = setup_batch(tmp_path)

    def corrupt_output(context):
        context.output_path.write_bytes(b"\x00not CSV")
        return context.output_path

    monkeypatch.setattr(trials, "execute_repair_plan", corrupt_output)
    results = evaluate(source, batch, policy)
    assert all(r.status == "OUTPUT_BLOCKED" and r.regression is None for r in results)


@pytest.mark.parametrize("field", [
    "audit_after", "comparison", "output_fingerprint_sha256"
])
def test_missing_trial_evidence_cannot_pass(tmp_path, monkeypatch, field):
    source, batch, policy, results = completed_trials(tmp_path)
    inject_trials(monkeypatch, (replace(results[0], **{field: None}), results[1]))
    checked = evaluate(source, batch, policy)
    assert checked[0].status == "EVIDENCE_INVALID"
    assert checked[0].regression is None
    assert checked[1].status == "REGRESSION_DETECTED"


def test_inconsistent_comparison_cannot_pass(tmp_path, monkeypatch):
    source, batch, policy, results = completed_trials(tmp_path)
    wrong = results[0].comparison.model_copy(update={"quality_score_delta": 999})
    inject_trials(monkeypatch, (replace(results[0], comparison=wrong), results[1]))
    assert evaluate(source, batch, policy)[0].status == "EVIDENCE_INVALID"


def test_inconsistent_output_fingerprint_cannot_pass(tmp_path, monkeypatch):
    source, batch, policy, results = completed_trials(tmp_path)
    wrong = replace(results[0], output_fingerprint_sha256="f" * 64)
    inject_trials(monkeypatch, (wrong, results[1]))
    assert evaluate(source, batch, policy)[0].status == "EVIDENCE_INVALID"


def test_mismatched_policy_evidence_cannot_pass(tmp_path, monkeypatch):
    source, batch, policy, results = completed_trials(tmp_path)
    wrong_decision = results[0].assessment.policy_decision.model_copy(
        update={"can_execute": False}
    )
    wrong_assessment = replace(results[0].assessment, policy_decision=wrong_decision)
    inject_trials(monkeypatch, (replace(results[0], assessment=wrong_assessment), results[1]))
    assert evaluate(source, batch, policy)[0].status == "EVIDENCE_INVALID"


def test_unknown_trial_status_cannot_pass(tmp_path, monkeypatch):
    source, batch, policy, results = completed_trials(tmp_path)
    inject_trials(monkeypatch, (replace(results[0], status="UNKNOWN"), results[1]))
    assert evaluate(source, batch, policy)[0].status == "EVIDENCE_INVALID"


def test_integrity_failure_is_not_swallowed(tmp_path, monkeypatch):
    source, batch, policy = setup_batch(tmp_path)

    def broken(*args, **kwargs):
        raise trials.CandidateTrialError("Integrity check failed.")

    monkeypatch.setattr(evaluation, "run_candidate_trials", broken)
    with pytest.raises(trials.CandidateTrialError, match="Integrity check failed"):
        evaluate(source, batch, policy)


@pytest.mark.parametrize("change", ["missing", "reordered", "different-plan"])
def test_batch_result_mismatch_aborts(tmp_path, monkeypatch, change):
    source, batch, policy, results = completed_trials(tmp_path)
    if change == "missing":
        wrong = results[:1]
    elif change == "reordered":
        wrong = tuple(reversed(results))
    else:
        candidate = results[0].assessment.candidate.model_copy(deep=True)
        candidate.repair_plan.summary = "Not the requested plan."
        assessment = replace(results[0].assessment, candidate=candidate)
        wrong = (replace(results[0], assessment=assessment), results[1])
    inject_trials(monkeypatch, wrong)
    with pytest.raises(evaluation.CandidateEvaluationError):
        evaluate(source, batch, policy)


def test_empty_batch_returns_empty_evaluation(tmp_path):
    source, batch, policy = setup_batch(tmp_path, [])
    assert evaluate(source, batch, policy) == ()


def test_mutated_input_is_revalidated(tmp_path):
    source, batch, policy = setup_batch(tmp_path)
    batch.candidates[1].candidate_id = "dedupe"
    with pytest.raises(ValidationError):
        evaluate(source, batch, policy)


def test_no_regression_is_not_the_same_as_ready(tmp_path):
    source, batch, policy = setup_batch(
        tmp_path, [("dedupe", RepairAction.REMOVE_EXACT_DUPLICATES, [])],
        text="item,category\nA,\nA,\nB,tools\n",
    )
    result = evaluate(source, batch, policy)[0]
    assert result.status == "NO_AUDIT_REGRESSION"
    assert result.trial.audit_after.status == "QUARANTINED"


def test_evaluation_does_not_select_or_authorize_release(tmp_path):
    source, batch, policy = setup_batch(tmp_path)
    for result in evaluate(source, batch, policy):
        assert not hasattr(result, "winner")
        assert not hasattr(result, "release_authorized")
        assert not hasattr(result, "output_path")
