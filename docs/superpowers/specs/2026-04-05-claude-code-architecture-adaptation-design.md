# Claude Code Architecture Adaptation — Design Spec

> **SUPERSEDED (2026-08-08):** implemented instead as the smaller ModuleStep framework — see 2026-04-13-module-step-framework-design.md. Kept for historical reference.

**Date:** 2026-04-05
**Author:** Madhav + Claude
**Status:** Draft — awaiting review
**Source:** Claude Code open-source (`claude-code-src-code-main`), verified against source

---

## Executive Summary

This spec defines how to adapt Claude Code's production-grade orchestration architecture into Jarvis Engine's Python/FastAPI backend. We take the **chassis** (tool protocol, hook system, streaming executor, query loop) from Claude Code and mount Jarvis's **domain engine** (OR-Tools scheduling, PEARL behavioral learning, SM-2 memory, Socratic chunking) on it.

### What This Gives Us

1. **Every Jarvis service becomes a Tool** — composable, testable, with concurrency declarations
2. **Cross-cutting concerns decouple from tools** — SSE progress, memory extraction, cache invalidation, PEARL detection all become hooks
3. **Parallel tool execution** — safe tools run concurrently (4-5x faster PLAN_DAY with MoE models)
4. **Hybrid query loop** — deterministic pipelines for known intents + free-form LLM tool calling for new intents
5. **Context compaction** — multi-layer strategy prevents context overflow in long conversations
6. **Agent system foundation** — parent-child forking and coordinator/swarm mode (Phases 3-4)

### What Does NOT Change

- 3-tier memory architecture (working/recall/archival) with SM-2 decay
- PEARL behavioral pattern detection
- OR-Tools CP-SAT deterministic scheduling
- Draft negotiation UX (accept/edit/reject/chat-more)
- Document intelligence pipeline
- Anti-guilt psychology (WOOP, CLT, TMT)
- Database schema (all Supabase tables stay)
- Registry pattern (enhanced, not replaced)
- LLM routing rules (local-first)
- All security rules

### Language Decision: Python

The adaptation is pure Python/asyncio. Claude Code's power comes from its architecture, not TypeScript. Our entire domain stack (OR-Tools, MLX-Embed, Docling, ChromaDB, LiteLLM) is Python-native. The 4 TS-specific patterns (AbortController hierarchy, `yield*` composition, compile-time feature flags, Bun native I/O) all have straightforward Python equivalents.

### Phased Delivery

| Phase | What | Depends On |
|-------|------|------------|
| **Phase 1** | Tool Protocol + Hook System + Streaming Executor | Nothing |
| **Phase 2** | Query Loop Engine + Context Compaction | Phase 1 |
| **Phase 3** | Agent System (parent-child forking) | Phase 2 |
| **Phase 4** | Coordinator/Swarm Mode | Phase 3 |
| **Phase 5** | Advanced (speculative execution, auto-mode classifier, denial tracking) | Phase 2 |

---

## Phase 1A: Tool Protocol

### Design Principles

Every Jarvis service becomes a `Tool` — a self-describing, composable unit with:
- Pydantic schema for input validation
- Concurrency safety declaration (per-invocation, input-dependent)
- Permission checking
- Progress streaming during execution
- Result size budgeting

### The Protocol

```python
from typing import Protocol, Literal, Callable, Awaitable, Any
from pydantic import BaseModel
from dataclasses import dataclass, field

class Tool(Protocol):
    # ── Identity ──
    name: str
    aliases: list[str]                         # Backward compat when renaming
    description: str
    input_schema: type[BaseModel]              # Pydantic model for validation
    max_result_size_chars: int                  # Auto-truncate large results (default 100_000)
    uses_llm: bool                             # Does this tool call the LLM?

    # ── Safety Classification ──
    def is_concurrency_safe(self, input: BaseModel) -> bool:
        """Can run in parallel? Takes input so read vs write can differ.
        Conservative default: False."""
        ...

    def is_read_only(self, input: BaseModel) -> bool:
        """Does this tool only read data?"""
        ...

    def is_destructive(self, input: BaseModel) -> bool:
        """Is this operation irreversible? (delete, overwrite, send)
        Used by permission system and auto-mode classifier."""
        ...

    def interrupt_behavior(self) -> Literal["cancel", "block"]:
        """What happens when user sends new message mid-execution?
        'cancel' = abort immediately. 'block' = finish, then process new message.
        Default: 'block' (let tool finish)."""
        ...

    # ── Validation & Permissions ──
    async def validate_input(
        self, input: BaseModel, ctx: "ToolContext"
    ) -> str | None:
        """Runtime context-aware validation beyond schema.
        Return error string or None if valid."""
        ...

    async def check_permissions(
        self, input: BaseModel, ctx: "ToolContext"
    ) -> "PermissionResult":
        """Per-tool permission override AFTER general hook-based permissions."""
        ...

    def prepare_permission_matcher(
        self, input: BaseModel
    ) -> Callable[[str], bool] | None:
        """Returns closure for matching hook `if` patterns.
        E.g., 'ScheduleTasksTool(horizon > 10000)'. None = no matching."""
        ...

    # ── Lifecycle ──
    def is_enabled(self) -> bool:
        """Is this tool currently active? Default: True.
        Allows disabling tools without unregistering (e.g., feature flags,
        stub tools like RecordCompletionTool before DKT is implemented)."""
        ...

    def search_hint(self) -> str | None:
        """Keyword phrase for deferred tool discovery by LLM.
        Enables future tool-search for free-form CHAT intent path."""
        ...

    # ── Execution ──
    async def call(
        self,
        input: BaseModel,
        ctx: "ToolContext",
        on_progress: Callable[["ToolProgress"], None] | None = None,
    ) -> "ToolResult":
        """Execute the tool. May yield progress events during execution."""
        ...
```

