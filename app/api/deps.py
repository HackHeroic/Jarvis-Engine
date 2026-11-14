"""Request dependencies shared by the v1 routes.

``CurrentUser`` is the single seam for caller identity. Today it reads a
required ``user_id`` query parameter; when auth lands, a verified JWT subject
replaces it here and every route that declares ``user: CurrentUser`` follows.
"""

from typing import Annotated, Any

from fastapi import Depends, Query, Request

from app.container import AppContainer
from app.domain import errors


async def current_user(
    user_id: str = Query(
        ...,
        min_length=1,
        max_length=128,
        description="Caller identity. Auth seam: a JWT subject replaces this.",
    ),
) -> str:
    """The id of the user making the request."""
    return user_id


CurrentUser = Annotated[str, Depends(current_user)]


def require_supabase(request: Request) -> Any:
    """The live Supabase client, or ``PersistenceError`` when the app has none."""
    db_client = getattr(request.app.state, "db_client", None)
    supabase = getattr(db_client, "supabase", None)
    if supabase is None:
        raise errors.PersistenceError("Could not reach the database.")
    return supabase


def container_of(request: Request) -> AppContainer | None:
    """The composition root on ``app.state``, or None when the lifespan built none
    (a route that degrades rather than fails without it)."""
    return getattr(request.app.state, "container", None)


def get_container(request: Request) -> AppContainer:
    """The composition root the lifespan built (``app.state.container``)."""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("No AppContainer on app.state; the lifespan builds it at startup.")
    return container
