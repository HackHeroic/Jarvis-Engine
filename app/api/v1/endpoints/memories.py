"""Memory CRUD endpoints for Jarvis."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


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