### ToolContext

Everything a tool needs without global imports:

```python
@dataclass
class ToolContext:
    user_id: str
    session_id: str
    db: Any                                    # SupabaseClient
    chroma: Any                                # ChromaClient
    llm: Any                                   # LLMRouter (hybrid_route_query)
    memory_store: Any | None                   # Can be None (graceful degradation)
    draft_store: Any                           # DraftStore
    abort_signal: asyncio.Event                # Cooperative cancellation
    sibling_abort: asyncio.Event | None        # Sibling abort (set by executor, tools check both)
    permissions: "PermissionContext"
    messages: list["Message"]                  # Conversation history (LLM-ready format)
    conversation_history: list[dict] | None    # Raw chat history for brain dump extraction
    query_tracking: "QueryTracking | None"     # Chain ID + depth + source
    progress_callback: Callable[[str, dict], Awaitable[None]] | None  # SSE progress emission

    # State access (getter/setter, not direct mutation)
    get_app_state: Callable[[], "AppState"]
    set_app_state: Callable[[Callable[["AppState"], "AppState"]], None]

    # Agent spawning (Phase 3)
    spawn_agent: Callable[..., Awaitable["AgentResult"]] | None = None
```

### ToolResult

```python
@dataclass
class ToolResult:
    data: Any                                  # The actual output
    is_error: bool = False
    new_messages: list["Message"] | None = None
    context_modifier: Callable[["ToolContext"], "ToolContext"] | None = None  # Non-concurrent only
    metadata: dict = field(default_factory=dict)
```

### ToolProgress

```python
@dataclass
class ToolProgress:
    tool_use_id: str
    data: dict                                 # Tool-specific progress data
```

### PermissionResult

```python
@dataclass
class PermissionResult:
    allowed: bool = True
    reason: str | None = None
    updated_input: dict | None = None          # Mutate input if needed
```

### Tool Registry

Evolves the existing `BaseRegistry` pattern with LLM schema generation:

```python
class ToolRegistry:
    """Registry of all available tools. Extends Jarvis's BaseRegistry."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}     # alias → canonical name

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        for alias in getattr(tool, 'aliases', []):
            self._aliases[alias] = tool.name

    def get(self, name: str) -> Tool | None:
        canonical = self._aliases.get(name, name)
        return self._tools.get(canonical)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def list_for_llm(self) -> list[dict]:
        """Generate OpenAI-format tool schemas for LLM API calls."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema.model_json_schema(),
                }
            }
            for tool in self._tools.values()
        ]

    def partition_by_concurrency(
        self, names: list[str], inputs: dict[str, BaseModel]
    ) -> tuple[list[str], list[str]]:
        """Split into (safe, unsafe) given actual inputs."""
        safe, unsafe = [], []
        for name in names:
            tool = self.get(name)
            if tool and tool.is_concurrency_safe(inputs[name]):
                safe.append(name)
            else:
                unsafe.append(name)
        return safe, unsafe
```

### buildTool() Factory

Fail-closed defaults matching Claude Code:

```python
def build_tool(**kwargs) -> Tool:
    """Create a tool with safe defaults."""
    defaults = {
        "aliases": [],
        "max_result_size_chars": 100_000,
        "uses_llm": False,
        "is_concurrency_safe": lambda self, input: False,   # Conservative
        "is_read_only": lambda self, input: False,
        "is_destructive": lambda self, input: False,
        "interrupt_behavior": lambda self: "block",          # Let tool finish
        "is_enabled": lambda self: True,                     # Active by default
        "search_hint": lambda self: None,
        "validate_input": lambda self, input, ctx: None,
        "check_permissions": lambda self, input, ctx: PermissionResult(),
        "prepare_permission_matcher": lambda self, input: None,
    }
    merged = {**defaults, **kwargs}
    # Return a concrete Tool class instance
    return _ToolImpl(**merged)
```

### Complete Tool Inventory (22 Tools)

| Tool | uses_llm | is_destructive | interrupt_behavior | is_concurrency_safe (dense 27B) | is_concurrency_safe (MoE 4B-active) |
|------|----------|----------------|--------------------|---------------------------------|--------------------------------------|
| `BrainDumpExtractTool` | Yes | No | cancel | Unsafe | **Safe** |
| `TranslateHabitsTool` | Yes | No | cancel | Unsafe | **Safe** |
| `ExpandHorizonTool` | No | No | cancel | Safe | Safe |
| `DecomposeGoalTool` | Yes | No | cancel | Unsafe | **Safe** |
| `FetchConstraintsTool` | No | No | cancel | Safe | Safe |
| `FetchPendingTasksTool` | No | No | cancel | Safe | Safe |
| `ScheduleTasksTool` | No | No | **block** | Safe | Safe |
| `PersistScheduleTool` | No | **Yes** | block | **Unsafe** | **Unsafe** |
| `IngestDocumentTool` | No | No | cancel | **Unsafe** | **Unsafe** |
| `ExtractCalendarTool` | Yes | No | cancel | Unsafe | **Safe** |
| `LinkMaterialsTool` | No | No | cancel | **Unsafe** | **Unsafe** |
| `StoreConstraintTool` | No | No | cancel | **Unsafe** | **Unsafe** |
| `DeleteConstraintTool` | No | **Yes** | cancel | **Unsafe** | **Unsafe** |
| `EditTaskTool` | No | **Yes** | cancel | **Unsafe** | **Unsafe** |
| `RearrangeTasksTool` | No | **Yes** | cancel | **Unsafe** | **Unsafe** |
| `WorkspaceBuilderTool` | No | No | cancel | Safe | Safe |
| `RecordCompletionTool` | No | No | cancel | **Unsafe** | **Unsafe** |
| `ExtractMemoriesTool` | Yes | No | cancel | Unsafe | **Safe** |
| `DraftAcceptTool` | No | **Yes** | block | **Unsafe** | **Unsafe** |
| `DraftRejectTool` | No | No | cancel | **Unsafe** | **Unsafe** |
| `DirectQATool` | Yes | No | cancel | Unsafe | **Safe** |
| `SynthesizeResponseTool` | Yes | No | cancel | Unsafe | **Safe** |

