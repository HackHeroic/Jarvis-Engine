"""Tutor and voice modes pick the conversational system prompt; neither is on by default."""

import pytest

from app.modules.conversation import (
    JARVIS_CHAT_PROMPT,
    JARVIS_TUTOR_PROMPT,
    JARVIS_VOICE_RULES,
    chat_system_prompt,
)
from app.schemas.chat_stream import ChatStreamRequest
from app.services.chat_turn.context import initial_state


def test_chat_system_prompt__no_mode__the_plain_chat_prompt():
    assert chat_system_prompt() == JARVIS_CHAT_PROMPT


@pytest.mark.parametrize("tutor, base", [(False, JARVIS_CHAT_PROMPT), (True, JARVIS_TUTOR_PROMPT)])
def test_chat_system_prompt__voice_mode__spoken_rules_follow_the_base_prompt(tutor, base):
    spoken = chat_system_prompt(tutor_mode=tutor, voice_mode=True)

    assert spoken == base + JARVIS_VOICE_RULES  # the base stays a cacheable prefix
    assert "No markdown" in spoken and "read aloud" in spoken


def test_chat_request__modes__off_unless_asked_for():
    request = ChatStreamRequest(user_id="alice", user_prompt="hello")

    assert (request.tutor_mode, request.voice_mode) == (False, False)


def test_chat_request__voice_and_tutor__reach_the_turn_state():
    import inspect

    request = ChatStreamRequest(user_id="alice", user_prompt="hi", tutor_mode=True, voice_mode=True)
    # initial_state copies both flags from the request (its other arguments vary by turn).
    source = inspect.getsource(initial_state)

    assert '"tutor_mode": request.tutor_mode' in source and '"voice_mode": request.voice_mode' in source
    assert request.voice_mode and request.tutor_mode
