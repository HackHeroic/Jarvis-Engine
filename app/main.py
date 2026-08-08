"""Entry point: initializes FastAPI and LiteLLM Router."""

from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

# Stub used when Supabase fails at startup so the app can still run
class _StubDBClient:
    supabase = None  # type: ignore[assignment]

    async def check_connection(self) -> bool:
        return False
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.db.supabase_py import DatabaseClient


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

    # Initialize draft store (Supabase-backed, no TTL — persistence across restarts)
    from app.services.draft_store import DraftStore
    app.state.draft_store = DraftStore(supabase_client=getattr(app.state.db_client, 'supabase', None))

    from app.services.memory.store import MemoryStore
    app.state.memory_store = MemoryStore(
        supabase_client=getattr(app.state.db_client, 'supabase', None)
    )

    # Same singletons, reachable from code with no request scope — notably
    # _load_context rebuilding the UserModel facade on a checkpoint-resumed turn.
    from app.core.runtime import set_shared_clients
    set_shared_clients(db=app.state.db_client, memory_store=app.state.memory_store)

    from app.services.intent_registry import register_default_intents
    register_default_intents()

    from app.services.documents.registry import register_default_document_types
    register_default_document_types()

    from app.services.memory.pearl import register_default_patterns
    register_default_patterns()

    from app.modules import register_default_modules
    register_default_modules()

    from app.core.config import CHECKPOINT_DB_PATH
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph

    # SQLite checkpointer, one thread per (user, session). The saver scrubs the
    # non-serializable per-turn objects (user_model, progress callback/queue);
    # _load_context rebuilds the facade from the checkpointed user_id.
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
        print(f" ⚠️  {reason} — all LLM calls will use Gemini cloud")

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

    try:
        yield
    finally:
        print("Shutting down Jarvis Reasoning Engine.")
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
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
def root():
    """Root endpoint."""
    return {"message": "Hello, Jarvis this side."}


@app.get("/health")
async def health(request: Request):
    """
    Health check endpoint. Uses pre-initialized database connection from startup.
    Returns 200 if healthy, 500 if database connection failed at startup.
    """
    db_client: DatabaseClient = request.app.state.db_client
    try:
        connected = await db_client.check_connection()
        if connected:
            return {"status": "healthy", "database": "connected"}
        raise HTTPException(status_code=500, detail="Database check returned false")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": "unhealthy", "database": "disconnected", "error": str(e)},
        )
