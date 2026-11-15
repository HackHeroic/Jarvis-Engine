"""Proactive Task Workspace: the right kind of help for the kind of task.

A task's workspace is built by the recipe for its kind (``task_kind.classify_task``,
``workspace/recipes.py``): material from the documents that are about it
(``workspace/materials.py``), a quiz (``workspace/quiz.py``) or an outline
(``workspace/outline.py``), and links from at most one web search.

Workspaces are PERSISTED in `task_workspaces` table with a 24h TTL.
get_or_build_task_workspace() is the public entry — checks cache, returns if fresh,
else rebuilds via build_task_workspace() and upserts. The cache is dropped when the
documents linked to the task change (``DocumentLinker``, ``DocumentService.delete``).
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Optional

from app.core import config
from app.core.config import CLAUDE_MODEL, SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.core.model_router import PreCloudLLMRedactor, metered_web_search
from app.domain import errors
from app.domain.entities import Task
from app.domain.errors import LLMUnavailableError
from app.domain.events import EventBus
from app.domain.ports import DocumentRepository, GoalRepository, TaskRepository
from app.models.brain.claude_web_search import claude_web_search
from app.models.brain.litellm_conf import gemini_web_search
from app.orchestrator.hooks import get_hooks
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.schemas.workspace import StudyAsset, TaskWorkspace
from app.services.analytical.task_kind import classify_task, is_programming_topic
from app.services.analytical.workspace.composer import (
    ComposerTask,
    compose_workspace,
    composed_links,
)
from app.services.analytical.workspace.materials import (
    MaterialChunk,
    excerpt_assets,
    fetch_chunks_for_task,
)
from app.services.analytical.workspace.recipes import RecipeContext, recipe_for
from app.services.analytical.workspace.web import (
    WEB_RESULTS_UNAVAILABLE,
    SearchWeb,
    WebSearchUnavailableError,
    found_links,
    link_type,
)
from app.services.analytical.workspace.woop import woop_card
from app.services.documents.service import DocumentService
from app.services.memory.memory_service import MemoryService
from app.services.user_preferences import get_learning_style
from app.db.supabase_py import build_supabase_client

WORKSPACE_TTL_HOURS = 24

_NO_TASK_STORE = "Could not reach the database."
_NO_SUCH_TASK = "That task does not exist."


# After the web search fails (no key, no quota, the provider is down) it is not tried
# again for this long: every workspace would otherwise wait on a call that is known to
# fail. Process-local, like the other single-process guards.
WEB_SEARCH_COOLDOWN_S = 300.0


class _WebSearchCooldown:
    """Remembers that the web search just failed, for ``WEB_SEARCH_COOLDOWN_S``."""

    def __init__(self) -> None:
        self._down_until = 0.0

    def is_down(self) -> bool:
        return time.monotonic() < self._down_until

    def mark_down(self) -> None:
        self._down_until = time.monotonic() + WEB_SEARCH_COOLDOWN_S

    def reset(self) -> None:
        self._down_until = 0.0


web_search_cooldown = _WebSearchCooldown()


async def _search_web(user_prompt: str, system_prompt: str, *, bus: EventBus) -> str:
    """Search the web, raising ``WebSearchUnavailableError`` when no search answers.

    Claude's server-side search first (the key that serves chat), then Gemini's
    grounded search. Only a model that can search is asked: one that cannot would
    invent links. Each answers an empty string when its key is missing or the call
    fails, and a search nobody answered starts the cooldown.

    The query carries user text (a task title, the chat message) and goes to the
    cloud outside the provider chain, so the chain's PII gate is run here first. A
    gate that denies the send, or wants to ask, means no search is made. An answered
    search is metered onto ``bus``, the composition root's (``metered_web_search``),
    under the provider that answered it.
    """
    if web_search_cooldown.is_down():
        raise WebSearchUnavailableError(WEB_RESULTS_UNAVAILABLE)
    redactor = PreCloudLLMRedactor(get_hooks())
    try:
        user_prompt, redacted_in_prompt = await redactor.redact(user_prompt)
        system_prompt, redacted_in_system = await redactor.redact(system_prompt)
    except LLMUnavailableError as exc:
        raise WebSearchUnavailableError(WEB_RESULTS_UNAVAILABLE) from exc
    redactions = redacted_in_prompt + redacted_in_system
    # Metered into the usage ledger like a chain call: the chain never sees a search.
    raw = await metered_web_search(
        lambda: claude_web_search(user_prompt=user_prompt, system_prompt=system_prompt),
        bus=bus,
        pii_redactions=redactions,
        provider="claude",
        model=CLAUDE_MODEL,
    )
    if not raw or not raw.strip():
        raw = await metered_web_search(
            lambda: gemini_web_search(user_prompt=user_prompt, system_prompt=system_prompt),
            bus=bus,
            pii_redactions=redactions,
        )
    if not raw or not raw.strip():
        web_search_cooldown.mark_down()
        raise WebSearchUnavailableError(WEB_RESULTS_UNAVAILABLE)
    return raw


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return build_supabase_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def perform_learning_style_search(
    task_title: str, learning_style: str, *, bus: EventBus
) -> list[StudyAsset]:
    """Use Gemini + Google Search to find learning-style-curated links (YouTube or articles).

    Raises ``WebSearchUnavailableError`` when the search could not run. The search is
    metered onto ``bus`` (the composition root's), which is required.
    """
    assets: list[StudyAsset] = []
    if not task_title.strip():
        return assets

    if learning_style == "watcher":
        prompt = (
            f"Search for the best highly-rated YouTube tutorial explaining: {task_title}. "
            "Return exactly one result. Format: URL | Title (one line, pipe-separated)."
        )
        sys = "You are a tutor. Find the best YouTube tutorial. Return only: URL | Title"
    elif learning_style == "reader":
        prompt = (
            f"Search for the best comprehensive written guide or documentation for: {task_title}. "
            "Return exactly one result. Format: URL | Title (one line, pipe-separated)."
        )
        sys = "You are a tutor. Find the best article or docs. Return only: URL | Title"
    else:
        prompt = (
            f"For topic '{task_title}', find: (1) One YouTube tutorial URL and title, "
            "(2) One article/blog URL and title. Format: YouTube: URL | Title. Article: URL | Title"
        )
        sys = "You are a tutor. Return YouTube and Article lines with URL | Title"

    raw = await _search_web(prompt, sys, bus=bus)

    for link in found_links(raw):
        asset_type = "youtube_link" if link_type(link.url) == "youtube_link" else "article_link"
        if learning_style == "interactive" and asset_type == "article_link" and "blog" in link.line.lower():
            asset_type = "blog_link"
        assets.append(
            StudyAsset(
                asset_type=asset_type,
                title=link.title,
                content_or_url=link.url,
                rationale=f"Matches your {learning_style} learning style",
            )
        )
        if learning_style != "interactive" and len(assets) >= 1:
            break
        if learning_style == "interactive" and len(assets) >= 2:
            break

    return assets


async def build_task_workspace(
    user_id: str,
    task_id: str,
    user_prompt: Optional[str] = None,
    *,
    tasks: TaskRepository | None,
    goals: GoalRepository | None = None,
    materials: DocumentRepository | None = None,
    memory: MemoryService | None = None,
    bus: EventBus,
) -> TaskWorkspace:
    """Assemble the task's workspace: composed for it by the model (``composer``), from
    the task, its material and what Jarvis remembers about the user (``memory``); or,
    when the model cannot answer or ``WORKSPACE_COMPOSER`` is off, the fixed recipe of
    the task's kind.

    The task is read once, the user's own, through ``tasks`` (the composition root's
    repository): its title, keywords, goal, criteria, length, deadline and if-then plan.
    NotFoundError when the user has no such task (another user's included);
    PersistenceError when the task store cannot answer, or there is none. ``goals``
    gives the WOOP card its wish and outcome; None leaves them at the task's title.
    ``materials`` (the document repository) gives the task's linked documents and their
    names; None means no material. Web searches are metered onto ``bus``, the
    composition root's, which is required (``metered_web_search``).
    """
    if tasks is None:
        raise errors.PersistenceError(_NO_TASK_STORE)
    task = await tasks.get(user_id, task_id)
    if task is None:
        raise errors.NotFoundError(_NO_SUCH_TASK, details={"task_id": task_id})
    title = task.title.strip() or task_id
    keywords = task.topic_keywords
    kind = classify_task(title, keywords, task.goal_id)
    recipe = recipe_for(kind)
    criteria = _criteria(task, title)
    composing = config.WORKSPACE_COMPOSER

    material, learning_style, known = await asyncio.gather(
        fetch_chunks_for_task(user_id, task_id, title, keywords, documents=materials)
        if composing or recipe.reads_documents
        else _no_material(),
        get_learning_style(user_id, _get_supabase()),
        _known_about_user(memory, user_id, title) if composing else _nothing_known(),
    )
    card = await woop_card(user_id, task, title, goals)
    if composing:
        composed = await _composed_workspace(
            ComposerTask(
                title=title,
                completion_criteria=criteria,
                duration_minutes=task.duration_minutes,
                deadline_hint=task.deadline_hint,
                goal=card.wish if card is not None else "",
                learning_style=learning_style,
                user_prompt=user_prompt,
                known_about_user=known,
            ),
            material,
            programming=is_programming_topic(title, keywords, task.goal_id),
            search_web=partial(_search_web, bus=bus),
        )
        if composed is not None:
            return TaskWorkspace(
                task_id=task_id,
                task_title=title,
                primary_objective=f"{title} {' '.join(keywords)}".strip(),
                task_kind=composed.kind,
                approach=composed.approach,
                completion_criteria=criteria,
                duration_minutes=task.duration_minutes,
                deadline_hint=task.deadline_hint,
                surfaced_assets=composed.assets,
                woop_card=card,
                notices=composed.notices,
            )
        if not recipe.reads_documents:
            material = []  # the fallback recipe for an errand shows no excerpts
    ctx = RecipeContext(
        task_title=title,
        topic_keywords=keywords,
        goal_id=task.goal_id,
        completion_criteria=criteria,
        learning_style=learning_style,
        user_prompt=user_prompt,
        material=material,
        search_web=partial(_search_web, bus=bus),
        style_search=partial(perform_learning_style_search, bus=bus),
    )
    surfaced = await recipe.assets(ctx)

    return TaskWorkspace(
        task_id=task_id,
        task_title=title,
        primary_objective=f"{title} {' '.join(keywords)}".strip(),
        task_kind=kind.value,
        completion_criteria=criteria,
        duration_minutes=task.duration_minutes,
        deadline_hint=task.deadline_hint,
        surfaced_assets=surfaced,
        woop_card=card,
        notices=[WEB_RESULTS_UNAVAILABLE] if ctx.web_unavailable else [],
    )


@dataclass(frozen=True)
class _ComposedWorkspace:
    """A composed workspace ready to return: its assets in order, and what to say is missing."""

    kind: str
    approach: str
    assets: list[StudyAsset]
    notices: list[str]


async def _composed_workspace(
    task: ComposerTask,
    material: list[MaterialChunk],
    *,
    programming: bool,
    search_web: SearchWeb,
) -> _ComposedWorkspace | None:
    """The workspace the model composes for the task (``composer``): the excerpts of the
    material it used, its blocks, then the links of the one search it asked for. None
    when it could not compose one; the caller then builds the kind's fixed recipe.

    One PRIMARY-role call, then at most one web search (after it: the search is the
    model's own choice). A search that cannot run leaves the notice, not a failure.
    """
    composed = await compose_workspace(task, material)
    if composed is None:
        return None
    links: list[StudyAsset] = []
    notices: list[str] = []
    if composed.search is not None:
        try:
            links = await composed_links(
                composed.search, task.title, programming=programming, search_web=search_web
            )
        except WebSearchUnavailableError:
            notices.append(WEB_RESULTS_UNAVAILABLE)
    excerpts = excerpt_assets(material) if composed.material_used else []
    return _ComposedWorkspace(
        kind=composed.kind,
        approach=composed.approach,
        assets=[*excerpts, *composed.assets, *links],
        notices=notices,
    )


async def _known_about_user(
    memory: MemoryService | None, user_id: str, task_title: str
) -> tuple[str, ...]:
    """What Jarvis remembers that bears on the task: the standing profile and the
    memories nearest the task's title, in words. Best effort: nothing when there is no
    memory service or it cannot answer (logged by type)."""
    if memory is None:
        return ()
    try:
        recall = await memory.recall(user_id, task_title)
    except Exception as exc:
        logger.warning("The workspace was composed without memories (%s)", type(exc).__name__)
        return ()
    return tuple(m.content.strip() for m in recall.all() if m.content and m.content.strip())


async def _nothing_known() -> tuple[str, ...]:
    return ()


def _criteria(task: Task, title: str) -> str:
    """The task's completion criteria, or "" when it has none worth showing: a carried-
    over task gets the filler "Finish: <title>" (``task_rows.to_task_chunk``), which only
    says the title again."""
    criteria = (task.completion_criteria or "").strip()
    return "" if criteria in ("", f"Finish: {title}", f"Finish: {task.title}") else criteria


async def _no_material() -> list[MaterialChunk]:
    """A recipe that reads no documents gets no material, and no document is read."""
    return []


# ─── Workspace persistence ────────────────────────────────────────────

async def _read_cached_workspace(user_id: str, task_id: str) -> Optional[TaskWorkspace]:
    """Return cached workspace if still fresh (within TTL). Tolerates missing table."""
    sb = _get_supabase()
    if not sb:
        return None
    try:
        result = await asyncio.to_thread(
            lambda: sb.table("task_workspaces")
            .select("workspace_json, expires_at")
            .eq("user_id", user_id)
            .eq("task_id", task_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        row = rows[0]
        expires_at = row.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc):
                    return None  # stale → caller will rebuild
            except Exception:
                pass
        ws_json = row.get("workspace_json")
        if isinstance(ws_json, str):
            ws_json = json.loads(ws_json)
        if isinstance(ws_json, dict):
            return TaskWorkspace.model_validate(ws_json)
    except Exception as exc:
        logger.debug(
            "Workspace cache read failed (non-fatal — table may not exist yet): %s",
            type(exc).__name__,
        )
    return None


async def _write_cached_workspace(user_id: str, task_id: str, ws: TaskWorkspace) -> None:
    """Persist workspace with 24h TTL. Tolerates missing table (silent no-op)."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        now = datetime.now(timezone.utc)
        payload = {
            "user_id": user_id,
            "task_id": task_id,
            "workspace_json": ws.model_dump(mode="json"),
            "built_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=WORKSPACE_TTL_HOURS)).isoformat(),
        }
        await asyncio.to_thread(
            lambda: sb.table("task_workspaces")
            .upsert(payload, on_conflict="user_id,task_id")
            .execute()
        )
    except Exception as exc:
        logger.debug(
            "Workspace cache write failed (non-fatal — table may not exist yet): %s",
            type(exc).__name__,
        )


async def invalidate_workspace_cache(user_id: str, task_id: str) -> None:
    """Drop cached workspace for a task. Called when new materials get linked."""
    sb = _get_supabase()
    if not sb:
        return
    try:
        await asyncio.to_thread(
            lambda: sb.table("task_workspaces")
            .delete()
            .eq("user_id", user_id)
            .eq("task_id", task_id)
            .execute()
        )
    except Exception:
        pass


async def invalidate_workspaces(user_id: str, task_ids: list[str]) -> None:
    """Drop the cached workspaces of these tasks: the documents linked to them changed."""
    for task_id in task_ids:
        await invalidate_workspace_cache(user_id, task_id)


async def get_or_build_task_workspace(
    user_id: str,
    task_id: str,
    user_prompt: Optional[str] = None,
    refresh: bool = False,
    *,
    goals: GoalRepository | None = None,
    bus: EventBus,
    documents: DocumentService | None = None,
    materials: DocumentRepository | None = None,
    tasks: TaskRepository | None = None,
    memory: MemoryService | None = None,
) -> TaskWorkspace:
    """Cache-aware workspace fetch. Builds + persists if missing/stale/refresh=True.

    ``tasks``, ``goals``, ``materials``, ``memory`` and ``bus`` are the composition root's (see
    ``build_task_workspace``, whose NotFoundError and PersistenceError pass through).
    ``documents`` (the composition root's DocumentService) lists the task's linked
    documents, read fresh on every request after the cache, so a document deleted or a
    file kept since shows as it is now; None leaves the list empty.
    """
    ws = None
    if not refresh:
        ws = await _read_cached_workspace(user_id, task_id)
    if ws is None:
        ws = await build_task_workspace(
            user_id=user_id, task_id=task_id, user_prompt=user_prompt,
            tasks=tasks, goals=goals, materials=materials, memory=memory, bus=bus,
        )
        if not ws.notices:
            # A workspace missing its web results is not cached: once search is back,
            # the next open rebuilds it instead of repeating the notice for a day.
            await _write_cached_workspace(user_id, task_id, ws)
    if documents is None:
        return ws
    return ws.model_copy(
        update={"linked_documents": await documents.linked_documents(user_id, task_id)}
    )
