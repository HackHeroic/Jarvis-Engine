"""Proactive Task Workspace: RAG + learning-style web search + dynamic practice assets."""

import asyncio
import re
from typing import Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL
from app.models.brain.litellm_conf import gemini_web_search, hybrid_route_query
from app.core.jarvis_logger import JARVIS_LOGGER as logger
from app.schemas.workspace import StudyAsset, TaskWorkspace, WoopCard
from app.services.user_preferences import get_learning_style
from supabase import create_client


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def fetch_chunks_for_task(
    user_id: str,
    task_id: str,
    task_title: str = "",
    topic_keywords: Optional[list[str]] = None,
) -> list[str]:
    """Fetch RAG chunks linked to this task via task_materials.

    For each source_id in task_materials, query ChromaDB. If no links exist,
    fall back to semantic query on task title + topic_keywords.
    """
    chunks: list[str] = []
    supabase = _get_supabase()
    topic_kw = topic_keywords or []

    try:
        from app.utils.chroma_client import get_chroma_client

        client = get_chroma_client()
        collection = client.get_or_create_collection("jarvis_knowledge", metadata={"hnsw:space": "cosine"})

        if supabase:
            result = (
                supabase.table("task_materials")
                .select("source_id")
                .eq("user_id", user_id)
                .eq("task_id", task_id)
                .execute()
            )
            source_ids = [r["source_id"] for r in (result.data or []) if r.get("source_id")]

            for sid in source_ids:
                try:
                    out = collection.get(where={"source_id": sid})
                    if out and out.get("documents"):
                        chunks.extend(out["documents"])
                except Exception:
                    pass

        if not chunks and (task_title or topic_kw):
            query_text = f"{task_title} {' '.join(topic_kw)}".strip()
            if query_text:
                try:
                    out = collection.query(query_texts=[query_text], n_results=5)
                    if out and out.get("documents") and len(out["documents"]) > 0:
                        chunks.extend(out["documents"][0])
                except Exception:
                    pass
    except ImportError:
        pass
    except Exception as e:
        print(f"[Workspace] fetch_chunks_for_task error: {e}")

    return chunks


async def perform_learning_style_search(task_title: str, learning_style: str) -> list[StudyAsset]:
    """Use Gemini + Google Search to find learning-style-curated links (YouTube or articles)."""
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

    raw = await gemini_web_search(user_prompt=prompt, system_prompt=sys)
    if not raw:
        return assets

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
    chunks: list[str],
    task_title: str,
    topic_keywords: list[str],
    learning_style: str = "reader",
    user_prompt: Optional[str] = None,
) -> list[StudyAsset]:
    """Generate context-aware practice assets.

    If RAG chunks exist: local 27B creates 3-5 question quiz from materials.
    If no chunks: Gemini searches for 2 LeetCode/Codeforces links + 1 YouTube.
    If user_prompt provided: incorporate into the generation.
    """
    assets: list[StudyAsset] = []

    if chunks:
        context = "\n\n".join(chunks[:8])[:8000]
        user_hint = f"\n\nUser request: {user_prompt}" if user_prompt else ""
        prompt = f"""Based on the following syllabus/materials, create a short practice quiz (3-5 questions) testing "{task_title}".{user_hint}
Use the content heavily - pick or adapt questions from the material. Cover multiple topics if present.
Format as markdown with numbered questions and brief expected answers.

Materials:
{context}

Return a JSON object: {{"assets": [{{"asset_type": "generated_quiz", "title": "Quiz: ...", "content_or_url": "<markdown>", "rationale": "From your syllabus"}}]}}"""
        try:
            result = await hybrid_route_query(
                user_prompt=prompt,
                system_prompt="You are a tutor. Generate practice questions from the materials. Return valid JSON only.",
                response_schema=None,
                force_cloud=False,
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
                            rationale=a.get("rationale", "From your syllabus"),
                        ))
                except json.JSONDecodeError:
                    assets.append(StudyAsset(
                        asset_type="generated_quiz",
                        title=f"Quiz: {task_title}",
                        content_or_url=result[:4000],
                        rationale="Generated from your materials",
                    ))
        except Exception as e:
            print(f"[Workspace] generate_practice_assets (chunks) error: {e}")

    else:
        topic = task_title or " ".join(topic_keywords) or "programming"
        user_hint = f" User specifically asked: {user_prompt}." if user_prompt else ""
        prompt = (
            f"Search for practice resources for: {topic}.{user_hint} "
            "Return: (1) 2 LeetCode or Codeforces problem URLs with titles, "
            "(2) 1 YouTube tutorial URL with title. "
            "Format each as: URL | Title. Use real URLs from leetcode.com, codeforces.com, youtube.com."
        )
        raw = await gemini_web_search(
            user_prompt=prompt,
            system_prompt="You are a tutor. Find real LeetCode, Codeforces, and YouTube URLs. Return URL | Title per line.",
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
) -> TaskWorkspace:
    """Assemble proactive workspace: RAG chunks, web links, practice assets. Target <5s."""
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

    chunks_task = fetch_chunks_for_task(user_id, task_id, task_title, topic_keywords)
    style_task = get_learning_style(user_id, supabase)

    chunks, learning_style = await asyncio.gather(chunks_task, style_task)

    web_task = perform_learning_style_search(task_title, learning_style)
    practice_task = generate_practice_assets(
        chunks, task_title, topic_keywords, learning_style, user_prompt
    )

    web_assets, practice_assets = await asyncio.gather(web_task, practice_task)

    surfaced: list[StudyAsset] = []

    for i, c in enumerate(chunks[:5]):
        surfaced.append(
            StudyAsset(
                asset_type="pdf_chunk",
                title=f"Material excerpt {i + 1}",
                content_or_url=c[:3000],
                rationale="From your ingested materials",
            )
        )

    surfaced.extend(web_assets)
    surfaced.extend(practice_assets)

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
                    import json
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
                    if goal_id:
                        goal_result = sb.table("user_plan_updates").select("goal_metadata, planning_goal").eq("user_id", user_id).eq("goal_id", goal_id).limit(1).execute()
                        if goal_result.data:
                            gm = goal_result.data[0]
                            wish = gm.get("planning_goal", task_title)
                            meta = gm.get("goal_metadata") or {}
                            if isinstance(meta, str):
                                import json
                                try:
                                    meta = json.loads(meta)
                                except (json.JSONDecodeError, TypeError):
                                    meta = {}
                            outcome = meta.get("outcome_visualization", "")
                    woop_card = WoopCard(wish=wish, outcome=outcome, obstacle=obstacle, plan=plan)
    except Exception as e:
        logger.warning(f"WOOP card construction failed (non-fatal): {e}")

    return TaskWorkspace(
        task_id=task_id,
        task_title=task_title,
        primary_objective=primary_objective,
        surfaced_assets=surfaced,
        woop_card=woop_card,
    )
