"""Shared test fixtures for Jarvis Engine tests."""

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_supabase():
    """Mock Supabase client that returns empty results by default."""
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table

    empty_result = MagicMock()
    empty_result.data = []

    for method in ["select", "insert", "update", "delete", "upsert"]:
        chained = getattr(table, method).return_value
        chained.execute.return_value = empty_result
        chained.eq.return_value = chained
        chained.neq.return_value = chained
        chained.gt.return_value = chained
        chained.lt.return_value = chained
        chained.is_.return_value = chained
        chained.order.return_value = chained
        chained.limit.return_value = chained

    return client


@pytest.fixture
def mock_llm_response():
    """Factory for mocking hybrid_route_query responses."""
    def _make(response_data):
        async def _mock_route(*args, **kwargs):
            schema = kwargs.get("response_schema")
            if schema and isinstance(response_data, dict):
                return schema.model_validate(response_data)
            return response_data
        return _mock_route
    return _make


@pytest.fixture
def sample_tasks():
    """Sample TaskChunk data for testing."""
    return [
        {
            "task_id": "goal1_t1",
            "title": "Study CNNs - convolution layers",
            "duration_minutes": 25,
            "difficulty_weight": 0.5,
            "dependencies": [],
            "completion_criteria": "Explain convolution operation",
            "deadline_hint": None,
        },
        {
            "task_id": "goal1_t2",
            "title": "Study backpropagation math",
            "duration_minutes": 25,
            "difficulty_weight": 0.7,
            "dependencies": ["goal1_t1"],
            "completion_criteria": "Derive backprop gradient",
            "deadline_hint": None,
        },
        {
            "task_id": "goal1_t3",
            "title": "Practice: implement basic neural network",
            "duration_minutes": 25,
            "difficulty_weight": 0.6,
            "dependencies": ["goal1_t2"],
            "completion_criteria": "Working forward pass in Python",
            "deadline_hint": None,
        },
    ]
