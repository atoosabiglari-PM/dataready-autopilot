"""Tests for the DataReady Autopilot command-line interface."""

import asyncio
from pathlib import Path

import pytest

from app.cli import (
    SAFE_EXECUTABLE_ACTIONS,
    build_dataset_policy,
    build_parser,
    exit_code_for_result,
    run_cli,
)
from app.core.policy import RepairAction
from app.services.autopilot import AutopilotResult
from app.tools.audit import AuditReport
from app.tools.fingerprint import calculate_sha256
from app.tools.preflight import PreflightReport


def make_preflight(
    file_name: str,
) -> PreflightReport:
    """Create a representative accepted preflight report."""

    return PreflightReport(
        status="ACCEPTED",
        file_name=file_name,
        file_size_bytes=1024,
        encoding="utf-8",
        delimiter=",",
        column_count=2,
        data_row_count=2,
        risk_flags=[],
        messages=[],
    )


def make_audit(
    source: Path,
    *,
    status: str = "READY",
) -> AuditReport:
    """Create a representative audit result."""

    return AuditReport(
        status=status,
        file_name=source.name,
        fingerprint_sha256=calculate_sha256(source),
        row_count=2,
        column_count=2,
        duplicate_row_count=0,
        quality_score=100 if status == "READY" else 90,
        preflight=make_preflight(source.name),
        issues=[],
    )


def make_result(
    source: Path,
    *,
    status: str,
) -> AutopilotResult:
    """Create a minimal Autopilot result for CLI testing."""

    return AutopilotResult(
        status=status,
        audit_report=make_audit(
            source,
            status=("READY" if status == "READY" else "QUARANTINED"),
        ),
        repair_plan=None,
        policy_decision=None,
        output_path=None,
        post_repair_audit=None,
        readiness_comparison=None,
        lineage_evidence=None,
        machine_readable_report=None,
        report_path=None,
        message=f"Test result: {status}",
    )


def test_parser_accepts_source_output_and_safe_repair_flag() -> None:
    """The demo parser must accept the documented competition command."""

    parser = build_parser()

    args = parser.parse_args(
        [
            "demo.csv",
            "repaired.csv",
            "--allow-safe-repairs",
        ]
    )

    assert args.source == Path("demo.csv")
    assert args.output == Path("repaired.csv")
    assert args.allow_safe_repairs is True


def test_default_cli_policy_does_not_auto_authorize_repairs() -> None:
    """Without explicit operator consent, no demo auto-repair policy exists."""

    policy = build_dataset_policy(
        allow_safe_repairs=False,
    )

    assert policy is None


def test_safe_repair_flag_authorizes_only_implemented_low_risk_actions() -> None:
    """The demo flag must not authorize unsupported or high-risk repairs."""

    policy = build_dataset_policy(
        allow_safe_repairs=True,
    )

    assert policy is not None
    assert policy.policy_id == "demo-safe-auto-repairs"

    assert policy.allowed_actions == {
        RepairAction.TRIM_OUTER_WHITESPACE,
        RepairAction.REMOVE_EXACT_DUPLICATES,
        RepairAction.STANDARDIZE_MISSING_MARKERS,
    }

    assert policy.allowed_actions == SAFE_EXECUTABLE_ACTIONS

    assert RepairAction.REDACT_PII not in policy.allowed_actions
    assert RepairAction.DROP_ROWS not in policy.allowed_actions
    assert RepairAction.FILL_MISSING_VALUES not in policy.allowed_actions
    assert RepairAction.CONVERT_COLUMN_TYPE not in policy.allowed_actions
    assert RepairAction.RENAME_COLUMNS not in policy.allowed_actions


@pytest.mark.parametrize(
    ("status", "expected_exit_code"),
    [
        ("READY", 0),
        ("REPAIRED", 0),
        ("REQUIRES_REVIEW", 2),
        ("DENIED", 3),
        ("BLOCKED", 4),
    ],
)
def test_cli_exit_codes_reflect_governed_result(
    tmp_path: Path,
    status: str,
    expected_exit_code: int,
) -> None:
    """CLI exit codes must distinguish governed outcomes."""

    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\nAlice,active\nBob,inactive\n",
        encoding="utf-8",
    )

    result = make_result(
        source,
        status=status,
    )

    assert exit_code_for_result(result) == expected_exit_code


def test_run_cli_passes_no_policy_without_explicit_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI must not silently grant repair execution permission."""

    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\nAlice,active\nBob,inactive\n",
        encoding="utf-8",
    )

    output = tmp_path / "repaired.csv"

    captured_policy: object = object()

    async def fake_run_autopilot(
        source_file: str | Path,
        output_file: str | Path,
        *,
        dataset_policy: object = None,
    ) -> AutopilotResult:
        nonlocal captured_policy

        captured_policy = dataset_policy

        assert Path(source_file) == source
        assert Path(output_file) == output

        return make_result(
            source,
            status="READY",
        )

    monkeypatch.setattr(
        "app.cli.run_autopilot",
        fake_run_autopilot,
    )

    parser = build_parser()

    args = parser.parse_args(
        [
            str(source),
            str(output),
        ]
    )

    exit_code = asyncio.run(run_cli(args))

    assert exit_code == 0
    assert captured_policy is None


def test_run_cli_passes_explicit_safe_policy_when_flag_is_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit CLI consent must pass only the constrained safe policy."""

    source = tmp_path / "source.csv"
    source.write_text(
        "name,status\n Alice ,active\n Bob ,inactive\n",
        encoding="utf-8",
    )

    output = tmp_path / "repaired.csv"

    captured_policy: object = None

    async def fake_run_autopilot(
        source_file: str | Path,
        output_file: str | Path,
        *,
        dataset_policy: object = None,
    ) -> AutopilotResult:
        nonlocal captured_policy

        captured_policy = dataset_policy

        assert Path(source_file) == source
        assert Path(output_file) == output

        return make_result(
            source,
            status="REQUIRES_REVIEW",
        )

    monkeypatch.setattr(
        "app.cli.run_autopilot",
        fake_run_autopilot,
    )

    parser = build_parser()

    args = parser.parse_args(
        [
            str(source),
            str(output),
            "--allow-safe-repairs",
        ]
    )

    exit_code = asyncio.run(run_cli(args))

    assert exit_code == 2
    assert captured_policy is not None

    assert captured_policy.policy_id == "demo-safe-auto-repairs"

    assert captured_policy.allowed_actions == {
        RepairAction.TRIM_OUTER_WHITESPACE,
        RepairAction.REMOVE_EXACT_DUPLICATES,
        RepairAction.STANDARDIZE_MISSING_MARKERS,
    }
