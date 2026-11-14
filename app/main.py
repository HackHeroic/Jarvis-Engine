"""Entry point: initializes FastAPI and LiteLLM Router."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request

# Stub used when Supabase fails at startup so the app can still run
class _StubDBClient:
    supabase = None  # type: ignore[assignment]

    async def check_connection(self) -> bool:
        return False
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import REQUEST_ID_HEADER, RequestIdMiddleware, register_error_handlers
from app.api.v1.router import api_router
from app.db.supabase_py import DatabaseClient
from app.domain.errors import PersistenceError

logger = logging.getLogger(__name__)


def _log_warm_up_failure(task: asyncio.Task) -> None:
    """Log why the embedder could not warm up; a memory write reports its own error later."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "The memory embedder could not warm up (%s)",
            type(task.exception()).__name__,
            exc_info=task.exception(),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources at startup, cleanup at shutdown."""
    print("Starting Jarvis Reasoning Engine...")
    try:
        db_client = DatabaseClient()
        app.state.db_client = db_client
        connected = await db_client.check_connection()
        if connected:
            print(" ✅ Database connection successful: Supabase connected.")
        else:
            print("❌ WARNING: Database check returned false. Health endpoint may fail.")
    except ValueError as e:
        print(f"ERROR: Supabase not configured. {e}")
        print("   → Add SUPABASE_URL and SUPABASE_SERVICE_KEY to Jarvis-Engine/.env")
        app.state.db_client = _StubDBClient()
    except Exception as e:
        print(f"ERROR: Failed to connect to Supabase at startup: {e}")
        print("   → Check SUPABASE_URL in .env (e.g. https://xxx.supabase.co) and network access.")
        app.state.db_client = _StubDBClient()

    from app.services.documents.registry import register_default_document_types
    register_default_document_types()

    from app.modules import register_default_modules
    register_default_modules()

    from app.core.config import CHECKPOINT_DB_PATH
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    # SQLite checkpointer, one thread per (user, session). The saver scrubs the
    # non-serializable per-turn objects (user_model, container, progress callback/queue);
    # every turn brings its own.
    checkpoint_stack = AsyncExitStack()
    checkpointer = await checkpoint_stack.enter_async_context(
        open_checkpointer(CHECKPOINT_DB_PATH)
    )
    app.state.jarvis_graph = build_jarvis_graph(checkpointer=checkpointer)
    print(f" ✅ Checkpointer: {CHECKPOINT_DB_PATH}")

    # Detect loaded LM Studio models — NEVER load/unload models (OOM risk on 24GB).
    # Prefers 26B for primary, E4B for fast. If only one Gemma loaded, points both at it.
    # If LM Studio unreachable → force GEMINI_PRIMARY so all calls go to cloud.
    import app.core.config as _cfg

    def _force_gemini_only(reason: str) -> None:
        _cfg.GEMINI_PRIMARY = True
        print(f" ⚠️  {reason} — all LLM calls will use the cloud providers")

    detect_result = _cfg.detect_loaded_models()
    if detect_result.get("error") or detect_result.get("warning"):
        warn = detect_result.get("warning") or detect_result.get("error")
        if not detect_result.get("loaded"):
            _force_gemini_only(warn or "No local models")
        else:
            print(f" ⚠️  {warn}")
    else:
        _cfg.GEMINI_PRIMARY = False
        print(f" ✅ Detected Gemma model(s): {detect_result.get('loaded')}")
        print(f"    PRIMARY → {_cfg.GEMMA_PRIMARY_MODEL}")
        if detect_result.get("single_model"):
            print(f"    FAST    → {_cfg.GEMMA_FAST_MODEL}  (single-model fallback; same as primary)")
        else:
            print(f"    FAST    → {_cfg.GEMMA_FAST_MODEL}")

    # The composition root: repositories, event bus, usage ledger, and the LLM chain
    # route_llm_call uses (metered onto that bus). With Supabase unreachable it runs
    # on in-memory repositories and says so; the legacy app.state.db_client above
    # stays for the routes not yet on the repositories. It lives on app.state only:
    # a request reaches it through the app, a chat turn through its state.
    from app.container import AppContainer
    from app.orchestrator.hooks import get_hooks

    supabase_client = getattr(app.state.db_client, "supabase", None)
    container = AppContainer.build(supabase_client=supabase_client, cfg=_cfg, hooks=get_hooks())
    app.state.container = container
    if supabase_client is not None:
        print(" ✅ Repositories: Supabase")

    # Load the sentence model now, off the event loop, so the first memory a user
    # shares is not kept waiting for it.
    embedder_warm_up = asyncio.create_task(
        asyncio.to_thread(container.embedder.embed, ["warm up"])
    )
    embedder_warm_up.add_done_callback(_log_warm_up_failure)

    # Which language models will answer, in the order they are tried. CLAUDE_PRIMARY
    # (like GEMINI_PRIMARY) makes the local provider report itself unavailable.
    live = [p.name for p in container.llm.providers if p.is_available()]
    if _cfg.CLAUDE_PRIMARY and "claude" not in live:
        print("   → no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN set; Claude cannot answer")
    if "claude" in live:
        print(f"    CLAUDE  → {_cfg.CLAUDE_MODEL}")
    print(f" ✅ LLM providers live, in order: {' → '.join(live) or 'none'}")
    print(
        f"    CLAUDE_PRIMARY {'on' if _cfg.CLAUDE_PRIMARY else 'off'}, "
        f"GEMINI_PRIMARY {'on' if _cfg.GEMINI_PRIMARY else 'off'}"
    )

    # The checkpoint file is the orchestrator's own, so its daily retention sweep joins
    # the container's sweeps here, where the checkpointer is open.
    from app.orchestrator.checkpoint import CheckpointRetentionSweep

    container.sweeps.append(CheckpointRetentionSweep(checkpointer))

    # Anticipation: one in-process loop runs every subsystem's sweeps, then the
    # proactive triggers. The first tick comes a full tick after startup.
    # JARVIS_ANTICIPATION=false keeps it off.
    from app.services.anticipation.scheduler import AnticipationScheduler

    anticipation_scheduler = AnticipationScheduler(
        container.anticipation,
        container.repos.profiles,
        container.sweeps,
        tick_s=_cfg.ANTICIPATION_TICK_SECONDS,
    )
    app.state.anticipation_scheduler = anticipation_scheduler
    if _cfg.ANTICIPATION_ENABLED:
        anticipation_scheduler.start()
        print(
            f" ✅ Anticipation scheduler started: a tick every {_cfg.ANTICIPATION_TICK_SECONDS}s"
        )
    else:
        print("    Anticipation scheduler off (JARVIS_ANTICIPATION=false)")

    try:
        yield
    finally:
        print("Shutting down Jarvis Reasoning Engine.")
        embedder_warm_up.cancel()  # a no-op once it is done
        # First: a tick in progress may still be recording model calls.
        await anticipation_scheduler.stop()
        # Then the container's own background work (PEARL runs after outcomes).
        await container.shutdown()
        # Bounded: in-flight usage records get a moment to land, never a hang.
        await container.usage_ledger.shutdown()
        await checkpoint_stack.aclose()


app = FastAPI(title="Jarvis Reasoning Engine", lifespan=lifespan, redirect_slashes=False)

# CORS: allow jarvis-demo (Next.js) to call the API from a different origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The browser hides response headers from page scripts unless they are listed:
    # the frontend reads the request id to quote it in a bug report.
    expose_headers=[REQUEST_ID_HEADER],
)
app.add_middleware(RequestIdMiddleware)
register_error_handlers(app)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
def root():
    """Root endpoint."""
    return {"message": "Hello, Jarvis this side."}


@app.get("/health")
async def health(request: Request):
    """
    Health check endpoint. Uses pre-initialized database connection from startup.
    Returns 200 if healthy; 503 `persistence_unavailable` if the database is unreachable.
    """
    db_client: DatabaseClient = request.app.state.db_client
    unreachable = PersistenceError("Database is unreachable.", details={"database": "disconnected"})
    try:
        connected = await db_client.check_connection()
    except Exception as exc:
        raise unreachable from exc
    if not connected:
        raise unreachable
    return {"status": "healthy", "database": "connected"}