**Model-aware concurrency config:**

```python
# In config.py or .env:
LLM_CONCURRENCY_SAFE = True   # True for MoE models (Gemma 4), False for dense (Qwen-27B)
```

---

## Phase 1B: Hook System

### Design Principles

Hooks are middleware that fire at lifecycle points — decoupling cross-cutting concerns (SSE, memory, caching, PEARL) from tools. Two types:
- **Function hooks** (internal, registered in code) — optimized fast-path
- **Command hooks** (user-configurable, in settings.json) — shell commands or HTTP

### Hook Events (26)

```python
class HookEvent(str, Enum):
    # Tool lifecycle
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_USE_FAILURE = "post_tool_use_failure"

    # Session lifecycle
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_MESSAGE = "user_message"
    SETUP = "setup"                            # One-time init (registry warm-up, model loading)

    # Agent lifecycle (Phase 3+)
    AGENT_SPAWN = "agent_spawn"
    AGENT_STOP = "agent_stop"

    # Task lifecycle
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    TASK_SKIPPED = "task_skipped"
    TASKS_PERSISTED = "tasks_persisted"

    # Memory lifecycle
    MEMORY_UPDATED = "memory_updated"
    PATTERN_DETECTED = "pattern_detected"

    # Scheduling lifecycle
    CONSTRAINT_CHANGED = "constraint_changed"
    CACHE_INVALIDATED = "cache_invalidated"
    SCHEDULE_CHANGED = "schedule_changed"

    # Draft lifecycle
    DRAFT_CREATED = "draft_created"
    DRAFT_RESOLVED = "draft_resolved"

    # Ingestion lifecycle
    DOCUMENT_INGESTED = "document_ingested"

    # Pipeline lifecycle
    PIPELINE_STOP = "pipeline_stop"            # Cleanup when pipeline completes normally
    PIPELINE_STOP_FAILURE = "pipeline_stop_failure"  # Cleanup when pipeline fails mid-execution

    # Permission lifecycle
    PERMISSION_DENIED = "permission_denied"    # Tool permission denied (feeds denial tracker)

    # Compaction lifecycle (Phase 2)
    PRE_COMPACT = "pre_compact"                # Before context compaction runs
    POST_COMPACT = "post_compact"              # After compaction completes (with token savings)
```

### Hook Types

```python
@dataclass
class FunctionHook:
    """Internal hook registered in code. Uses optimized fast-path."""
    name: str
    event: HookEvent
    callback: Callable[["HookInput"], Awaitable["HookResponse"]]
    matcher: "HookMatcher | None" = None
    internal: bool = True
    timeout_ms: int = 5000

@dataclass
class CommandHook:
    """User-configurable hook from settings.json."""
    name: str
    event: HookEvent
    command: str                               # Shell command
    matcher: "HookMatcher | None" = None
    timeout_s: int = 30
    is_async: bool = False                     # Fire-and-forget
    shell: str = "/bin/zsh"
```

### Hook Matching

```python
@dataclass
class HookMatcher:
    tool_name: str | None = None
    tool_input_pattern: str | None = None      # Regex on serialized input

    def matches(self, event: HookEvent, input: "HookInput") -> bool:
        if self.tool_name and input.tool_name != self.tool_name:
            return False
        if self.tool_input_pattern and input.tool_input:
            serialized = json.dumps(input.tool_input, default=str)
            if not re.search(self.tool_input_pattern, serialized):
                return False
        return True
```

### Hook Input & Response

```python
@dataclass
class HookInput:
    event: HookEvent
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_result: ToolResult | None = None
    user_id: str = ""
    session_id: str = ""
    metadata: dict = field(default_factory=dict)

@dataclass
class HookResponse:
    # Permission (precedence: deny > ask > allow)
    decision: Literal["allow", "deny", "ask"] | None = None
    reason: str | None = None

    # Input mutation (last-write-wins across hooks)
    updated_input: dict | None = None

    # Flow control
    prevent_continuation: bool = False
    suppress_output: bool = False

    # Message injection
    inject_messages: list | None = None
    additional_context: str | None = None

    # Async
    is_async: bool = False
    async_timeout_s: int = 30
```

### Permission Precedence (Critical Invariant)

A hook's "allow" decision does **NOT** bypass settings-based deny/ask rules. This matches Claude Code's `resolveHookPermissionDecision()` semantics:

```
Permission resolution order:
1. Hook returns "deny"  → DENIED (highest precedence, immediate)
2. Hook returns "allow" → still check settings-based rules:
   a. Settings deny rule matches → DENIED (overrides hook allow)
   b. Settings ask rule matches  → ASK user (overrides hook allow)
   c. No settings rule matches   → ALLOWED
3. Hook returns "ask"   → prompt user (even if settings would allow)
4. No hook decision      → normal permission flow (settings rules only)

Invariant: A permissive hook can NEVER bypass a restrictive settings rule.
This prevents security holes where a custom hook accidentally allows
destructive operations that settings explicitly block.
```

### Hook Registry & Execution

```python
class HookRegistry:
    def __init__(self):
        self._function_hooks: dict[HookEvent, list[FunctionHook]] = defaultdict(list)
        self._command_hooks: dict[HookEvent, list[CommandHook]] = defaultdict(list)
        self._denial_tracker = DenialTracker()

    def register_function(self, hook: FunctionHook) -> None: ...
    def register_command(self, hook: CommandHook) -> None: ...
    def unregister(self, name: str) -> None: ...

    async def execute(self, event: HookEvent, input: HookInput) -> HookResponse:
        """Execute matching hooks with Claude Code semantics:
        - Fast-path for all-internal hooks (skip generator overhead)
        - Parallel execution for function hooks
        - Sequential for command hooks
        - Permission precedence: deny > ask > allow
        - updated_input: last-write-wins
        - Errors are non-blocking (hook crash doesn't kill tool)
        """
        ...

    def _merge_responses(self, current: HookResponse, new: HookResponse) -> HookResponse:
        """Merge with precedence rules."""
        ...
```

**Command hook execution:**
- JSON input on stdin, JSON output on stdout
- Exit code 0 = success, 1 = non-blocking error, 2 = BLOCK (tool denied)
- Environment variables injected: `JARVIS_PROJECT_DIR`, `JARVIS_USER_ID`, `JARVIS_TOOL_NAME`, `JARVIS_HOOK_EVENT`

**Denial tracking circuit breaker:**

```python
@dataclass
class DenialTracker:
    consecutive_denials: int = 0
    total_denials: int = 0

    def record_denial(self): ...
    def record_approval(self): ...

    def should_fallback_to_prompt(self) -> bool:
        return self.consecutive_denials >= 3 or self.total_denials >= 20
```

### Internal Hook Registration (Complete Mapping)

Every Jarvis side effect maps to a hook:

| Hook Name | Event | Matcher | Replaces | Sync/Async |
|-----------|-------|---------|----------|------------|
| `SSEProgressHook` | PRE + POST_TOOL_USE | All tools | progress_callback calls | Sync |
| `MemoryExtractionHook` | POST_TOOL_USE | SynthesizeResponseTool | asyncio.create_task(safe_extract_memories) | Async (fire-and-forget) |
| `PEARLDetectionHook` | MEMORY_UPDATED | All memory writes | detect_patterns() chain | Async (fire-and-forget) |
| `HabitCacheInvalidationHook` | CONSTRAINT_CHANGED | StoreConstraint/DeleteConstraint | invalidate_habit_cache() | Sync |
| `DecompositionCacheCheckHook` | PRE_TOOL_USE | DecomposeGoalTool | _decompose_cache check | Sync |
| `DecompositionCacheStoreHook` | POST_TOOL_USE | DecomposeGoalTool | _decompose_cache write | Sync |
| `DraftCreationHook` | POST_TOOL_USE | ScheduleTasksTool | DraftStore.create_draft() | Sync |
| `ChatHistoryHook` | USER_MESSAGE + POST_TOOL_USE(Synthesize) | Conversation | save_user/assistant_message | Sync |
| `DeadlinePersistHook` | POST_TOOL_USE | BrainDumpExtractTool | user_plan_updates INSERT | Sync (best-effort) |
| `SearchSpawnHook` | POST_TOOL_USE | BrainDumpExtractTool | asyncio.create_task(run_deep_research) | Async |
| `HabitStagingHook` | POST_TOOL_USE | BrainDumpExtractTool | _extract_and_stage_inline_habits | Sync |
| `SummaryCaptureHook` | PRE_TOOL_USE | SynthesizeResponseTool | _summary_capture ContextVar | Sync |
| `TaskMaterialLinkHook` | DOCUMENT_INGESTED | All ingestion | link_document_to_tasks | Sync |
| `DocumentDeadlineHook` | DOCUMENT_INGESTED | All ingestion | deadline parsing from docs | Sync (best-effort) |
| `IngestionMetadataHook` | DOCUMENT_INGESTED | All ingestion | ingested_documents UPSERT | Sync (best-effort) |

### User-Configurable Hooks (settings.json)

```json
{
  "hooks": [
    {
      "event": "post_tool_use",
      "matcher": { "tool_name": "ScheduleTasksTool" },
      "command": "python3 notify_slack.py",
      "timeout": 10
    },
    {
      "event": "task_completed",
      "command": "curl -X POST https://my-webhook.com/task-done",
      "async": true
    }
  ]
}
```

---

## Phase 1C: Streaming Tool Executor

### Design Principles

Manages concurrent tool execution with safety constraints. Adapted from Claude Code's `StreamingToolExecutor.ts`.

### Concurrency Rules

1. **No tools executing** → tool CAN start
2. **Tool is safe AND all executing are safe AND under max_concurrent** → CAN start
3. **Otherwise** → WAIT

Consecutive safe tools batch together. `[safe, safe, UNSAFE, safe]` = batch(S,S) → U alone → S alone. Last safe does NOT run with first two.

### Key Data Structures

