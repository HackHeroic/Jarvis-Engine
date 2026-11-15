"""One recipe per kind of task: what its workspace holds (Strategy pattern).

| Kind     | From documents | Generated                          | Web                              |
|----------|----------------|------------------------------------|----------------------------------|
| PRACTICE | excerpts       | quiz (when there is material)      | problems (LeetCode/Codeforces    |
|          |                |                                    | only for programming) + tutorial |
| STUDY    | excerpts       | quiz (when there is material)      | one learning-style resource      |
| WRITE    | excerpts       | outline + short checklist          | one guide or good example        |
| BUILD    | excerpts       | smallest first step, then the rest | one reference or docs page       |
| ADMIN    | none           | 3 to 5 things to have ready        | none                             |

Every recipe makes at most one PRIMARY-role model call and at most one web search; the
two may run together (a web search is not a model call). The excerpts are the task's
material, which is already only material about the task (``materials``).
"""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Protocol

from app.schemas.workspace import StudyAsset
from app.services.analytical.task_kind import TaskKind, is_programming_topic
from app.services.analytical.workspace.materials import MaterialChunk, excerpt_assets
from app.services.analytical.workspace.outline import generate_outline
from app.services.analytical.workspace.quiz import generate_quiz
from app.services.analytical.workspace.web import (
    SearchWeb,
    StyleSearch,
    WebSearchUnavailableError,
    found_links,
    is_problem_site,
    link_type,
)

# The most links a practice search surfaces: two to practise on and one tutorial.
MAX_PRACTICE_LINKS = 3


@dataclass
class RecipeContext:
    """What a recipe builds from: the task, its material and the searches it may make.

    ``web_unavailable`` turns true when a web search behind the recipe could not run, so
    the workspace can say its links are missing.
    """

    task_title: str
    topic_keywords: list[str]
    goal_id: str | None
    completion_criteria: str
    learning_style: str
    user_prompt: str | None
    material: list[MaterialChunk]
    search_web: SearchWeb
    style_search: StyleSearch
    web_unavailable: bool = field(default=False)

    async def links(self, find: Awaitable[list[StudyAsset]]) -> list[StudyAsset]:
        """The links ``find`` surfaces, or none (noted) when its web search could not run."""
        try:
            return await find
        except WebSearchUnavailableError:
            self.web_unavailable = True
            return []

    def asked(self) -> str:
        return f" The user asked: {self.user_prompt}." if self.user_prompt else ""


class WorkspaceRecipe(Protocol):
    """What one kind of task's workspace holds."""

    kind: TaskKind
    reads_documents: bool

    async def assets(self, ctx: RecipeContext) -> list[StudyAsset]: ...


class StudyRecipe:
    kind = TaskKind.STUDY
    reads_documents = True

    async def assets(self, ctx: RecipeContext) -> list[StudyAsset]:
        quiz, links = await asyncio.gather(
            generate_quiz(ctx.material, ctx.task_title, ctx.user_prompt, from_knowledge=True),
            ctx.links(ctx.style_search(ctx.task_title, ctx.learning_style)),
        )
        return [*excerpt_assets(ctx.material), *quiz, *links]


class PracticeRecipe:
    kind = TaskKind.PRACTICE
    reads_documents = True

    async def assets(self, ctx: RecipeContext) -> list[StudyAsset]:
        quiz, links = await asyncio.gather(
            generate_quiz(ctx.material, ctx.task_title, ctx.user_prompt, from_knowledge=True),
            ctx.links(practice_links(ctx)),
        )
        return [*excerpt_assets(ctx.material), *quiz, *links]


class WriteRecipe:
    kind = TaskKind.WRITE
    reads_documents = True

    async def assets(self, ctx: RecipeContext) -> list[StudyAsset]:
        outline, links = await asyncio.gather(
            generate_outline(
                TaskKind.WRITE, ctx.task_title, ctx.completion_criteria, ctx.material,
                ctx.user_prompt,
            ),
            ctx.links(one_link(
                ctx,
                f"Search for one well-regarded guide, or one good example, for this piece of"
                f" writing: {ctx.task_title}.{ctx.asked()} Return exactly one result."
                " Format: URL | Title.",
                "You are a writing tutor. Find one real guide or example. Return only: URL | Title",
                rationale="A guide or a good example to follow",
            )),
        )
        return [*excerpt_assets(ctx.material), *outline, *links]


