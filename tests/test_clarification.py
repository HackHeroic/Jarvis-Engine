"""Tests for clarification detection."""
from app.services.analytical.control_policy import _needs_clarification


def test_short_ambiguous_prompt_no_history():
    result = _needs_clarification(None, "do it", None)
    assert result is not None
    assert result.intent == "CLARIFICATION"
    assert result.clarification_options is not None
    assert len(result.clarification_options) > 0


def test_short_ambiguous_prompt_with_history():
    history = [{"role": "user", "content": "plan my day"}]
    result = _needs_clarification(None, "do it", history)
    assert result is None


def test_long_prompt_no_clarification():
    result = _needs_clarification(None, "I want to study for my math exam tomorrow", None)
    assert result is None


def test_valid_extraction_no_clarification():
    from app.schemas.context import BrainDumpExtraction
    extraction = BrainDumpExtraction(planning_goal="study math")
    result = _needs_clarification(extraction, "do it", None)
    assert result is None