```python
class ToolStatus(str, Enum):
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    YIELDED = "yielded"

@dataclass
class TrackedTool:
    id: str
    tool: Tool
    input: BaseModel
    status: ToolStatus = ToolStatus.QUEUED
    is_concurrency_safe: bool = False
    task: asyncio.Task | None = None
    result: ToolResult | None = None
    pending_progress: list[dict] = field(default_factory=list)
    context_modifiers: list[Callable] = field(default_factory=list)
    error: str | None = None

@dataclass
class ExecutorResult:
    tool_id: str
    tool_name: str
    result: ToolResult | None = None
    progress: dict | None = None
    is_synthetic_error: bool = False
    new_context: ToolContext | None = None
```

### StreamingToolExecutor

```python
class StreamingToolExecutor:
    def __init__(self, ctx: ToolContext, hook_registry: HookRegistry,
                 max_concurrent: int | None = None): ...

    def add_tool(self, tool: Tool, input: BaseModel, tool_use_id: str) -> None:
        """Add tool to queue. Triggers processQueue()."""

    async def get_results(self) -> AsyncGenerator[ExecutorResult, None]:
        """Yield results in tool-addition order.
        Progress events bypass ordering (yield immediately).
        Non-concurrent executing tool blocks yielding."""

    def cancel_all(self) -> None:
        """Cancel all running tools (user interrupt)."""

    def discard(self) -> None:
        """Discard all results (streaming fallback)."""
```

### Abort Hierarchy (Two-Level, adapted from Claude Code's AbortController)

Python lacks `AbortController` hierarchies. We use two `asyncio.Event`s:

```
Parent abort (ToolContext.abort_signal)     ← Set by: user interrupt, session end
    │
    └─ Sibling abort (ToolContext.sibling_abort)  ← Set by: LLM tool error within a batch
           │
           ├─ Tool 1 checks: abort_signal OR sibling_abort
           ├─ Tool 2 checks: abort_signal OR sibling_abort
           └─ Tool 3 checks: abort_signal OR sibling_abort
```

**Signal propagation rules:**
- **User interrupt** → parent `abort_signal` set → ALL tools see it
- **LLM tool error** (e.g., OOM) → `sibling_abort` set → sibling tools in same batch see it, but parent query loop does NOT abort (it continues with error results)
- **Non-LLM tool error** (e.g., DB write fails) → only that tool fails, siblings continue
- **Permission denial** → only that tool blocked, siblings continue

**Interrupt behavior enforcement:**
- On user interrupt, check each executing tool's `interrupt_behavior()`:
  - `'cancel'` → `task.cancel()`, generate synthetic "user interrupted" error
  - `'block'` → let it finish, queue new user message until tool completes

```python
# In StreamingToolExecutor._execute_tool():
if self._ctx.abort_signal.is_set():
    if tracked.tool.interrupt_behavior() == "cancel":
        tracked.result = self._create_synthetic_error(tracked, AbortReason.USER_INTERRUPTED)
        tracked.status = ToolStatus.COMPLETED
        return
    # else "block": keep executing normally

if self._sibling_abort.is_set():
    tracked.result = self._create_synthetic_error(tracked, AbortReason.SIBLING_ERROR)
    tracked.status = ToolStatus.COMPLETED
    return
```

### Execution Semantics

- **Progress events** yield immediately (bypass completion ordering)
- **Results** yield in tool-addition order (buffered until ready)
- **Sibling abort:** LLM-backed tool errors cascade to siblings via `sibling_abort` event (OOM protection). Non-LLM tool errors do NOT cascade.
- **Interrupt behavior:** `'cancel'` tools get synthetic error on user interrupt. `'block'` tools keep running.
- **Context modifiers:** Only applied for non-concurrent tools (silently ignored for concurrent)
- **Max concurrency:** Configurable via `JARVIS_MAX_TOOL_CONCURRENCY` env var (default 10)

### Tool Execution with Hooks

```python
async def execute_tool_with_hooks(
    tool: Tool, input: BaseModel, ctx: ToolContext,
    hook_registry: HookRegistry, on_progress: Callable | None = None,
) -> ToolResult:
    """Full execution: PRE hooks → validate → permissions → call → POST hooks."""
    # 1. PRE_TOOL_USE hooks (can block, modify input, approve/deny)
    # 2. Tool.validate_input() (runtime validation)
    # 3. Permission check (hooks don't bypass settings rules)
    # 4. Tool.call() (actual execution)
    # 5. POST_TOOL_USE hooks (or POST_TOOL_USE_FAILURE on error)
    # 6. Result size truncation (max_result_size_chars)
```

### Tool Result Persistence

When a tool result exceeds `max_result_size_chars`, it's persisted to a session-local file. The LLM receives a preview + file path:

```python
async def _persist_large_result(
    result: ToolResult, max_chars: int, session_dir: Path
) -> ToolResult:
    content = str(result.data)
    if len(content) <= max_chars:
        return result
    path = session_dir / f"tool-result-{uuid4()}.txt"
    path.write_text(content)
    preview = content[:2000]
    return ToolResult(
        data=f"Output too large ({len(content)} chars). "
             f"Saved to: {path}\nPreview:\n{preview}\n...",
        metadata={**result.metadata, "persisted_path": str(path)},
    )
```

---

## Phase 2: Query Loop Engine

### Design Principles

The Query Loop is a **hybrid** state machine:
- **Layer 1 (LLM):** Brain dump extraction, intent classification, Q&A, synthesis
- **Layer 2 (Deterministic):** PLAN_DAY pipeline (11 steps with retries), EDIT_TASK, draft management
- **Layer 3 (Infrastructure):** Hooks, compaction, error recovery, background tasks