class BuildRecipe:
    kind = TaskKind.BUILD
    reads_documents = True

    async def assets(self, ctx: RecipeContext) -> list[StudyAsset]:
        steps, links = await asyncio.gather(
            generate_outline(
                TaskKind.BUILD, ctx.task_title, ctx.completion_criteria, ctx.material,
                ctx.user_prompt,
            ),
            ctx.links(one_link(
                ctx,
                f"Search for the most useful reference or official documentation for building:"
                f" {ctx.task_title}.{ctx.asked()} Return exactly one result."
                " Format: URL | Title.",
                "You are an engineering mentor. Find one real reference or documentation page."
                " Return only: URL | Title",
                rationale="A reference for building it",
            )),
        )
        return [*excerpt_assets(ctx.material), *steps, *links]


class AdminRecipe:
    kind = TaskKind.ADMIN
    reads_documents = False  # an errand needs no excerpts, and no web search

    async def assets(self, ctx: RecipeContext) -> list[StudyAsset]:
        return await generate_outline(
            TaskKind.ADMIN, ctx.task_title, ctx.completion_criteria, [], ctx.user_prompt
        )


_RECIPES: dict[TaskKind, WorkspaceRecipe] = {
    recipe.kind: recipe
    for recipe in (StudyRecipe(), PracticeRecipe(), WriteRecipe(), BuildRecipe(), AdminRecipe())
}


def recipe_for(kind: TaskKind) -> WorkspaceRecipe:
    """The recipe for ``kind``."""
    return _RECIPES[kind]


async def practice_links(ctx: RecipeContext) -> list[StudyAsset]:
    """Problems to practise on and one tutorial, from one web search. LeetCode and
    Codeforces only when the topic is programming; never otherwise, even if found."""
    programming = is_programming_topic(ctx.task_title, ctx.topic_keywords, ctx.goal_id)
    if programming:
        prompt = (
            f"Search for practice problems for: {ctx.task_title}.{ctx.asked()} Return (1) 2"
            " LeetCode or Codeforces problem URLs with titles and (2) 1 tutorial URL (YouTube"
            " or an article) with its title. Format each as: URL | Title."
        )
        system = (
            "You are a tutor. Find real LeetCode or Codeforces problems and one tutorial."
            " Return URL | Title per line."
        )
    else:
        prompt = (
            f"Search for practice material for: {ctx.task_title}.{ctx.asked()} Return (1) 2"
            " pages of practice questions or exercises, with answers where possible, and"
            " (2) 1 tutorial (YouTube or an article). Format each as: URL | Title."
        )
        system = (
            "You are a tutor. Find real practice exercises and one tutorial."
            " Return URL | Title per line."
        )
    raw = await ctx.search_web(prompt, system)
    assets: list[StudyAsset] = []
    for link in found_links(raw):
        if is_problem_site(link.url) and not programming:
            continue
        assets.append(StudyAsset(
            asset_type=link_type(link.url),
            title=link.title,
            content_or_url=link.url,
            rationale=f"Practice for {ctx.task_title}"[:200],
        ))
        if len(assets) >= MAX_PRACTICE_LINKS:
            break
    return assets


async def one_link(
    ctx: RecipeContext, prompt: str, system: str, *, rationale: str
) -> list[StudyAsset]:
    """The first link one web search answers that is not a problem site, or none."""
    raw = await ctx.search_web(prompt, system)
    for link in found_links(raw):
        if is_problem_site(link.url):
            continue
        return [StudyAsset(
            asset_type=link_type(link.url),
            title=link.title,
            content_or_url=link.url,
            rationale=rationale,
        )]
    return []
