"""Proactive Task Workspace: RAG + learning-style web search + dynamic practice assets.

Workspaces are now PERSISTED in `task_workspaces` table with a 24h TTL.
get_or_build_task_workspace() is the public entry — checks cache, returns if fresh,
else rebuilds via build_task_workspace() and upserts. The cache is dropped when the
documents linked to the task change (``DocumentLinker``, ``DocumentService.delete``).
"""

import asyncio
import json
import time
import re
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import CLAUDE_MODEL, SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.core.model_router import PreCloudLLMRedactor, metered_web_search
from app.domain.errors import LLMUnavailableError
from app.domain.events import EventBus
from app.domain.ports import DocumentRepository, GoalRepository
from app.models.brain.claude_web_search import claude_web_search
from app.models.brain.litellm_conf import gemini_web_search
from app.orchestrator.hooks import get_hooks
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.schemas.workspace import StudyAsset, TaskWorkspace, WoopCard
from app.services.analytical.workspace.materials import (
    MaterialChunk,
    excerpt_assets,
    fetch_chunks_for_task,
    sources_phrase,
)
from app.services.documents.service import DocumentService
from app.services.user_preferences import get_learning_style
from app.db.supabase_py import build_supabase_client

WORKSPACE_TTL_HOURS = 24

WEB_RESULTS_UNAVAILABLE = "Web results are unavailable right now, so no links are shown"


class WebSearchUnavailableError(LLMUnavailableError):
    """Gemini's grounded web search did not answer; there are no web results to show."""

    code = "web_search_unavailable"


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

    # Parse URL | Title lines
    url_re = re.compile(r"https?://[^\s|]+")
    lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n") if ln.strip()]
    seen_urls: set[str] = set()

    for line in lines:
        match = url_re.search(line)
        if match:
            url = match.group(0).rstrip(".,;)")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = line.split("|")[-1].strip() if "|" in line else url[:60]
            asset_type = "youtube_link" if "youtube.com" in url or "youtu.be" in url else "article_link"
            if learning_style == "interactive" and asset_type == "article_link" and "blog" in line.lower():
                asset_type = "blog_link"
            assets.append(
                StudyAsset(
                    asset_type=asset_type,
                    title=title[:200],
                    content_or_url=url,
                    rationale=f"Matches your {learning_style} learning style",
                )
            )
            if learning_style != "interactive" and len(assets) >= 1:
                break
            if learning_style == "interactive" and len(assets) >= 2:
                break

    return assets


async def generate_practice_assets(
    chunks: list[MaterialChunk],
    task_title: str,
    topic_keywords: list[str],
    learning_style: str = "reader",
    user_prompt: Optional[str] = None,
    *,
    bus: EventBus,
) -> list[StudyAsset]:
    """Generate context-aware practice assets.

    If the task has material: the PRIMARY model creates a 3-5 question quiz from it,
    named after the documents it came from, or none when the model finds the material
    is not about the task. If no material: Gemini searches for 2 LeetCode/Codeforces
    links + 1 YouTube, and ``WebSearchUnavailableError`` is raised when that search
    could not run. If user_prompt provided: incorporate into the generation.
    """
    assets: list[StudyAsset] = []

    if chunks:
        context = "\n\n".join(f"[From {c.file_name}]\n{c.text}" for c in chunks[:8])[:8000]
        user_hint = f"\n\nUser request: {user_prompt}" if user_prompt else ""
        prompt = f"""Based on the following material, create a short practice quiz (3-5 questions) testing "{task_title}".{user_hint}
Use the content heavily - pick or adapt questions from the material. Cover multiple topics if present.
Format as markdown with numbered questions and brief expected answers.
If the material is not about "{task_title}", return {{"assets": []}} and nothing else.

Materials:
{context}

Return a JSON object: {{"assets": [{{"asset_type": "generated_quiz", "title": "Quiz: ...", "content_or_url": "<markdown>"}}]}}"""
        rationale = f"From {sources_phrase(chunks)}"
        try:
            from app.core.model_router import route_llm_call

            result = await route_llm_call(
                task="practice_generation",
                prompt=prompt,
                system_prompt="You are a tutor. Generate practice questions from the materials. Return valid JSON only.",
            )
            if isinstance(result, str):
                import json
                try:
                    parsed = json.loads(result)
                    for a in parsed.get("assets", [])[:3]:
                        assets.append(StudyAsset(
                            asset_type=a.get("asset_type", "generated_quiz"),
                            title=a.get("title", "Practice Quiz"),
                            content_or_url=a.get("content_or_url", ""),
                            rationale=rationale,
                        ))
                except json.JSONDecodeError:
                    assets.append(StudyAsset(
                        asset_type="generated_quiz",
                        title=f"Quiz: {task_title}",
                        content_or_url=result[:4000],
                        rationale=rationale,
                    ))
        except Exception as exc:
            logger.warning("The practice quiz could not be generated (%s)", type(exc).__name__)

    else:
        topic = task_title or " ".join(topic_keywords) or "programming"
        user_hint = f" User specifically asked: {user_prompt}." if user_prompt else ""
        prompt = (
            f"Search for practice resources for: {topic}.{user_hint} "
            "Return: (1) 2 LeetCode or Codeforces problem URLs with titles, "
            "(2) 1 YouTube tutorial URL with title. "
            "Format each as: URL | Title. Use real URLs from leetcode.com, codeforces.com, youtube.com."
        )
        raw = await _search_web(
            prompt,
            "You are a tutor. Find real LeetCode, Codeforces, and YouTube URLs. Return URL | Title per line.",
            bus=bus,
        )
        if raw:
            url_re = re.compile(r"https?://[^\s|]+")
            for line in raw.replace("\r", "\n").split("\n"):
                line = line.strip()
                match = url_re.search(line)
                if match:
                    url = match.group(0).rstrip(".,;)")
                    title = line.split("|")[-1].strip() if "|" in line else url[:60]
                    if "leetcode.com" in url:
                        asset_type = "leetcode_link"
                    elif "codeforces.com" in url:
                        asset_type = "codeforces_link"
                    elif "youtube.com" in url or "youtu.be" in url:
                        asset_type = "youtube_link"
                    else:
                        asset_type = "article_link"
                    assets.append(StudyAsset(
                        asset_type=asset_type,
                        title=title[:200],
                        content_or_url=url,
                        rationale=f"Practice resource for {topic}",
                    ))
                    if len(assets) >= 3:
                        break

    return assets


