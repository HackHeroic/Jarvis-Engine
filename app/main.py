"""Entry point: initializes FastAPI and LiteLLM Router."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

# Stub used when Supabase fails at startup so the app can still run
class _StubDBClient:
    supabase = None  # type: ignore[assignment]

    async def check_connection(self) -> bool:
        return False
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api.v1.router import api_router
from app.db.supabase_py import DatabaseClient
from app.models.brain.litellm_conf import hybrid_route_query


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

    from app.services.intent_registry import register_default_intents
    register_default_intents()

    from app.services.documents.registry import register_default_document_types
    register_default_document_types()

    from app.services.memory.pearl import register_default_patterns
    register_default_patterns()

    from app.modules import register_default_modules
    register_default_modules()

    from app.orchestrator.graph import build_jarvis_graph
    # No checkpointer — UserModel is not msgpack-serializable.
    # Checkpoint/resume requires excluding UserModel from persisted state.
    app.state.jarvis_graph = build_jarvis_graph()

    # Detect loaded LM Studio models — NEVER load/unload models (OOM risk on 24GB).
    # Maps whatever is loaded to LOCAL_LLM_MODEL and SLM_ROUTER_MODEL dynamically.
    # If nothing is loaded or LM Studio is off → force GEMINI_PRIMARY so all calls go to cloud.
    import app.core.config as _cfg
    from app.core.config import LM_STUDIO_NATIVE_URL

    def _force_gemini_only(reason: str) -> None:
        """No local models available — route everything to Gemini."""
        _cfg.GEMINI_PRIMARY = True
        print(f" ⚠️  {reason} — all LLM calls will use Gemini cloud")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            _loaded_resp = await client.get(f"{LM_STUDIO_NATIVE_URL}/v1/models")
            if _loaded_resp.status_code == 200:
                _loaded_models = _loaded_resp.json().get("data", [])
                _loaded_ids = [m["id"] for m in _loaded_models]
                if _loaded_ids:
                    # Use the first loaded model for all local tasks (single-model setup)
                    _primary = f"openai/{_loaded_ids[0]}"
                    _cfg.LOCAL_LLM_MODEL = _primary
                    _cfg.GEMMA_PRIMARY_MODEL = _primary
                    # If multiple models loaded, use second as fast/SLM; else same model
                    _fast = f"openai/{_loaded_ids[1]}" if len(_loaded_ids) > 1 else _primary
                    _cfg.SLM_ROUTER_MODEL = _fast
                    _cfg.GEMMA_FAST_MODEL = _fast
                    _cfg.GEMINI_PRIMARY = False  # local models available → local-first
                    print(f" ✅ Detected loaded model(s): {_loaded_ids}")
                    print(f"    PRIMARY → {_cfg.LOCAL_LLM_MODEL}")
                    if _fast != _primary:
                        print(f"    FAST/SLM → {_cfg.SLM_ROUTER_MODEL}")
                else:
                    _force_gemini_only("No models loaded in LM Studio")
            else:
                _force_gemini_only(f"LM Studio /v1/models returned {_loaded_resp.status_code}")
    except httpx.ConnectError:
        _force_gemini_only("LM Studio not running")
    except Exception as e:
        _force_gemini_only(f"Model detection failed: {e}")

    yield
    print("Shutting down Jarvis Reasoning Engine.")


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


class ChatRequest(BaseModel):
    """Request body for test-chat endpoint."""

    prompt: str


@app.post("/test-chat")
async def test_chat(request: ChatRequest):
    """
    Temporary endpoint to test the LiteLLM Hybrid Router.
    - Local Gemma: prompts without cloud keywords (e.g. study schedule)
    - Cloud Gemini: prompts with keywords like "latest news", "current events"
    """
    print(f"📥 Received prompt: {request.prompt}")

    system_prompt = "You are Jarvis, a highly efficient and concise AI assistant."

    response = await hybrid_route_query(
        user_prompt=request.prompt,
        system_prompt=system_prompt,
    )

    return {"status": "success", "response": response}

