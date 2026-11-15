"""The generated scaffold of a writing, building or admin task: an outline or a checklist.

One PRIMARY-role call (``workspace_outline``) with ``response_schema=TaskOutline``, so
the reply is parsed through Pydantic by the provider. Each kind asks for its own shape
and is held to its bounds: an outline of sections and a short checklist for writing,
the smallest first step then the rest for building, 3 to 5 things to have ready for an
errand. A reply out of bounds, or one that is not an outline, gives no asset.
"""

import logging
from dataclasses import dataclass

from app.core.model_router import route_llm_call
from app.schemas.workspace import StudyAsset, TaskOutline
from app.services.analytical.task_kind import TaskKind
from app.services.analytical.workspace.materials import MaterialChunk

logger = logging.getLogger(__name__)

OUTLINE_CONTEXT_CHUNKS = 5
OUTLINE_CONTEXT_MAX_CHARS = 4000

_SYSTEM = (
    "You help a student start a piece of work. Answer with the structure asked for, "
    "about this task only. Keep every line short."
)


@dataclass(frozen=True)
class _Shape:
    """What one kind of task asks for, and the bounds its answer is held to."""

    instruction: str
    sections: tuple[int, int]  # (fewest, most)
    checklist: tuple[int, int]
    default_title: str
    rationale: str


_SHAPES: dict[TaskKind, _Shape] = {
    TaskKind.WRITE: _Shape(
        instruction=(
            "Outline this piece of writing: 3 to 7 section headings in order, each with one"
            " short line on what goes in it, and a checklist of 2 to 5 things to check the"
            " draft against before it is done."
        ),
        sections=(2, 8),
        checklist=(0, 5),
        default_title="Outline",
        rationale="An outline to write from",
    ),
    TaskKind.BUILD: _Shape(
        instruction=(
            "Give a step checklist for building it: the first item is the smallest first"
            " step, one that takes under ten minutes; then the remaining steps in order,"
            " 3 to 8 items in all. Leave sections empty."
        ),
        sections=(0, 0),
        checklist=(2, 8),
        default_title="Steps",
        rationale="Start with the smallest step",
    ),
    TaskKind.ADMIN: _Shape(
        instruction=(
            "List 3 to 5 things to have ready before doing it, most important first."
            " Leave sections empty."
        ),
        sections=(0, 0),
        checklist=(3, 5),
        default_title="What to have ready",
        rationale="What to have ready before you start",
    ),
}


def outline_prompt(
    kind: TaskKind,
    task_title: str,
    completion_criteria: str,
    material: list[MaterialChunk],
    user_prompt: str | None,
) -> str:
    """The request for ``kind``'s scaffold: the task, what done means, the material."""
    parts = [f'Task: "{task_title}"']
    if completion_criteria:
        parts.append(f"Done when: {completion_criteria}")
    if user_prompt:
        parts.append(f"The user asked: {user_prompt}")
    parts.append(_SHAPES[kind].instruction)
    if material:
        context = "\n\n".join(
            f"[From {chunk.file_name}]\n{chunk.text}"
            for chunk in material[:OUTLINE_CONTEXT_CHUNKS]
        )[:OUTLINE_CONTEXT_MAX_CHARS]
        parts.append(
            "Use the material below where it helps; ignore it if it is not about the task."
            f"\n\nMaterial:\n{context}"
        )
    return "\n".join(parts)


def _bounded(items: list, bounds: tuple[int, int]) -> list | None:
    fewest, most = bounds
    return items[:most] if len(items) >= fewest else None


def outline_markdown(kind: TaskKind, outline: TaskOutline) -> str | None:
    """The scaffold as markdown, or None when it is outside ``kind``'s bounds."""
    shape = _SHAPES[kind]
    sections = _bounded([s for s in outline.sections if s.heading.strip()], shape.sections)
    checklist = _bounded([c.strip() for c in outline.checklist if c.strip()], shape.checklist)
    if sections is None or checklist is None:
        return None
    if kind is TaskKind.WRITE:
        lines = ["### Outline", ""]
        lines += [
            f"{n}. **{s.heading.strip()}**" + (f": {s.note.strip()}" if s.note.strip() else "")
            for n, s in enumerate(sections, start=1)
        ]
        if checklist:
            lines += ["", "### Before it is done", ""] + [f"- [ ] {item}" for item in checklist]
        return "\n".join(lines)
    if kind is TaskKind.BUILD:
        first, rest = checklist[0], checklist[1:]
        return "\n".join(
            [f"**First step:** {first}", "", "### Then", ""] + [f"- [ ] {item}" for item in rest]
        )
    return "\n".join(f"- [ ] {item}" for item in checklist)


async def generate_outline(
    kind: TaskKind,
    task_title: str,
    completion_criteria: str = "",
    material: list[MaterialChunk] | None = None,
    user_prompt: str | None = None,
) -> list[StudyAsset]:
    """``kind``'s scaffold as a ``generated_content`` asset, or [].

    Only writing, building and admin tasks have one. Best effort: a model that fails or
    a reply out of bounds gives no asset (logged by the exception's type).
    """
    shape = _SHAPES.get(kind)
    if shape is None or not task_title.strip():
        return []
    try:
        outline = await route_llm_call(
            task="workspace_outline",
            prompt=outline_prompt(kind, task_title, completion_criteria, material or [], user_prompt),
            system_prompt=_SYSTEM,
            response_schema=TaskOutline,
        )
    except Exception as exc:
        logger.warning("The %s outline could not be generated (%s)", kind.value, type(exc).__name__)
        return []
    if not isinstance(outline, TaskOutline):
        logger.warning("The %s outline was not an outline (%s)", kind.value, type(outline).__name__)
        return []
    markdown = outline_markdown(kind, outline)
    if markdown is None:
        logger.info("The %s outline was outside its bounds: not shown", kind.value)
        return []
    return [
        StudyAsset(
            asset_type="generated_content",
            title=(outline.title.strip() or shape.default_title)[:200],
            content_or_url=markdown,
            rationale=shape.rationale,
        )
    ]
