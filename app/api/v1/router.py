"""Centralized endpoint routing for API v1."""

from fastapi import APIRouter

from app.api.v1.endpoints import chat, habits, reasoning, schedule, ingestion

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
