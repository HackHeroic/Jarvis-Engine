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

    # Warmup LM Studio models using explicit load endpoint (avoids model-swap on chat/completions)
    from app.core.config import LOCAL_LLM_MODEL, SLM_ROUTER_MODEL, LM_STUDIO_NATIVE_URL
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Fetch LM Studio's downloaded model list once for ID resolution
            _lms_models: list[dict] = []
            _list_resp = await client.get(f"{LM_STUDIO_NATIVE_URL}/api/v1/models")
            if _list_resp.status_code == 200:
                _lms_models = _list_resp.json().get("data", [])

            # Fetch currently loaded models (OpenAI /v1/models only returns in-memory models)
            _loaded_ids: set[str] = set()
            _loaded_resp = await client.get(f"{LM_STUDIO_NATIVE_URL}/v1/models")
            if _loaded_resp.status_code == 200:
                _loaded_ids = {m["id"] for m in _loaded_resp.json().get("data", [])}

            async def load_model(model_id: str, label: str) -> None:
                bare_id = model_id.removeprefix("openai/")

                # Check if already loaded (exact match or fuzzy match against loaded set)
                search_key = bare_id.split("/")[-1].lower()
                already_loaded = next(
                    (mid for mid in _loaded_ids if search_key in mid.lower()), None
                )
                if already_loaded:
                    print(f" ✅ {label} model already loaded ({already_loaded})")
                    return

                resp = await client.post(
                    f"{LM_STUDIO_NATIVE_URL}/api/v1/models/load",
                    json={"model": bare_id},
                )
                if resp.status_code in (200, 201):
                    print(f" ✅ {label} model warmed up")
                    return
                if resp.status_code == 404 and _lms_models:
                    # Fuzzy-match against downloaded models (case-insensitive, ignore publisher prefix)
                    match = next(
                        (m["id"] for m in _lms_models if search_key in m["id"].lower()),
                        None,
                    )
                    if match:
                        retry = await client.post(
                            f"{LM_STUDIO_NATIVE_URL}/api/v1/models/load",
                            json={"model": match},
                        )
                        if retry.status_code in (200, 201):
                            print(f" ✅ {label} model warmed up (resolved: {match})")
                            return
                        print(f" ⚠️  {label} model load failed after resolve ({match}): {retry.status_code}")
                        return
                    print(f" ⚠️  {label}: no downloaded model matches '{bare_id}' — check LM Studio")
                    return
                print(f" ⚠️  {label} model load returned {resp.status_code}: {resp.text[:120]}")

            await load_model(SLM_ROUTER_MODEL, "4B")
            await load_model(LOCAL_LLM_MODEL, "27B")
    except Exception as e:
        print(f" ⚠️  Model warmup failed (non-fatal): {e}")

    yield
    print("Shutting down Jarvis Reasoning Engine.")


app = FastAPI(title="Jarvis Reasoning Engine", lifespan=lifespan, redirect_slashes=False)

# CORS: allow jarvis-demo (Next.js) to call the API from a different origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
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
    - Local Qwen: prompts without cloud keywords (e.g. study schedule)
    - Cloud Gemini: prompts with keywords like "latest news", "current events"
    """
    print(f"📥 Received prompt: {request.prompt}")

    system_prompt = "You are Jarvis, a highly efficient and concise AI assistant."

    response = await hybrid_route_query(
        user_prompt=request.prompt,
        system_prompt=system_prompt,
    )

    return {"status": "success", "response": response}