The LLM does NOT freely choose tools for known intents. It classifies intent, then deterministic pipelines execute. LLM-backed tools are called within those pipelines at specific points.

### Why Hybrid (Not Pure LLM-Driven)

The Jarvis audit found that many decisions CANNOT be left to the LLM:

| Decision | Why Deterministic |
|----------|-------------------|
| Dependency prefixing (`goal_id + "_" + task_id`) | Exact formula, LLM would get wrong |
| Horizon retry `[2880, 4320, 7200, ...]` | Must try in order, stop on first feasible |
| Decomposition retry if < 5 tasks | Quality gate requiring count check |
| Logical day fix (3 AM = yesterday) | Timezone math |
| `has_knowledge` + question pattern guard | Regex correction of LLM's own error |
| Multi-goal fusion namespace collision | Deterministic prevention |
| `_persist_fused_tasks` atomicity | DELETE + INSERT must be atomic |
| SSE event ordering | Frontend expects exact sequence |
| Draft state across requests | Multi-turn stateful loop |

### State Machine

```python
@dataclass
class QueryState:
    messages: list[Message]
    ctx: ToolContext
    turn_count: int = 0
    max_turns: int = 25
    has_attempted_compact: bool = False
    has_escalated_tokens: bool = False
    recovery_attempts: int = 0
    draft: Any | None = None
    execution_summary: dict = field(default_factory=dict)
    transition: str | None = None

class StopReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    USER_INTERRUPTED = "user_interrupted"
    MODEL_ERROR = "model_error"
    CONTEXT_OVERFLOW = "context_overflow"
```

### Main Loop Flow

```
User message arrives
    │
    ├─ BRANCH 1: draft_schedule? → _run_schedule_modify_flow (skip pipeline)
    ├─ BRANCH 2: model_mode="27b"? → _direct_qa_flow (skip pipeline)
    │
    ▼ (Main pipeline)
    │
    ├─ Build memory context → inject into system prompt
    ├─ Brain dump extraction → BrainDumpExtraction schema
    ├─ Intent classification
    │
    ├─ Spawn background tasks (search, habits staging, deadlines)
    │
    ├─ IF PLAN_DAY intent:
    │   └─ _run_plan_day_pipeline (11 deterministic steps)
    │       P1: Fetch & translate habits (sequential, never concurrent)
    │       P2: Compute horizon_start (with logical day fix)
    │       P3: Memory constraints bridge (PEARL patterns → solver constraints)
    │       P4: Decompose goal (with cache check + retry if < 5 tasks)
    │       P5: Multi-goal fusion (namespace + validate dependencies)
    │       P6: Compute horizon steps (from deadlines)
    │       P7: Schedule with retry (OR-Tools, retry horizons on INFEASIBLE)
    │       P8: Create draft (ephemeral until user accepts)
    │       P9: Await search results
    │       P10: Build response (skip VoJ if schedule exists)
    │       P11: Return ChatResponse with draft_id
    │
    ├─ IF INGEST intent: process_ingestion pipeline
    ├─ IF EDIT_TASK: parse → modify → re-solve
    ├─ IF ACCEPT_DRAFT: validate → persist atomically
    ├─ IF REJECT_DRAFT: discard → store reason as memory
    ├─ IF CHAT/QA: free-form LLM tool calling (Sections 1-3 apply)
    │
    ├─ Fire-and-forget memory extraction
    └─ Return/yield results
```

### Pipeline Error Recovery Matrix

Each PLAN_DAY pipeline step has explicit error handling (preserving existing `control_policy.py` behavior):

| Step | On Failure | Recovery | User Sees |
|------|-----------|----------|-----------|
| P1: Fetch habits | DB error | Continue with empty habits | Schedule without habit constraints |
| P1: Translate habits | LLM error / parse fail | Continue with empty slots | Schedule without habit time blocks |
| P2: Compute horizon | Invalid timezone | Snap to now() | Normal schedule |
| P3: Memory constraints | Memory store unavailable | Continue without PEARL constraints | Schedule without behavioral adaptations |
| P4: Decompose (primary) | LLM error / < 5 tasks | Retry with cloud (Gemini) | Brief delay |
| P4: Decompose (retry) | Cloud also fails | Return graceful "clarify your goal" message | "I had trouble breaking that down" |
| P5: Multi-goal fusion | Dependency validation fails | Log error, proceed with new chunks only (drop pending) | Partial schedule |
| P6: Compute horizon steps | No deadline info | Use default sequence [2880, 4320, 7200, 10080, 20160, 43200] | Normal |
| P7: Schedule (per horizon) | INFEASIBLE (422) | Try next horizon in sequence | Transparent (retries hidden) |
| P7: Schedule (all exhausted) | All horizons INFEASIBLE | Return anti-guilt message with recalibration suggestions | "This isn't feasible — consider..." |
| P8: Create draft | DB error | Return schedule without draft (memory-only) | Schedule shown but can't accept |
| P9: Await search | Timeout (10s) / error | Continue without search results | Normal response, no research section |
| P10: Build response | N/A (pure formatting) | N/A | N/A |

**Invariant:** No pipeline step failure produces a 5xx HTTP error. All failures yield a graceful `ChatResponse` with a helpful message.

### Multi-Layer Context Compaction

5 stages in order (adapted from Claude Code):

