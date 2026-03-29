"""Test that the memory system is properly wired into the live request path.

Verifies WIRING (imports, initialization, calling) rather than
memory system logic (tested in test_memory_*.py).

NOTE: Several modules (main, control_policy, chat) transitively import
OR-Tools which crashes on this platform.  Those tests read the source
files directly instead of importing the modules.
"""

import ast
import inspect
from pathlib import Path

# Root of the engine package
_APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _read_source(rel_path: str) -> str:
    """Read a source file relative to the app/ directory."""
    return (_APP_ROOT / rel_path).read_text()


def test_memory_store_in_main():
    """MemoryStore should be initialized in app.state during lifespan."""
    source = _read_source("main.py")
    assert "MemoryStore" in source, "main.py must import MemoryStore"
    assert "app.state.memory_store" in source, "main.py must set app.state.memory_store"


def test_chat_response_has_memories_field():
    """ChatResponse schema must have an optional memories field."""
    from app.schemas.context import ChatResponse
    fields = ChatResponse.model_fields
    assert "memories" in fields, "ChatResponse must have a 'memories' field"
    assert fields["memories"].default is None, "memories must default to None"


def test_build_memory_context_exists():
    """build_memory_context must be importable from retriever."""
    from app.services.memory.retriever import build_memory_context
    assert callable(build_memory_context)


def test_control_policy_accepts_memory_params():
    """execute_agentic_flow must accept memory_context and memory_store params."""
    source = _read_source("services/analytical/control_policy.py")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute_agentic_flow":
            params = [arg.arg for arg in node.args.args]
            assert "memory_context" in params, "execute_agentic_flow must accept memory_context"
            assert "memory_store" in params, "execute_agentic_flow must accept memory_store"
            return
    raise AssertionError("execute_agentic_flow not found in control_policy.py")


def test_run_plan_day_flow_accepts_memory_store():
    """_run_plan_day_flow must accept memory_store param."""
    source = _read_source("services/analytical/control_policy.py")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run_plan_day_flow":
            params = [arg.arg for arg in node.args.args]
            assert "memory_store" in params, "_run_plan_day_flow must accept memory_store"
            return
    raise AssertionError("_run_plan_day_flow not found in control_policy.py")


def test_chat_endpoint_references_memory():
    """chat.py must reference memory_store and build_memory_context."""
    source = _read_source("api/v1/endpoints/chat.py")
    assert "memory_store" in source, "chat.py must reference memory_store"
    assert "build_memory_context" in source, "chat.py must call build_memory_context"
    assert "safe_extract_memories" in source, "chat.py must call safe_extract_memories"
