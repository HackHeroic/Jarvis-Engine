"""Centralized endpoint routing for API v1."""

from fastapi import APIRouter

from app.api.v1.endpoints import reasoning, schedule

api_router = APIRouter()
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