```
Stage 1: applyToolResultBudget — cap large results per message
Stage 2: snipCompact — remove old tool results (lightweight)
Stage 3: microCompact — clear specific tool types: "[Old content cleared]"
Stage 4: contextCollapse — collapse old context sections (keeps granular data)
Stage 5: autoCompact — full LLM-powered summarization (expensive, last resort)

Reactive: on prompt-too-long error, try stages 2-5 as recovery
```

For Jarvis Phase 2, implement stages 1, 2, 3, and 5 (minimum viable compaction). Stage 2 (snipCompact) is trivial — remove tool results older than N turns — and prevents premature triggering of stage 3. Stage 4 (contextCollapse) is deferred to Phase 5.

### Max Output Tokens Recovery

Three-phase recovery when LLM hits output limit:

1. **Escalate:** 8k → 64k max_output_tokens, retry same request (silent)
2. **Multi-turn:** Inject "Resume directly — no recap" message, up to 3 attempts
3. **Give up:** Surface error gracefully

### Streaming Fallback

When LLM API fails mid-stream:
- Tombstone orphaned messages (remove from UI/history)
- Clear all accumulated state
- Switch to fallback model if available
- Retry entire request

### Query Chain Tracking

```python
@dataclass
class QueryTracking:
    chain_id: str
    depth: int = 0
    source: str = "chat"       # "chat", "agent", "compact", "background"

    def is_foreground(self) -> bool:
        return self.source in ("chat", "agent")
```

Only foreground queries retry on 529/rate-limit. Background queries bail immediately.

### Dependency Injection (for Testing)

```python
@dataclass
class QueryDeps:
    call_model: Callable       # LLM call function
    compact: Callable          # History compaction
    uuid: Callable = field(default_factory=lambda: uuid4)
```

### SSE Event Format (Preserved)

The frontend expects these exact event types (unchanged from current):

| Event | Data | When |
|-------|------|------|
| `phase` | `{"phase": str, ...detail}` | Each pipeline step |
| `step` | `{"intent": str, "stage": str}` | Pipeline complete |
| `thinking` | `{"token": str}` | LLM reasoning token |
| `message` | `{"token": str}` | Response text token |
| `complete` | Full ChatResponse JSON | Stream done |
| `error` | `{"error": str}` | Pipeline error |

### Compatibility with Architecture Reset Spec

| Spec Component | Status |
|----------------|--------|
| Control Policy (`execute_agentic_flow`) | **Replaced** by Query Loop + deterministic pipelines |
| Intent Registry | **Enhanced** — classification routes to pipeline, not if/elif |
| Sequential pipeline | **Replaced** — deterministic pipelines use tools with concurrency |
| SSE streaming | **Preserved** — same events via SSEProgressHook |
| Draft negotiation | **Preserved** — Branch 1 detects draft_schedule |
| Memory retrieval | **Preserved** — injected into system prompt before loop |
| Memory extraction | **Preserved** — fire-and-forget via MemoryExtractionHook |
| PEARL detection | **Preserved** — chained after memory extraction via hook |
| All DB writes | **Preserved** — same tables, same operations |
| All constants/thresholds | **Preserved** — same magic numbers |
| Intent Discovery Engine | **Deferred** to Phase 5 — auto-mode classifier subsumes clustering; free-form CHAT path handles unknown intents via LLM tool calling until discovery is built |
| Schedule Modification Flow | **Preserved** — Branch 1 in query loop detects `draft_schedule` and routes to `_run_schedule_modify_flow` using existing `schedule_modifier.py` and `task_rearranger.py` |

---

## Phase 3: Agent System (Future)

Parent-child forking with prompt cache sharing. Children inherit parent context, run their own query loops, return results. Example: one agent decomposes goals while another fetches constraints.

### Key concepts (design only, not implemented yet):
- `AgentTool` in tool registry spawns child agents
- Children share frozen system prompt for cache hits
- Background execution via asyncio tasks
- Results aggregated back to parent

---

## Phase 4: Coordinator/Swarm Mode (Future)

Central coordinator dispatches to specialist workers (scheduling worker, memory worker, document worker). Workers can communicate. Most advanced pattern from Claude Code.

---

## Phase 5: Advanced Patterns (Future)

- **Speculative execution:** Pre-compute likely next steps during user typing
- **Auto-mode classifier:** LLM-based permission gate for automated tool approval
- **Denial tracking:** Circuit breaker after 3 consecutive / 20 total denials
- **Token budget continuation:** Auto-nudge model to continue when output budget remains
- **Tool use summaries:** Haiku-generated summaries of tool actions (async, fire-and-forget)

---

## Model Strategy: Gemma 4 Upgrade Path

Based on Gemma 4 benchmarks (Arena Elo 1441, t2-bench 85.5%):

| Role | Current Model | Recommended Upgrade | Memory | Concurrent? |
|------|---------------|--------------------|---------|----|
| Primary reasoning | Qwen-27B (dense, ~18GB) | Gemma 4 27B A4B (MoE, ~6GB) | 3x less | **Yes** (3-4 concurrent) |
| Fast classification | Qwen-4B (~3GB) | Gemma 4 E4B (~3GB) | Same | Yes |
| Cloud fallback | Gemini 2.5 Flash | Gemini 2.5 Flash (keep) | N/A | N/A |

**Impact on concurrency:** With Gemma 4 MoE, LLM tools become `is_concurrency_safe=True`. PLAN_DAY goes from sequential (30s) to parallel (8-10s). Controlled by single config: `LLM_CONCURRENCY_SAFE=true`.

---

## Tool-to-Service File Mapping

Each tool wraps an existing service. This is the migration guide:

