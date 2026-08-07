"""Memory CRUD endpoints for Jarvis."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

router = APIRouter()

_VALID_MEMORY_TYPES = {
    "constraint", "behavioral_pattern", "preference",
    "temporal_event", "goal", "fact", "feedback", "observation",
}


class MemoryEdit(BaseModel):
    content: Optional[str] = Field(default=None, max_length=2000)
    memory_type: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("memory_type")
    @classmethod
    def _check_memory_type(cls, v):
        if v is None:
            return v
        if v not in _VALID_MEMORY_TYPES:
            raise ValueError(f"memory_type must be one of {sorted(_VALID_MEMORY_TYPES)}")
        return v


def _get_memory_store(request: Request):
    store = getattr(request.app.state, "memory_store", None)
    if not store:
        raise HTTPException(status_code=503, detail="Memory store not available")
    return store


@router.get("/")
async def list_memories(
    user_id: str = Query(..., description="User ID"),
    memory_type: Optional[str] = Query(default=None, description="Filter by memory type"),
    min_confidence: float = Query(default=0.0, description="Minimum confidence threshold"),
    request: Request = None,
) -> dict:
    """List active memories for a user, optionally filtered by type."""
    store = _get_memory_store(request)
    if memory_type:
        memories = store.get_memories_by_type(user_id, memory_type, min_confidence)
    else:
        memories = store.get_active_memories(user_id)
    return {"memories": memories or [], "count": len(memories or [])}


@router.patch("/{memory_id}")
async def edit_memory(
    memory_id: str,
    edit: MemoryEdit,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """Edit a memory's content/type/confidence. Persists to user_memories table."""
    store = _get_memory_store(request)
    supabase = getattr(store, "_supabase", None) or getattr(store, "supabase_client", None)
    if not supabase:
        raise HTTPException(status_code=503, detail="Database unavailable")

    update: dict = {}
    if edit.content is not None:
        update["content"] = edit.content
    if edit.memory_type is not None:
        update["memory_type"] = edit.memory_type
    if edit.confidence is not None:
        update["confidence"] = max(0.0, min(1.0, edit.confidence))
    if not update:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        result = supabase.table("user_memories") \
            .update(update) \
            .eq("id", memory_id) \
            .eq("user_id", user_id) \
            .execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"status": "updated", "memory": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """Archive a memory (set strength to 0, excluded from active queries)."""
    store = _get_memory_store(request)
    success = store.archive_memory(memory_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "archived"}


@router.post("/{memory_id}/confirm")
async def confirm_memory(
    memory_id: str,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """User confirms a PEARL pattern — reinforce it."""
    store = _get_memory_store(request)
    success = store.reinforce_memory(memory_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "reinforced"}


@router.post("/{memory_id}/dismiss")
async def dismiss_memory(
    memory_id: str,
    user_id: str = Query(..., description="User ID"),
    request: Request = None,
) -> dict:
    """User dismisses a pattern — reduce confidence."""
    store = _get_memory_store(request)
    success = store.weaken_memory(memory_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "weakened"}
