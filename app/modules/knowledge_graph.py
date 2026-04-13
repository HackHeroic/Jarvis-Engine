"""Knowledge module sub-graph — document ingestion, file ops, ChromaDB, task linking."""

from typing import Any, Optional, TypedDict

from app.core.jarvis_logger import JARVIS_LOGGER as logger


class KnowledgeState(TypedDict):
    user_id: str
    user_model: Any
    db_client: Any  # DatabaseClient — needed for ingested_documents persistence
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
    progress_queue: Any


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
            db_client=state.get("db_client"),
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


from app.core.module_framework import ModuleStep, ModuleDefinition


def knowledge_state_in(state) -> dict:
    user_model = state.get("user_model")
    _file_bytes = None
    _file_b64 = state.get("file_base64")
    if _file_b64:
        import base64
        _file_bytes = base64.b64decode(_file_b64)
    _db_client = getattr(user_model, "_db", None) if user_model else None
    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "db_client": _db_client,
        "content": state.get("user_message", ""),
        "file_bytes": _file_bytes,
        "media_type": state.get("file_media_type"),
        "file_name": state.get("file_name"),
        "content_type": None,
        "ingestion_result": None,
        "calendar_result": None,
        "linked_tasks": [],
        "action_proposals": [],
        "error": None,
        "progress_queue": state.get("progress_queue"),
    }


def knowledge_state_out(result: dict, module_name: str) -> dict:
    return {
        "ingestion_result": result.get("ingestion_result"),
        "error": result.get("error"),
    }


knowledge_module = ModuleDefinition(
    name="knowledge_module",
    state_class=KnowledgeState,
    state_in=knowledge_state_in,
    state_out=knowledge_state_out,
    steps=[
        ModuleStep(name="classify_content", handler=classify_content, read_only=True,
                   routes_to={content_type_router: {
                       "calendar": "extract_calendar",
                       "document": "ingest_document",
                       "file_op": "file_operations",
                   }}),
        ModuleStep(name="extract_calendar", handler=extract_calendar),
        ModuleStep(name="ingest_document", handler=ingest_document, timeout_ms=60_000),
        ModuleStep(name="link_to_tasks", handler=link_to_tasks,
                   depends_on=["ingest_document"]),
        ModuleStep(name="propose_actions", handler=propose_actions,
                   depends_on=["link_to_tasks"]),
        ModuleStep(name="file_operations", handler=file_operations),
    ],
)


def build_knowledge_graph():
    from app.core.module_framework import build_module_graph
    return build_module_graph(knowledge_module)