| Tool | Current Implementation | File |
|------|----------------------|------|
| `BrainDumpExtractTool` | `_run_brain_dump_extraction()` | `control_policy.py` |
| `TranslateHabitsTool` | `translate_habits_to_slots()` | `habit_translator.py` |
| `ExpandHorizonTool` | `expand_semantic_slots_to_time_slots()` | `horizon_expander.py` |
| `DecomposeGoalTool` | `_call_decompose()` + retry logic | `control_policy.py` |
| `FetchConstraintsTool` | `get_behavioral_context_for_calendar()` | `behavioral_store.py` |
| `FetchPendingTasksTool` | `get_all_pending_tasks()` | `task_retrieval.py` |
| `ScheduleTasksTool` | `run_schedule()` | `schedule.py` |
| `PersistScheduleTool` | `_persist_fused_tasks()` | `control_policy.py` |
| `IngestDocumentTool` | `process_ingestion()` | `orchestrator.py` |
| `ExtractCalendarTool` | `extract_calendar_slots()` | `calendar_extractor.py` |
| `LinkMaterialsTool` | `link_document_to_tasks()` | `task_material_linker.py` |
| `StoreConstraintTool` | `store_behavioral_constraint()` | `behavioral_store.py` |
| `DeleteConstraintTool` | `delete_behavioral_constraint()` | `behavioral_store.py` |
| `EditTaskTool` | `handle_edit_task()` | `task_editor.py` |
| `RearrangeTasksTool` | `handle_rearrange()` | `task_rearranger.py` |
| `WorkspaceBuilderTool` | `build_task_workspace()` | `workspace_builder.py` |
| `RecordCompletionTool` | `record_completion()` | `sm2_engine.py` |
| `ExtractMemoriesTool` | `safe_extract_memories()` | `memory/extractor.py` |
| `DraftAcceptTool` | `draft_store.accept_draft()` + `_persist_fused_tasks()` | `draft_store.py` + `control_policy.py` |
| `DraftRejectTool` | `draft_store.reject_draft()` | `draft_store.py` |
| `DirectQATool` | `_direct_qa_response()` | `control_policy.py` |
| `SynthesizeResponseTool` | `synthesize_jarvis_response()` | `voice_of_jarvis.py` |
| `ScheduleModifyTool` | `_run_schedule_modify_flow()` | `control_policy.py` + `schedule_modifier.py` |

---

## Migration Strategy

Incremental migration from `control_policy.py` — NOT a big-bang rewrite:

1. **Phase 1a:** Extract tools one at a time. Each tool wraps the existing function. `control_policy.py` calls the tool instead of the function directly. Tests pass at every step.
2. **Phase 1b:** Add hook system. Register hooks. Existing side effects (memory extraction, cache invalidation) move from inline code to hooks one at a time.
3. **Phase 1c:** Add streaming executor. Replace `asyncio.gather` calls (if any) with executor-managed concurrency.
4. **Phase 2:** Build query loop. Initially, the query loop just calls `execute_agentic_flow` as a single "legacy tool." Then gradually move pipeline steps out of `control_policy.py` into the query loop's deterministic pipeline.
5. **Final:** Delete `control_policy.py` when all logic has migrated.

At every step, `pytest tests/` must pass. No step breaks the existing API.

---

## Observability

Structured logging for debugging pipeline issues (adapted from Claude Code's telemetry):

```python
@dataclass
class PipelineEvent:
    event_type: str                    # "tool_start", "tool_complete", "tool_error", "compact", "retry"
    tool_name: str | None = None
    duration_ms: int | None = None
    token_count: int | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

Key events to log:
- Every tool start/complete/error with duration
- Every LLM call with model, token count, latency
- Every compaction trigger with tokens freed
- Every pipeline step with phase name
- Every retry attempt with reason
- Every background task spawn/complete/error

Logged to `~/.jarvis/logs/` in JSONL format. Optional structured export to analytics.

---

## Testing Strategy

### Unit Tests
- Each tool testable in isolation (inject mock ToolContext)
- Hook system testable with mock callbacks
- Executor testable with mock tools (fast async, slow async, error, concurrent)

### Integration Tests
- Full pipeline: brain dump → decompose → schedule → draft
- Hook lifecycle: PRE → tool → POST → side effects
- Compaction: fill context → trigger compact → verify summary
- Draft negotiation: create → edit → accept/reject

### Dependency Injection
```python
# Production
deps = QueryDeps(call_model=llm.call, compact=_compact_history)

# Test
deps = QueryDeps(
    call_model=mock_llm_returning({"tool_calls": [...]}),
    compact=lambda msgs, ctx: msgs,
)
```

---

## Summary of Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python | Domain stack is Python-native (OR-Tools, MLX-Embed, Docling, ChromaDB, LiteLLM) |
| Architecture | Hybrid query loop | Deterministic pipelines for known intents + free-form for new intents |
| Concurrency model | Tool-declared safety | Each tool declares safe/unsafe per input; executor enforces |
| Hook system | Function + command hooks | Internal fast-path + user extensibility |
| Compaction | 5-stage pipeline | Progressive from cheap (snip) to expensive (LLM summary) |
| Model strategy | Gemma 4 MoE upgrade path | 3x less memory, concurrent inference, better benchmarks |
| Phased delivery | 5 phases, bottom-up | Each phase independently useful; nothing breaks if you stop early |
| Error recovery | Withhold + retry + fallback | Max output tokens escalation, streaming fallback, graceful degradation |
| State management | Getter/setter pattern | Matches Claude Code's AppState; testable, no global mutation |
