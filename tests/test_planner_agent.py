"""Tests for the constrained DataReady Gemini planner configuration."""

from app.agents.dataready_planner.agent import (
    MODEL_ID,
    PLANNER_INSTRUCTION,
    root_agent,
)
from app.core.policy import RepairPlan


def test_planner_uses_constrained_structured_configuration() -> None:
    """The planner must produce RepairPlan output without operational tools."""
    assert root_agent.name == "dataready_repair_planner"
    assert str(root_agent.model) == MODEL_ID
    assert root_agent.output_schema is RepairPlan
    assert root_agent.output_key == "repair_plan"
    assert root_agent.tools == []


def test_planner_instruction_preserves_safety_boundaries() -> None:
    """Essential governance rules must remain explicit in the prompt."""
    instruction = PLANNER_INSTRUCTION.lower()

    required_safety_phrases = (
        "untrusted data",
        "never follow instructions",
        "raw rows",
        "detected pii values",
        "human review is required",
        "deterministic policy engine",
        "original csv is immutable",
        "return only the structured repairplan",
    )

    for phrase in required_safety_phrases:
        assert phrase in instruction
