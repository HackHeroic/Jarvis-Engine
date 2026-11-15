"""The workspace the model composes for one task.

One PRIMARY-role call is told the task (title, criteria, length, deadline, goal), what
Jarvis remembers about the user, and the material found for the task, and decides what
would help: which blocks (questions, flashcards, an outline, steps, a checklist, key
points, a worked example, a note), what they say, and whether one web search is worth
making. Nothing here is a table of task kinds: the fixed recipes (``recipes``) are only
what the workspace falls back to when the model cannot answer.

The reply is a ``WorkspaceComposition`` parsed by the router; a block that carries
nothing is dropped, and a composition with no block left is no composition (None), so
the caller falls back. The model's words are shown; its claims about sources are not
trusted: a block says it is from the user's documents only when material was given and
the model found it relevant.
"""

import logging
from dataclasses import dataclass

from app.core.model_router import route_llm_call
from app.schemas.workspace import (
    MAX_COMPOSED_BLOCKS,
    QUIZ_MAX_QUESTIONS,
    QUIZ_MIN_QUESTIONS,
    Quiz,
    QuizQuestion,
    StudyAsset,
    WorkspaceBlock,
    WorkspaceComposition,
    WorkspaceSearch,
)
from app.services.analytical.workspace.materials import MaterialChunk, sources_phrase
from app.services.analytical.workspace.quiz import quiz_markdown
from app.services.analytical.workspace.web import (
    SearchWeb,
    found_links,
    is_problem_site,
    link_type,
)

logger = logging.getLogger(__name__)

# How much of the material, and of what Jarvis remembers, the prompt carries.
COMPOSER_CONTEXT_CHUNKS = 6
COMPOSER_CONTEXT_MAX_CHARS = 6000
COMPOSER_MEMORY_LINES = 8
# The most links one search surfaces: a few to practise on, else the best one or two.
MAX_PROBLEM_LINKS = 3
MAX_LINKS = 2

NOT_FROM_DOCUMENTS = "not from your documents"

_SYSTEM = (
    "You are Jarvis, preparing the workspace a person opens when they sit down to do one "
    "task. Decide what would actually help them do this task in the time they planned, "
    "and write it. Everything is specific to this task; nothing generic."
)

_QUESTION_TYPES = ("questions", "flashcards")
_SEARCH_WANTS = {
    "video": "one well-regarded video",
    "article": "one clear article or guide",
    "practice_problems": "two or three practice problems or exercises, and one tutorial",
    "documentation": "the official documentation or one reference page",
    "example": "one good worked example of the same thing",
}


@dataclass(frozen=True)
class ComposerTask:
    """What the composer is told about the task and the person doing it."""

    title: str
    completion_criteria: str = ""
    duration_minutes: int | None = None
    deadline_hint: str | None = None
    goal: str = ""
    learning_style: str = ""
    user_prompt: str | None = None
    known_about_user: tuple[str, ...] = ()


@dataclass(frozen=True)
class Composed:
    """A composed workspace: the kind and approach the model gave, its blocks as assets,
    whether the material was used, and the search it asked for (None: no search)."""

    kind: str
    approach: str
    assets: list[StudyAsset]
    material_used: bool
    search: WorkspaceSearch | None


def composer_prompt(task: ComposerTask, material: list[MaterialChunk]) -> str:
    """The request: the task, the person, the material, and what a workspace may hold."""
    lines = [f'Task: "{task.title}"']
    if task.completion_criteria:
        lines.append(f"Done when: {task.completion_criteria}")
    if task.duration_minutes:
        lines.append(f"Time planned: {task.duration_minutes} minutes")
    if task.deadline_hint:
        lines.append(f"Due: {task.deadline_hint}")
    if task.goal and task.goal != task.title:
        lines.append(f"Part of the goal: {task.goal}")
    if task.learning_style:
        lines.append(f"Learns best as a: {task.learning_style}")
    if task.user_prompt:
        lines.append(f"The user asked: {task.user_prompt}")
    if task.known_about_user:
        known = "\n".join(f"- {line}" for line in task.known_about_user[:COMPOSER_MEMORY_LINES])
        lines.append(f"What Jarvis remembers about this person:\n{known}")
    if material:
        context = "\n\n".join(
            f"[From {chunk.file_name}]\n{chunk.text}" for chunk in material[:COMPOSER_CONTEXT_CHUNKS]
        )[:COMPOSER_CONTEXT_MAX_CHARS]
        lines.append(
            "Material from the person's own documents, found for this task (set "
            "material_is_relevant to false, and do not use it, if it is not about the "
            f"task):\n{context}"
        )
    else:
        lines.append("There is no material from the person's documents for this task.")
    lines.append(
        "Compose the workspace: one to three blocks, the most useful first, sized to the "
        "time planned.\n"
        "- Choose the blocks this task calls for. Recalling or understanding a subject: "
        "questions or flashcards, perhaps key_points. Solving problems: questions that are "
        "problems to solve, easier to harder, perhaps a worked_example. Writing something: "
        "an outline, perhaps a checklist of what makes it done. Building something: steps, "
        "the smallest first. An errand: a short checklist of what to have ready. Mix them "
        "when the task needs it; these are examples, not rules.\n"
        "- Questions test the subject itself (define, apply, solve, compare), never what a "
        "document says or lists. Where the material explains a point, follow it; where it "
        "only names a topic (a syllabus), answer from standard knowledge of the topic. "
        f"Give {QUIZ_MIN_QUESTIONS} to {QUIZ_MAX_QUESTIONS} questions or cards, each with "
        "its answer.\n"
        "- Use what Jarvis remembers about the person when it bears on the task (their "
        "goal, how they work), without repeating it back.\n"
        "- approach: one or two sentences, to the person, on how to spend this session.\n"
        "- search: one web search only if a page on the web would really help with this "
        "task; null for an errand or when the blocks are enough. Ask for practice_problems "
        "on LeetCode or Codeforces only for programming practice."
    )
    return "\n\n".join(lines)


