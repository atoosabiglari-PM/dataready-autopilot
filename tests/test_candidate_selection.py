"""Recommendation tests: real trial integration plus controlled audit fixtures."""

from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic import ValidationError

from app.core.candidates import CandidateBatch, RepairCandidate
from app.core.policy import DatasetPolicy, ProposedRepair, RepairAction, RepairPlan
from app.services import candidate_selection as selection
from app.services.candidate_evaluation import CandidateEvaluation
from app.services.candidate_policy import CandidatePolicyAssessment
from app.services.candidate_trials import CandidateTrialError, CandidateTrialResult
from app.services.comparison import build_readiness_comparison
from app.services.regression import assess_audit_regressions
from app.tools.audit import AuditIssue, AuditReport
from app.tools.preflight import PreflightReport

DEDUPE = RepairAction.REMOVE_EXACT_DUPLICATES
TRIM = RepairAction.TRIM_OUTER_WHITESPACE
SOURCE = "a" * 64
OUTPUT = "b" * 64


def proposal(identifier, fingerprint, actions):
    return RepairCandidate(candidate_id=identifier, repair_plan=RepairPlan(
        source_fingerprint_sha256=fingerprint, summary="Test proposal.",
        actions=[ProposedRepair(
            action=action, columns=columns, justification="Test."
        ) for action, columns in actions],
    ))


def real_setup(tmp_path, specifications=None, text=None):
    source = tmp_path / "input.csv"
    source.write_text(
        text or "item,category\n A ,tools\n A ,tools\nA,tools\nB,parts\n",
        encoding="utf-8",
    )
    fingerprint = sha256(source.read_bytes()).hexdigest()
    specs = specifications if specifications is not None else [
        ("dedupe", [(DEDUPE, [])]),
        ("trim", [(TRIM, ["item"])]),
    ]
    batch = CandidateBatch(source_fingerprint_sha256=fingerprint, candidates=[
        proposal(identifier, fingerprint, actions) for identifier, actions in specs
    ])
    policy = DatasetPolicy(allowed_actions={
        action for _, actions in specs for action, _ in actions
    })
    return source, batch, policy


def recommend(source, batch, policy):
    return selection.recommend_candidate_batch(
        source, batch, dataset_policy=policy
    )


def test_recommends_improving_dedupe_and_excludes_regressing_trim(tmp_path):
    source, batch, policy = real_setup(tmp_path)
    result = recommend(source, batch, policy)

    assert result.status == "RECOMMENDED"
    assert result.recommended_candidate_id == "dedupe"
    assert result.shortlisted_candidate_ids == ("dedupe",)
    assert [r.disposition for r in result.rows] == ["IMPROVING", "EXCLUDED"]
    assert result.rows[1].evaluation_status == "REGRESSION_DETECTED"


def test_no_permission_produces_no_recommendation(tmp_path):
    source, batch, _ = real_setup(tmp_path)
    result = selection.recommend_candidate_batch(source, batch)

    assert result.status == "NO_IMPROVING_CANDIDATE"
    assert all(r.evaluation_status == "REQUIRES_REVIEW" for r in result.rows)


@pytest.mark.parametrize("kind", ["failed", "unsupported", "source-changed"])
def test_nonpassing_candidates_cannot_win(tmp_path, kind):
    specs = [("test", [(TRIM, ["absent"])])]
    if kind == "unsupported":
        specs = [("test", [(RepairAction.DROP_ROWS, [])])]

    source, batch, policy = real_setup(tmp_path, specs)
    if kind == "source-changed":
        source.write_text("item,category\nC,other\n", encoding="utf-8")

    result = recommend(source, batch, policy)

    assert result.status == "NO_IMPROVING_CANDIDATE"
    assert result.recommended_candidate_id is None
    assert result.rows[0].disposition == "EXCLUDED"


def test_undetected_whitespace_change_is_not_claimed_as_audit_improvement(tmp_path):
    source, batch, policy = real_setup(
        tmp_path,
        [("trim", [(TRIM, ["item"])])],
        "item,category\n A ,tools\nB,parts\n",
    )
    result = recommend(source, batch, policy)

    assert result.rows[0].disposition == "NO_MEASURED_IMPROVEMENT"
    assert result.recommended_candidate_id is None


def test_empty_batch_abstains(tmp_path):
    source, batch, policy = real_setup(tmp_path, [])
    result = recommend(source, batch, policy)

    assert result.status == "NO_IMPROVING_CANDIDATE"
    assert result.rows == ()
    assert result.shortlisted_candidate_ids == ()


def test_ready_no_action_candidate_is_not_a_repair_improvement(tmp_path):
    source, batch, policy = real_setup(
        tmp_path, [("unchanged", [])],
        "item,category\nA,tools\nB,parts\n",
    )
    result = recommend(source, batch, policy)

    assert result.rows[0].disposition == "NO_MEASURED_IMPROVEMENT"


