"""Anti-guilt copy: the phrases Jarvis never says about something that did not happen.

Unfinished work is never a failing. A task that did not happen was set aside, and a
new slot can be found. The one list of blame phrases lives here, so the behaviour
suites (which check every reply) and the production code that vets model-written text
(a conversation summary that reaches the user) agree on what counts as blame.
"""

import re

BLAME_PHRASES: tuple[str, ...] = (
    "failed",
    "you didn't",
    "you did not",
    "you missed",
    "behind schedule",
    "falling behind",
    "lazy",
    "should have",
    "your fault",
)


def blame_phrases_in(text: str) -> list[str]:
    """Every blame phrase ``text`` contains, matched case-insensitively as whole phrases."""
    return [
        phrase
        for phrase in BLAME_PHRASES
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, flags=re.IGNORECASE)
    ]
