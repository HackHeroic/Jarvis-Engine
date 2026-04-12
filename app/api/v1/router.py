"""Centralized endpoint routing for API v1."""

from fastapi import APIRouter

from app.api.v1.endpoints import chat, documents, habits, models, reasoning, schedule, ingestion, tasks, workspace, lmstudio_chat
from app.api.v1.endpoints.drafts import router as drafts_router
from app.api.v1.endpoints.memories import router as memories_router
from app.api.v1.endpoints.sessions import router as sessions_router

api_router = APIRouter()
api_router.include_router(
    chat.router,
    prefix="/chat",
    tags=["Chat"],
)
api_router.include_router(
    reasoning.router,
    prefix="/reasoning",
    tags=["Reasoning"],
)
api_router.include_router(
    schedule.router,
    prefix="/schedule",
    tags=["Scheduling"],
)
api_router.include_router(
    ingestion.router,
    prefix="/ingestion",
    tags=["Ingestion"],
)
api_router.include_router(
    habits.router,
    prefix="/habits",
    tags=["Habits"],
)
api_router.include_router(
    workspace.router,
    prefix="/tasks",
    tags=["Workspace"],
)
api_router.include_router(
    tasks.router,
    prefix="/tasks",
    tags=["Tasks"],
)
api_router.include_router(
    lmstudio_chat.router,
    prefix="/lmstudio",
    tags=["LM Studio"],
)
api_router.include_router(
    documents.router,
    prefix="/documents",
    tags=["Documents"],
)
api_router.include_router(
    models.router,
    prefix="/models",
    tags=["Models"],
)
api_router.include_router(drafts_router, prefix="/drafts", tags=["drafts"])
api_router.include_router(memories_router, prefix="/memories", tags=["Memories"])
api_router.include_router(sessions_router, prefix="/sessions", tags=["Sessions"])