def _clean(text: str) -> str:
    return (text or "").strip()


def _quiz_of(block: WorkspaceBlock) -> Quiz | None:
    """The block as a quiz, when it has enough questions with answers."""
    questions = [
        QuizQuestion(question=_clean(i.text), answer=_clean(i.answer), hint=_clean(i.hint))
        for i in block.items
        if _clean(i.text) and _clean(i.answer)
    ][:QUIZ_MAX_QUESTIONS]
    if len(questions) < QUIZ_MIN_QUESTIONS:
        return None
    return Quiz(title=_clean(block.title) or "Questions", questions=questions)


def block_markdown(block: WorkspaceBlock) -> str:
    """The block as markdown, or "" when it carries nothing."""
    items = [i for i in block.items if _clean(i.text)]
    if block.type in ("worked_example", "note"):
        body = _clean(block.body)
        return body or "\n".join(f"- {_clean(i.text)}" for i in items)
    if not items:
        return _clean(block.body)
    if block.type == "checklist":
        return "\n".join(f"- [ ] {_clean(i.text)}" for i in items)
    if block.type == "steps":
        first, rest = items[0], items[1:]
        lines = [f"**First step:** {_clean(first.text)}"]
        if rest:
            lines += ["", "### Then", ""] + [f"- [ ] {_clean(i.text)}" for i in rest]
        return "\n".join(lines)
    numbered = block.type in ("outline", *_QUESTION_TYPES)
    lines = []
    for number, item in enumerate(items, start=1):
        lead = f"{number}." if numbered else "-"
        note = _clean(item.answer)
        lines.append(f"{lead} **{_clean(item.text)}**" + (f": {note}" if note else ""))
    return "\n".join(lines)


def _rationale(block: WorkspaceBlock, material: list[MaterialChunk], used: bool) -> str:
    """Why the block is here, and where it came from: the model's reason, then the
    documents it drew on, or that it is not from the user's documents."""
    why = _clean(block.why).rstrip(".")
    source = f"from {sources_phrase(material)}" if used and material else NOT_FROM_DOCUMENTS
    return (f"{why} · {source}" if why else source[0].upper() + source[1:])[:300]


def block_asset(
    block: WorkspaceBlock, material: list[MaterialChunk], used: bool
) -> StudyAsset | None:
    """The block as a workspace asset, or None when it carries nothing to show."""
    title = (_clean(block.title) or block.type.replace("_", " ").capitalize())[:200]
    rationale = _rationale(block, material, used)
    if block.type in _QUESTION_TYPES:
        quiz = _quiz_of(block)
        if quiz is not None:
            return StudyAsset(
                asset_type="generated_quiz",
                title=title,
                content_or_url=quiz_markdown(quiz),
                rationale=rationale,
                metadata={"quiz": quiz.model_dump(), "block": block.type},
            )
    markdown = block_markdown(block)
    if not markdown:
        return None
    return StudyAsset(
        asset_type="generated_content",
        title=title,
        content_or_url=markdown,
        rationale=rationale,
        metadata={"block": block.type},
    )


async def compose_workspace(
    task: ComposerTask, material: list[MaterialChunk]
) -> Composed | None:
    """The workspace the model composes for ``task``, or None: the model could not
    answer, or nothing it wrote could be shown (logged by type, never the reply)."""
    if not task.title.strip():
        return None
    try:
        composition = await route_llm_call(
            task="workspace_composition",
            prompt=composer_prompt(task, material),
            system_prompt=_SYSTEM,
            response_schema=WorkspaceComposition,
        )
    except Exception as exc:
        logger.warning("The workspace could not be composed (%s)", type(exc).__name__)
        return None
    if not isinstance(composition, WorkspaceComposition):
        logger.warning("The composition was not one (%s)", type(composition).__name__)
        return None
    used = bool(material) and composition.material_is_relevant
    assets = [
        asset
        for block in composition.blocks[:MAX_COMPOSED_BLOCKS]
        if (asset := block_asset(block, material, used)) is not None
    ]
    if not assets:
        logger.info("The composed workspace had nothing to show: falling back")
        return None
    search = composition.search
    if search is not None and not _clean(search.query):
        search = None
    return Composed(
        kind=composition.kind,
        approach=_clean(composition.approach)[:600],
        assets=assets,
        material_used=used,
        search=search,
    )


async def composed_links(
    search: WorkspaceSearch, task_title: str, *, programming: bool, search_web: SearchWeb
) -> list[StudyAsset]:
    """The links the composition's one search finds. LeetCode and Codeforces links are
    kept only for practice problems on a programming topic, whatever was found.

    ``WebSearchUnavailableError`` passes through: the caller says links are missing.
    """
    wanted = _SEARCH_WANTS[search.want]
    raw = await search_web(
        f'For the task "{task_title}", search the web for: {search.query}. Return {wanted}.'
        " Format each as: URL | Title.",
        "You are a tutor. Find real pages. Return only lines of: URL | Title",
    )
    problems = search.want == "practice_problems"
    most = MAX_PROBLEM_LINKS if problems else MAX_LINKS
    assets: list[StudyAsset] = []
    for link in found_links(raw):
        if is_problem_site(link.url) and not (problems and programming):
            continue
        assets.append(StudyAsset(
            asset_type=link_type(link.url),
            title=link.title,
            content_or_url=link.url,
            rationale=f"Found for: {search.query}"[:200],
        ))
        if len(assets) >= most:
            break
    return assets
