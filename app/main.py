"""Entry point: initializes FastAPI and LiteLLM Router."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app.db.supabase_py import DatabaseClient
from app.models.brain.litellm_conf import hybrid_route_query


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources at startup, cleanup at shutdown."""
    print("Starting Jarvis Reasoning Engine...")
    db_client = DatabaseClient()
    app.state.db_client = db_client
    try:
        connected = await db_client.check_connection()
        if connected:
            print(" ✅ Database connection successful: Supabase connected.")
        else:
            print("❌ WARNING: Database check returned false. Health endpoint may fail.")
    except Exception as e:
        print(f"ERROR: Failed to connect to Supabase at startup: {e}")
    yield
    print("Shutting down Jarvis Reasoning Engine.")


app = FastAPI(title="Jarvis Reasoning Engine", lifespan=lifespan)


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

