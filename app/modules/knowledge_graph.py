"""Knowledge module sub-graph — document ingestion, file ops, ChromaDB, task linking."""

from typing import Any, Optional, TypedDict
from langgraph.graph import END, StateGraph
from app.core.jarvis_logger import JARVIS_LOGGER as logger


class KnowledgeState(TypedDict):
    user_id: str
    user_model: Any
    content: Optional[str]
    file_bytes: Optional[bytes]
    media_type: Optional[str]
    file_name: Optional[str]
    content_type: Optional[str]
    ingestion_result: Optional[dict]
    calendar_result: Optional[dict]
    linked_tasks: list
    action_proposals: list
    error: Optional[str]


async def classify_content(state: KnowledgeState) -> dict:
    content = state.get("content", "") or ""
    file_name = state.get("file_name", "") or ""
    if "calendar" in content.lower() or "timetable" in file_name.lower():
        return {"content_type": "calendar"}
    elif state.get("file_bytes") or state.get("media_type"):
        return {"content_type": "document"}
    return {"content_type": "file_op"}


def content_type_router(state: KnowledgeState) -> str:
    return state.get("content_type", "document")


async def extract_calendar(state: KnowledgeState) -> dict:
    return {"calendar_result": {"status": "pending_approval"}}


async def ingest_document(state: KnowledgeState) -> dict:
    try:
        from app.services.extraction.orchestrator import process_ingestion
        result = await process_ingestion(
            payload=state.get("content"),
            file_bytes=state.get("file_bytes"),
            media_type=state.get("media_type"),
            user_id=state.get("user_id"),
            file_name=state.get("file_name"),
        )
        return {"ingestion_result": result.model_dump() if hasattr(result, "model_dump") else {"status": "ok"}}
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        return {"error": str(e)}


async def link_to_tasks(state: KnowledgeState) -> dict:
    return {"linked_tasks": []}


async def file_operations(state: KnowledgeState) -> dict:
    return {}


async def propose_actions(state: KnowledgeState) -> dict:
    return {"action_proposals": []}


def build_knowledge_graph():
    graph = StateGraph(KnowledgeState)
    graph.add_node("classify_content", classify_content)
    graph.add_node("extract_calendar", extract_calendar)
    graph.add_node("ingest_document", ingest_document)
    graph.add_node("link_to_tasks", link_to_tasks)
    graph.add_node("file_operations", file_operations)
    graph.add_node("propose_actions", propose_actions)

    graph.set_entry_point("classify_content")
    graph.add_conditional_edges("classify_content", content_type_router, {
        "calendar": "extract_calendar",
        "document": "ingest_document",
        "file_op": "file_operations",
    })
    graph.add_edge("ingest_document", "link_to_tasks")
    graph.add_edge("link_to_tasks", "propose_actions")
    graph.add_edge("propose_actions", END)
    graph.add_edge("extract_calendar", END)
    graph.add_edge("file_operations", END)
    return graph.compile()