def test_tied_audit_results_require_human_choice(tmp_path):
    source, batch, policy = real_setup(tmp_path, [
        ("dedupe", [(DEDUPE, [])]),
        ("both", [(TRIM, ["item"]), (DEDUPE, [])]),
    ], "item,category\n A ,tools\n A ,tools\nB,parts\n")
    result = recommend(source, batch, policy)

    assert result.status == "HUMAN_CHOICE_REQUIRED"
    assert result.shortlisted_candidate_ids == ("both", "dedupe")
    assert result.recommended_candidate_id is None


def test_identical_repaired_bytes_choose_strictly_simpler_plan(tmp_path):
    """Real isolated trials, not a mocked tie: same bytes, fewer actions win."""
    missing = RepairAction.STANDARDIZE_MISSING_MARKERS
    source, batch, policy = real_setup(
        tmp_path,
        [
            ("combined", [(missing, ["category"]), (DEDUPE, [])]),
            ("dedupe", [(DEDUPE, [])]),
        ],
        "item,category\nA,tools\nA,tools\nB,\nC,parts\n",
    )
    before = source.read_bytes()
    result = recommend(source, batch, policy)
    assert result.status == "RECOMMENDED"
    assert result.recommended_candidate_id == "dedupe"
    assert result.shortlisted_candidate_ids == ("dedupe",)
    assert "identical repaired bytes" in result.message
    assert "simpler" in result.message
    assert [r.disposition for r in result.rows] == ["IMPROVING", "IMPROVING"]
    trials = [evaluation.trial for evaluation in result.evaluations]
    assert trials[0].output_fingerprint_sha256 == trials[1].output_fingerprint_sha256
    assert all(trial.audit_after.quality_score == 90 for trial in trials)
    assert all(trial.audit_after.duplicate_row_count == 0 for trial in trials)
    assert source.read_bytes() == before


def test_same_audit_score_but_different_repaired_bytes_still_requires_review(tmp_path):
    """Two verifiably different files must not be collapsed just for equal scores."""
    source, batch, policy = real_setup(
        tmp_path,
        [
            ("dedupe", [(DEDUPE, [])]),
            ("trim_and_dedupe", [(TRIM, ["item"]), (DEDUPE, [])]),
        ],
        "item,category\n A ,tools\n A ,tools\nB,parts\n",
    )
    result = recommend(source, batch, policy)
    assert result.status == "HUMAN_CHOICE_REQUIRED"
    assert result.recommended_candidate_id is None
    # Both outputs have no remaining audit findings, but their bytes differ.
    assert result.evaluations[0].trial.audit_after.quality_score == 100
    assert result.evaluations[1].trial.audit_after.quality_score == 100
    assert result.evaluations[0].trial.output_fingerprint_sha256 != (
        result.evaluations[1].trial.output_fingerprint_sha256
    )


def test_recommendation_can_still_have_unresolved_findings(tmp_path):
    source, batch, policy = real_setup(
        tmp_path, [("dedupe", [(DEDUPE, [])])],
        "item,category\nA,\nA,\nB,tools\n",
    )
    result = recommend(source, batch, policy)

    assert result.status == "RECOMMENDED"
    assert result.evaluations[0].trial.audit_after.status == "QUARANTINED"
    assert not hasattr(result, "release_authorized")
    assert not hasattr(result, "output_path")


def test_original_and_input_models_are_preserved(tmp_path):
    source, batch, policy = real_setup(tmp_path)
    original = source.read_bytes()
    batch_before, policy_before = batch.model_dump(), policy.model_dump()

    recommend(source, batch, policy)

    assert source.read_bytes() == original
    assert batch.model_dump() == batch_before
    assert policy.model_dump() == policy_before
    assert list(tmp_path.iterdir()) == [source]


def test_integrity_error_propagates(tmp_path, monkeypatch):
    source, batch, policy = real_setup(tmp_path)

    def fail(*args, **kwargs):
        raise CandidateTrialError("Integrity failure.")

    monkeypatch.setattr(selection, "evaluate_candidate_batch", fail)

    with pytest.raises(CandidateTrialError):
        recommend(source, batch, policy)


def test_mutated_input_is_revalidated(tmp_path):
    source, batch, policy = real_setup(tmp_path)
    batch.candidates[1].candidate_id = "dedupe"

    with pytest.raises(ValidationError):
        recommend(source, batch, policy)


# Unit scenarios use synthetic audits, not claimed business measurements.
def audit(counts, *, fingerprint=SOURCE, score=70, severity="WARNING"):
    issues = [AuditIssue(
        code="MISSING_VALUES", column=column, count=count,
        severity=severity, message="Synthetic finding.",
    ) for column, count in counts.items() if count]

    return AuditReport(
        status="QUARANTINED" if issues else "READY",
        file_name="fixture.csv",
        fingerprint_sha256=fingerprint,
        row_count=10,
        column_count=2,
        duplicate_row_count=0,
        quality_score=score,
        issues=issues,
        preflight=PreflightReport(
            status="ACCEPTED", file_name="fixture.csv", file_size_bytes=100
        ),
    )


