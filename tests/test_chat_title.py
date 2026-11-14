"""A conversation's title, shortened from its first message."""

from app.services.chat_history import generate_session_title


def test_title__long_first_sentence__cut_at_a_whole_word():
    title = generate_session_title(
        "I have a machine learning quiz on Thursday and a compiler design assignment due Saturday."
    )

    assert title == "I have a machine learning quiz on Thursday and a compiler..."
    assert len(title) <= 63


def test_title__short_first_sentence__kept_whole():
    assert generate_session_title("Plan my week. I have two exams.") == "Plan my week."
