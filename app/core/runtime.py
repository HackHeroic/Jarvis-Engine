"""Process-wide registry for the shared clients created at startup.

Request handlers reach the db through ``app.state``. Code that
runs *outside* a request scope cannot: a checkpoint-resumed graph turn arrives
with only the serializable ``user_id`` and has to rebuild the ``UserModel``
facade from scratch, and a facade wired to ``db=None`` is worse than no facade
at all — it is truthy, so it passes every ``if user_model:`` guard and then
raises ``AttributeError`` deep inside a module.

So the lifespan registers the same db client here once at startup, and
``_load_context`` reads them back. The ``AppContainer`` is registered the same
way, for code that reaches services without a request (the orchestrator's
draft accept). Accessors return ``None`` before startup (tests, scripts) —
callers degrade rather than crash.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.container import AppContainer

_db: Any = None
_container: "AppContainer | None" = None


def set_shared_clients(db: Any = None) -> None:
    """Register the startup db client. Called once, from the FastAPI lifespan.

    ``None`` leaves an earlier registration in place.
    """
    global _db
    if db is not None:
        _db = db


def get_db() -> Any:
    """The shared ``DatabaseClient``, or ``None`` before startup registered it."""
    return _db


def set_container(container: "AppContainer") -> None:
    """Register the composition root. Called once, from the FastAPI lifespan."""
    global _container
    _container = container


def registered_container() -> "AppContainer | None":
    """The ``AppContainer`` the lifespan built, or ``None`` before startup (tests, scripts).

    Not ``app.api.deps.get_container``: that one is the request-scoped FastAPI
    dependency; this is for code with no request in hand.
    """
    return _container


def reset_shared_clients() -> None:
    """Drop every registration. Test hygiene only — never call from app code."""
    global _db, _container
    _db = None
    _container = None
