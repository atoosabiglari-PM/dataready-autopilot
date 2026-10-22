"""NVIDIA provider boundary tests: zero network calls or key disclosure."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import nebius_candidates as provider
from app.services.planner import PlannerInputError
from app.tools.audit import audit_csv


def sample_report(tmp_path):
    source = tmp_path / "private_data.csv"
    source.write_text("item,private_flag\nA,\nA,\nB,ok\n", encoding="utf-8")
    report = audit_csv(source)
    assert report.status == "QUARANTINED"
    return report


def example_json(report):
    fingerprint = report.fingerprint_sha256

    def plan(identifier, action, columns):
        return {
            "candidate_id": identifier,
            "repair_plan": {
                "source_fingerprint_sha256": fingerprint,
                "summary": "Conservative proposal.",
                "actions": [{
                    "action": action,
                    "justification": "Evidence supports testing this action.",
                    "columns": columns,
                }],
            },
        }

    return {
        "source_fingerprint_sha256": fingerprint,
        "candidates": [
            plan("dedupe", "REMOVE_EXACT_DUPLICATES", []),
            plan("missing", "STANDARDIZE_MISSING_MARKERS", ["column_001"]),
        ],
    }


def stub_nebius(monkeypatch, content):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=content)
        )]
    )
    configuration = []

    def constructor(**kwargs):
        configuration.append(kwargs)
        return client

    monkeypatch.setattr(provider, "OpenAI", constructor)
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key-never-sent-to-network")
    return client, configuration


def test_returns_two_proposals_and_restores_aliases(tmp_path, monkeypatch):
    report = sample_report(tmp_path)
    api, config = stub_nebius(monkeypatch, json.dumps(example_json(report)))
    batch = provider.propose_nebius_candidates(report)
    assert len(batch.candidates) == 2
    assert batch.candidates[1].repair_plan.actions[0].columns == ["private_flag"]
    assert batch.source_fingerprint_sha256 == report.fingerprint_sha256
    assert not hasattr(batch, "can_execute")
    assert config[0]["base_url"] == provider.BASE_URL
    request = api.chat.completions.create.call_args.kwargs
    assert request["model"] == provider.MODEL
    transmitted = str(request["messages"])
    assert "private_data.csv" not in transmitted
    assert "private_flag" not in transmitted
    assert "column_001" in transmitted


def test_accepts_empty_batch_as_abstention(tmp_path, monkeypatch):
    report = sample_report(tmp_path)
    stub_nebius(monkeypatch, json.dumps({
        "source_fingerprint_sha256": report.fingerprint_sha256,
        "candidates": [],
    }))
    assert provider.propose_nebius_candidates(report).candidates == []


def test_blocked_source_is_never_sent_to_model(tmp_path, monkeypatch):
    source = tmp_path / "broken.csv"
    source.write_text("x,y\n1,2,3\n", encoding="utf-8")
    monkeypatch.setattr(provider, "OpenAI", lambda **k: pytest.fail("Model was called"))
    with pytest.raises(PlannerInputError):
        provider.propose_nebius_candidates(audit_csv(source))


def test_missing_key_fails_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setattr(provider, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(provider, "OpenAI", lambda **k: pytest.fail("Model was called"))
    with pytest.raises(provider.NebiusCandidateError, match="not configured"):
        provider.propose_nebius_candidates(sample_report(tmp_path))


def test_wrong_source_fingerprint_is_rejected(tmp_path, monkeypatch):
    report = sample_report(tmp_path)
    data = example_json(report)
    data["source_fingerprint_sha256"] = "b" * 64
    for candidate in data["candidates"]:
        candidate["repair_plan"]["source_fingerprint_sha256"] = "b" * 64
    stub_nebius(monkeypatch, json.dumps(data))
    with pytest.raises(provider.NebiusCandidateError, match="fingerprint"):
        provider.propose_nebius_candidates(report)


def test_unknown_column_alias_is_rejected(tmp_path, monkeypatch):
    report = sample_report(tmp_path)
    data = example_json(report)
    data["candidates"][1]["repair_plan"]["actions"][0]["columns"] = ["column_999"]
    stub_nebius(monkeypatch, json.dumps(data))
    with pytest.raises(provider.NebiusCandidateError, match="rejected"):
        provider.propose_nebius_candidates(report)


@pytest.mark.parametrize("change", [
    "invalid-json", "unknown-action", "authority-field", "duplicate-strategy",
])
def test_rejects_invalid_model_proposals(tmp_path, monkeypatch, change):
    report = sample_report(tmp_path)
    data = example_json(report)
    if change == "unknown-action":
        data["candidates"][0]["repair_plan"]["actions"][0]["action"] = "RUN_PYTHON"
    elif change == "authority-field":
        data["candidates"][0]["release_authorized"] = True
    elif change == "duplicate-strategy":
        data["candidates"][1]["repair_plan"]["actions"] = (
            data["candidates"][0]["repair_plan"]["actions"]
        )
    stub_nebius(
        monkeypatch,
        "not JSON" if change == "invalid-json" else json.dumps(data),
    )
    with pytest.raises(provider.NebiusCandidateError, match="rejected"):
        provider.propose_nebius_candidates(report)


def test_incomplete_output_is_rejected(tmp_path, monkeypatch):
    report = sample_report(tmp_path)
    api, _ = stub_nebius(monkeypatch, json.dumps(example_json(report)))
    api.chat.completions.create.return_value.choices[0].finish_reason = "length"
    with pytest.raises(provider.NebiusCandidateError, match="no complete response"):
        provider.propose_nebius_candidates(report)


def test_nebius_errors_do_not_echo_provider_response(tmp_path, monkeypatch):
    report = sample_report(tmp_path)
    api, _ = stub_nebius(monkeypatch, "")
    api.chat.completions.create.side_effect = RuntimeError("PRIVATE_RESPONSE_TEXT")
    with pytest.raises(provider.NebiusCandidateError) as caught:
        provider.propose_nebius_candidates(report)
    assert "PRIVATE_RESPONSE_TEXT" not in str(caught.value)
