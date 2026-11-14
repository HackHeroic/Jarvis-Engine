"""How every provider lays out a conversation: notes folded into the final user turn.

A ``system`` entry in the history (the turn's context note, a context summary) changes
from turn to turn. In the system prompt it would break the provider's prompt cache,
and Gemma's chat template rejects a system turn mid-conversation; so it goes to the
front of the final user turn instead. Claude, the local model and Gemini all render a
request through this one rule, so they see the same conversation, and the stable part
(system prompt, then the earlier turns) stays a byte-identical prefix from turn to turn.
"""


def fold_system_notes(history: list[dict] | None, prompt: str) -> tuple[list[dict], str]:
    """The prior turns to send, and the prompt with the history's notes in front of it.

    Only ``role`` and ``content`` are kept, and only user and assistant turns with
    content. Each ``system`` entry with text content becomes a paragraph before
    ``prompt``, in order. The conversation must open on a user turn, so a window that
    opens on an assistant reply drops that reply. ``history`` itself is not changed.
    """
    notes: list[str] = []
    turns: list[dict] = []
    for message in history or []:
        role, content = message.get("role"), message.get("content")
        if not content:
            continue
        if role == "system" and isinstance(content, str):
            notes.append(content)
        elif role in ("user", "assistant"):
            turns.append({"role": role, "content": content})
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)
    return turns, "\n\n".join([*notes, prompt])