def inject_evidence(monkeypatch, before, after_reports):
    from app.core.policy import validate_repair_plan

    candidates = [
        proposal(f"c{i}", SOURCE, [(TRIM, [f"col{i}"])])
        for i in range(len(after_reports))
    ]
    batch = CandidateBatch(
        source_fingerprint_sha256=SOURCE, candidates=candidates
    )
    policy = DatasetPolicy(allowed_actions={TRIM})
    results = []

    for candidate, after in zip(candidates, after_reports, strict=True):
        decision = validate_repair_plan(before, candidate.repair_plan, policy)
        trial = CandidateTrialResult(
            CandidatePolicyAssessment(candidate, decision),
            "REAUDITED",
            before,
            after,
            build_readiness_comparison(before, after),
            OUTPUT,
        )
        regression = assess_audit_regressions(
            before,
            after,
            candidate.repair_plan,
            expected_output_fingerprint_sha256=OUTPUT,
        )
        results.append(CandidateEvaluation(
            trial, regression.status, regression
        ))

    monkeypatch.setattr(
        selection, "evaluate_candidate_batch", lambda *a, **k: tuple(results)
    )
    return batch, policy, results


def test_score_increase_alone_is_not_improvement(monkeypatch):
    batch, policy, _ = inject_evidence(
        monkeypatch, audit({"a": 3}),
        [audit({"a": 3}, fingerprint=OUTPUT, score=90)],
    )

    assert recommend(
        "unused.csv", batch, policy
    ).status == "NO_IMPROVING_CANDIDATE"


def test_reduced_issue_count_with_same_score_is_improvement(monkeypatch):
    batch, policy, _ = inject_evidence(
        monkeypatch, audit({"a": 3}),
        [audit({"a": 1}, fingerprint=OUTPUT)],
    )

    assert recommend(
        "unused.csv", batch, policy
    ).recommended_candidate_id == "c0"


def test_clear_dominance_recommends_stronger_candidate(monkeypatch):
    batch, policy, _ = inject_evidence(
        monkeypatch, audit({"a": 3, "b": 3}), [
            audit({"a": 2, "b": 2}, fingerprint=OUTPUT),
            audit({"a": 1, "b": 1}, fingerprint=OUTPUT),
        ],
    )
    result = recommend("unused.csv", batch, policy)

    assert result.recommended_candidate_id == "c1"
    assert all(r.disposition == "IMPROVING" for r in result.rows)


def test_column_tradeoffs_are_not_hidden_by_total_counts(monkeypatch):
    batch, policy, _ = inject_evidence(
        monkeypatch, audit({"a": 4, "b": 4}), [
            audit({"a": 1, "b": 4}, fingerprint=OUTPUT, score=90),
            audit({"a": 4, "b": 2}, fingerprint=OUTPUT),
        ],
    )

    assert recommend(
        "unused.csv", batch, policy
    ).status == "HUMAN_CHOICE_REQUIRED"


def test_severity_and_count_tradeoff_requires_choice(monkeypatch):
    batch, policy, _ = inject_evidence(
        monkeypatch, audit({"a": 4}), [
            audit({"a": 4}, fingerprint=OUTPUT, severity="INFO"),
            audit({"a": 2}, fingerprint=OUTPUT),
        ],
    )

    assert recommend(
        "unused.csv", batch, policy
    ).status == "HUMAN_CHOICE_REQUIRED"


@pytest.mark.parametrize("status", [
    "DENIED",
    "REQUIRES_REVIEW",
    "UNSUPPORTED",
    "TRIAL_FAILED",
    "OUTPUT_BLOCKED",
    "EVIDENCE_INVALID",
    "REGRESSION_DETECTED",
])
def test_every_nonpassing_status_is_excluded(monkeypatch, status):
    batch, policy, results = inject_evidence(
        monkeypatch, audit({"a": 3}),
        [audit({"a": 1}, fingerprint=OUTPUT)],
    )
    results[0] = replace(results[0], status=status)
    result = recommend("unused.csv", batch, policy)

    assert result.rows[0].disposition == "EXCLUDED"
    assert result.recommended_candidate_id is None


def test_missing_assessment_does_not_pass(monkeypatch):
    batch, policy, results = inject_evidence(
        monkeypatch, audit({"a": 3}),
        [audit({"a": 1}, fingerprint=OUTPUT)],
    )
    results[0] = replace(results[0], regression=None)

    assert recommend(
        "unused.csv", batch, policy
    ).rows[0].evaluation_status == "EVIDENCE_INVALID"


def test_comparison_is_independent_of_candidate_iteration_order(monkeypatch):
    batch, policy, results = inject_evidence(
        monkeypatch, audit({"a": 3, "b": 3}), [
            audit({"a": 1, "b": 3}, fingerprint=OUTPUT),
            audit({"a": 3, "b": 1}, fingerprint=OUTPUT),
        ],
    )
    first = recommend("unused.csv", batch, policy)
    results.reverse()
    second = recommend("unused.csv", batch, policy)

    assert first.status == second.status == "HUMAN_CHOICE_REQUIRED"
    assert first.shortlisted_candidate_ids == second.shortlisted_candidate_ids == (
        "c0", "c1"
    )