async def build_task_workspace(
    user_id: str,
    task_id: str,
    user_prompt: Optional[str] = None,
    *,
    goals: GoalRepository | None = None,
    materials: DocumentRepository | None = None,
    bus: EventBus,
) -> TaskWorkspace:
    """Assemble proactive workspace: RAG chunks, web links, practice assets. Target <5s.

    ``goals`` (the composition root's repository) gives the WOOP card its wish and
    outcome; None leaves them at the task's title. ``materials`` (the composition root's
    document repository) gives the task's linked documents and their names; None means
    no material. Web searches are metered onto ``bus``, the composition root's, which is
    required (``metered_web_search``).
    """
    supabase = _get_supabase()
    task_title = ""
    topic_keywords: list[str] = []

    if supabase:
        try:
            task_result = (
                supabase.table("user_tasks")
                .select("title, topic_keywords")
                .eq("user_id", user_id)
                .eq("task_id", task_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if task_result.data and len(task_result.data) > 0:
                t = task_result.data[0]
                task_title = t.get("title", "") or ""
                kw = t.get("topic_keywords")
                topic_keywords = list(kw) if isinstance(kw, (list, tuple)) else []
        except Exception as e:
            print(f"[Workspace] fetch task error: {e}")
            task_title = task_id

    if not task_title:
        task_title = task_id

    primary_objective = f"{task_title} {' '.join(topic_keywords)}".strip() or task_id

    chunks_task = fetch_chunks_for_task(
        user_id, task_id, task_title, topic_keywords, documents=materials
    )
    style_task = get_learning_style(user_id, supabase)

    chunks, learning_style = await asyncio.gather(chunks_task, style_task)

    web_task = perform_learning_style_search(task_title, learning_style, bus=bus)
    practice_task = generate_practice_assets(
        chunks, task_title, topic_keywords, learning_style, user_prompt, bus=bus
    )

    web_assets, practice_assets = await asyncio.gather(
        _unless_search_failed(web_task), _unless_search_failed(practice_task)
    )
    notices = [WEB_RESULTS_UNAVAILABLE] if web_assets is None or practice_assets is None else []

    surfaced: list[StudyAsset] = excerpt_assets(chunks)
    surfaced.extend(web_assets or [])
    surfaced.extend(practice_assets or [])

    # Build WOOP card from task's implementation_intention + goal metadata
    woop_card = None
    task_data = None
    try:
        sb = _get_supabase()
        if sb:
            task_result = sb.table("user_tasks").select("implementation_intention, goal_id").eq("task_id", task_id).eq("user_id", user_id).limit(1).execute()
            if task_result.data:
                task_data = task_result.data[0]
                intention = task_data.get("implementation_intention") or {}
                if isinstance(intention, str):
                    try:
                        intention = json.loads(intention)
                    except (json.JSONDecodeError, TypeError):
                        intention = {}
                obstacle = intention.get("obstacle_trigger", "")
                plan = intention.get("behavioral_response", "")
                if obstacle and plan:
                    # Fetch goal metadata for Wish + Outcome stages
                    goal_id = task_data.get("goal_id", "")
                    wish = task_title
                    outcome = ""
                    if goal_id and goals is not None:
                        # goal_metadata is stored inside the update's context_snippet
                        snippet = await goals.latest_context(user_id, goal_id) or {}
                        if isinstance(snippet, str):
                            try:
                                snippet = json.loads(snippet)
                            except (json.JSONDecodeError, TypeError):
                                snippet = {}
                        if isinstance(snippet, dict):
                            wish = snippet.get("planning_goal", task_title)
                            meta = snippet.get("goal_metadata") or {}
                            if isinstance(meta, str):
                                try:
                                    meta = json.loads(meta)
                                except (json.JSONDecodeError, TypeError):
                                    meta = {}
                            outcome = meta.get("outcome_visualization", "") if isinstance(meta, dict) else ""
                    woop_card = WoopCard(wish=wish, outcome=outcome, obstacle=obstacle, plan=plan)
    except Exception as e:
        logger.warning(f"WOOP card construction failed (non-fatal): {e}")

    return TaskWorkspace(
        task_id=task_id,
        task_title=task_title,
        primary_objective=primary_objective,
        surfaced_assets=surfaced,
        woop_card=woop_card,
        notices=notices,
    )


async def _unless_search_failed(assets: Awaitable[list[StudyAsset]]) -> list[StudyAsset] | None:
    """The assets, or None when the web search behind them could not run."""
    try:
        return await assets
    except WebSearchUnavailableError:
        return None


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
    except Exception as e:
        logger.debug(f"Workspace cache read failed (non-fatal — table may not exist yet): {e}")
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
    except Exception as e:
        logger.debug(f"Workspace cache write failed (non-fatal — table may not exist yet): {e}")


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
) -> TaskWorkspace:
    """Cache-aware workspace fetch. Builds + persists if missing/stale/refresh=True.

    ``goals``, ``materials`` and ``bus`` are the composition root's (see
    ``build_task_workspace``).
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
            goals=goals, materials=materials, bus=bus,
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
