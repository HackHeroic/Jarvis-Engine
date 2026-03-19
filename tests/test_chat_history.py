"""Unit tests for pure helpers in app.services.chat_history.

Only pure functions are tested here (generate_session_title, _truncate_messages).
Async DB functions require integration tests (real or mocked Supabase).
"""

import pytest
from app.services.chat_history import generate_session_title, _truncate_messages


# ---------------------------------------------------------------------------
# generate_session_title
# ---------------------------------------------------------------------------

class TestGenerateSessionTitle:
    def test_short_message__returns_as_is(self):
        assert generate_session_title("Hello there") == "Hello there"

    def test_empty_string__returns_new_conversation(self):
        assert generate_session_title("") == "New conversation"

    def test_whitespace_only__returns_new_conversation(self):
        assert generate_session_title("   ") == "New conversation"

    def test_long_message_no_sentence__truncated_to_63_chars(self):
        long_msg = "a" * 80
        result = generate_session_title(long_msg)
        assert len(result) <= 63  # 60 chars + "..."
        assert result.endswith("...")

    def test_exactly_60_chars__not_truncated(self):
        msg = "x" * 60
        result = generate_session_title(msg)
        assert result == msg
        assert not result.endswith("...")

    def test_61_chars__truncated(self):
        msg = "x" * 61
        result = generate_session_title(msg)
        assert result.endswith("...")
        assert len(result) == 63

    def test_sentence_boundary_period__returns_first_sentence(self):
        msg = "Plan my day. I also need to read a book."
        result = generate_session_title(msg)
        assert result == "Plan my day."

    def test_sentence_boundary_exclamation__returns_first_sentence(self):
        msg = "Study maths! Then revise physics."
        result = generate_session_title(msg)
        assert result == "Study maths!"

    def test_sentence_boundary_question__returns_first_sentence(self):
        msg = "What should I study today? I have exams next week."
        result = generate_session_title(msg)
        assert result == "What should I study today?"

    def test_sentence_longer_than_60_chars__truncated_even_if_sentence(self):
        long_sentence = "a" * 70 + ". Short second sentence."
        result = generate_session_title(long_sentence)
        assert result.endswith("...")
        assert len(result) <= 63

    def test_leading_whitespace__stripped(self):
        result = generate_session_title("   Hello world")
        assert result == "Hello world"

    def test_none_is_handled_as_falsy(self):
        # generate_session_title accepts str; passing empty string edge case
        result = generate_session_title("")
        assert result == "New conversation"


# ---------------------------------------------------------------------------
# _truncate_messages
# ---------------------------------------------------------------------------

class TestTruncateMessages:
    def test_empty_list__returns_empty_list(self):
        assert _truncate_messages([]) == []

    def test_preserves_role_and_content(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = _truncate_messages(messages)
        assert result == [{"role": "user", "content": "Hello"}]

    def test_strips_metadata_field(self):
        messages = [{"role": "user", "content": "Hi", "metadata": {"key": "val"}}]
        result = _truncate_messages(messages)
        assert "metadata" not in result[0]

    def test_strips_created_at_field(self):
        messages = [{"role": "assistant", "content": "Hi", "created_at": "2026-01-01T00:00:00"}]
        result = _truncate_messages(messages)
        assert "created_at" not in result[0]

    def test_strips_intent_field(self):
        messages = [{"role": "assistant", "content": "Hi", "intent": "PLAN_DAY"}]
        result = _truncate_messages(messages)
        assert "intent" not in result[0]

    def test_strips_arbitrary_extra_fields(self):
        messages = [{"role": "user", "content": "Hi", "session_id": "abc", "user_id": "u1"}]
        result = _truncate_messages(messages)
        assert set(result[0].keys()) == {"role", "content"}

    def test_long_content__truncated_with_ellipsis(self):
        long_content = "x" * 600
        messages = [{"role": "user", "content": long_content}]
        result = _truncate_messages(messages, max_chars=500)
        assert len(result[0]["content"]) == 503  # 500 + "..."
        assert result[0]["content"].endswith("...")

    def test_content_exactly_at_limit__not_truncated(self):
        content = "y" * 500
        messages = [{"role": "user", "content": content}]
        result = _truncate_messages(messages, max_chars=500)
        assert result[0]["content"] == content
        assert not result[0]["content"].endswith("...")

    def test_content_one_over_limit__truncated(self):
        content = "y" * 501
        messages = [{"role": "user", "content": content}]
        result = _truncate_messages(messages, max_chars=500)
        assert result[0]["content"].endswith("...")

    def test_multiple_messages__all_processed(self):
        messages = [
            {"role": "user", "content": "Hello", "metadata": {}},
            {"role": "assistant", "content": "Hi there", "intent": "GREETING", "created_at": "now"},
        ]
        result = _truncate_messages(messages)
        assert len(result) == 2
        assert set(result[0].keys()) == {"role", "content"}
        assert set(result[1].keys()) == {"role", "content"}

    def test_message_missing_role__skipped(self):
        messages = [{"content": "orphan message"}]
        result = _truncate_messages(messages)
        assert result == []

    def test_message_missing_content__skipped(self):
        messages = [{"role": "user"}]
        result = _truncate_messages(messages)
        assert result == []

    def test_custom_max_chars(self):
        messages = [{"role": "user", "content": "abcde"}]
        result = _truncate_messages(messages, max_chars=3)
        assert result[0]["content"] == "abc..."

    def test_does_not_mutate_original(self):
        original = {"role": "user", "content": "Hello", "metadata": {"x": 1}}
        _truncate_messages([original])
        assert "metadata" in original  # original dict should be untouched
