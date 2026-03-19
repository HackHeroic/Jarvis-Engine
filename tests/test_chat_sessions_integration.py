"""Integration tests for chat sessions flow."""

from app.services.chat_history import (
    generate_session_title,
    _truncate_messages,
)


def test_session_title_from_planning_request():
    title = generate_session_title("I want to plan my day with 3 tasks")
    assert "plan" in title.lower()
    assert len(title) <= 63


def test_session_title_from_short_message():
    title = generate_session_title("Hello")
    assert title == "Hello"


def test_session_title_sentence_boundary():
    title = generate_session_title("Plan my day. I have three tasks.")
    assert title == "Plan my day."


def test_truncate_preserves_roles():
    msgs = [
        {"role": "user", "content": "hello", "created_at": "t1"},
        {"role": "assistant", "content": "hi there", "created_at": "t2", "intent": "GREETING"},
    ]
    result = _truncate_messages(msgs, max_chars=500)
    assert len(result) == 2
    assert result[0] == {"role": "user", "content": "hello"}
    assert result[1] == {"role": "assistant", "content": "hi there"}


def test_truncate_long_content():
    msgs = [{"role": "user", "content": "x" * 1000, "created_at": "t1"}]
    result = _truncate_messages(msgs, max_chars=100)
    assert len(result[0]["content"]) == 103  # 100 + "..."
    assert result[0]["content"].endswith("...")


def test_truncate_empty():
    assert _truncate_messages([], max_chars=500) == []


def test_context_message_format():
    """Verify context messages are LLM-ready format."""
    msgs = [
        {"role": "user", "content": "plan my day", "created_at": "t1", "metadata": {}},
        {"role": "assistant", "content": "here's your schedule", "created_at": "t2", "metadata": {"schedule": {"big": "data"}}},
    ]
    result = _truncate_messages(msgs, max_chars=500)
    for msg in result:
        assert set(msg.keys()) == {"role", "content"}
        assert msg["role"] in ("user", "assistant")
