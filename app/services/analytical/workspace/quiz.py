"""The workspace quiz: 3 to 5 questions drawn from the task's material, as data.

One PRIMARY-role call (``practice_generation``). The reply is parsed through ``Quiz``
(markdown fences stripped first): a reply that does not parse, or a quiz of the wrong
size, means no quiz, never the model's raw text. The model may also say the material is
not about the task (``{"assets": []}``), which is no quiz too. The asset carries the quiz
as data (``metadata.quiz``) and as markdown, for a page that only renders markdown.
"""

import logging

from pydantic import ValidationError

from app.core.model_router import route_llm_call, strip_fences
from app.schemas.workspace import (
    QUIZ_MAX_QUESTIONS,
    QUIZ_MIN_QUESTIONS,
    Quiz,
    QuizReply,
    StudyAsset,
)
from app.services.analytical.workspace.materials import MaterialChunk, sources_phrase

logger = logging.getLogger(__name__)

# How much of the material the quiz prompt carries.
QUIZ_CONTEXT_CHUNKS = 8
QUIZ_CONTEXT_MAX_CHARS = 8000

_SYSTEM = (
    "You are a tutor. Write practice questions from the material you are given. "
    "Return valid JSON only, with no text around it."
)


def quiz_prompt(material: list[MaterialChunk], task_title: str, user_prompt: str | None) -> str:
    """The quiz request: the task, the material with each chunk's document, and the shape."""
    context = "\n\n".join(
        f"[From {chunk.file_name}]\n{chunk.text}" for chunk in material[:QUIZ_CONTEXT_CHUNKS]
    )[:QUIZ_CONTEXT_MAX_CHARS]
    request = f"\nThe user asked: {user_prompt}" if user_prompt else ""
    return (
        f'Write a short practice quiz for the task "{task_title}", using only the material '
        f"below.{request}\n"
        f'If the material is not about "{task_title}", return {{"assets": []}} and nothing else.\n'
        f'Otherwise return {{"assets": [{{"title": "<what it covers>", "questions": '
        f'[{{"question": "...", "answer": "...", "hint": "..."}}]}}]}} with '
        f"{QUIZ_MIN_QUESTIONS} to {QUIZ_MAX_QUESTIONS} questions. Each answer is brief (one "
        'to three sentences) and comes from the material; a hint is a short nudge, or "".\n\n'
        f"Material:\n{context}"
    )


def parse_quiz_reply(raw: str) -> Quiz | None:
    """The quiz in a model reply, or None: when the model said the material is not about
    the task, or when the reply is not a valid quiz (logged by the exception's type).

    The reply is ``{"assets": [<quiz>]}`` as asked; a bare quiz object is accepted too.
    """
    clean = strip_fences(raw)
    try:
        reply = QuizReply.model_validate_json(clean)
    except ValidationError as wrapped:
        try:
            return Quiz.model_validate_json(clean)
        except ValidationError:
            logger.warning("The quiz reply was not a valid quiz (%s)", type(wrapped).__name__)
            return None
    if not reply.assets:
        logger.info("The model found the material is not about the task: no quiz")
        return None
    return reply.assets[0]


def quiz_markdown(quiz: Quiz) -> str:
    """The quiz as markdown: each question numbered, then its hint and its answer."""
    blocks = []
    for number, item in enumerate(quiz.questions, start=1):
        lines = [f"**{number}. {item.question}**"]
        if item.hint:
            lines.append(f"*Hint: {item.hint}*")
        lines.append(f"Answer: {item.answer}")
        blocks.append("\n\n".join(lines))
    return "\n\n".join(blocks)


def quiz_asset(quiz: Quiz, material: list[MaterialChunk]) -> StudyAsset:
    """The quiz as a workspace asset, named after the documents it was drawn from."""
    title = quiz.title if quiz.title.lower().startswith("quiz") else f"Quiz: {quiz.title}"
    return StudyAsset(
        asset_type="generated_quiz",
        title=title[:200],
        content_or_url=quiz_markdown(quiz),
        rationale=f"From {sources_phrase(material)}",
        metadata={"quiz": quiz.model_dump()},
    )


async def generate_quiz(
    material: list[MaterialChunk], task_title: str, user_prompt: str | None = None
) -> list[StudyAsset]:
    """A quiz asset drawn from ``material``, or [] (no material: no model call is made).

    Best effort: a model that fails, or a reply that is not a quiz, gives no quiz (logged
    by the exception's type, never the reply).
    """
    if not material or not task_title.strip():
        return []
    try:
        raw = await route_llm_call(
            task="practice_generation",
            prompt=quiz_prompt(material, task_title, user_prompt),
            system_prompt=_SYSTEM,
        )
    except Exception as exc:
        logger.warning("The practice quiz could not be generated (%s)", type(exc).__name__)
        return []
    if not isinstance(raw, str):
        logger.warning("The quiz reply was not text (%s)", type(raw).__name__)
        return []
    quiz = parse_quiz_reply(raw)
    return [quiz_asset(quiz, material)] if quiz is not None else []
