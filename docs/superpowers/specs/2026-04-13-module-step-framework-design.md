# ModuleStep Framework — Design Spec

**Date:** 2026-04-13
**Author:** Madhav + Claude (Opus 4.6)
**Status:** Draft — awaiting review
**Supersedes:** Hardcoded `build_planning_graph()`, `build_research_graph()`, `build_knowledge_graph()` in `app/modules/`
**References:**
- `docs/claude-code-architecture.md` — Claude Code internals (9 levels)
- `docs/superpowers/specs/2026-04-12-jarvis-architecture-v2-design.md` — v2 architecture spec
- `claude-code-src-code-main/src/services/tools/StreamingToolExecutor.ts` — Claude Code's generic executor
- `.superpowers/brainstorm/9519-1775987100/content/claude-code-full-architecture.html` — full verified architecture

---

## Executive Summary

Jarvis has 3 hardcoded `build_*_graph()` functions — each manually wires LangGraph nodes, manually emits SSE events, and requires a ~50-line boilerplate wrapper in the orchestrator. Adding a new module (meeting scheduler, code review assistant, study group coordinator) means writing a whole new hardcoded graph.

This spec introduces a **generic module execution framework** inspired by Claude Code's Tool interface and StreamingToolExecutor. Each module declares execution steps with metadata (`ModuleStep`), and a single `build_module_graph()` function builds the LangGraph DAG automatically — parallel where safe, sequential where dependent, with automatic SSE emission, timeouts, feature flags, and hook integration.

### What this replaces

| Before | After |
|--------|-------|
| `build_planning_graph()` (345 lines) | `ModuleDefinition` with 9 `ModuleStep` objects |
| `build_research_graph()` (71 lines) | `ModuleDefinition` with 5 `ModuleStep` objects |
| `build_knowledge_graph()` (92 lines) | `ModuleDefinition` with 6 `ModuleStep` objects |
| 13 manual `_emit_tool_use()` calls in planning | Zero — `_wrap_step()` handles all SSE emission |
| 3 copy-paste wrapper nodes in `graph.py` (~130 lines) | 1 generic `create_module_wrapper()` factory |
| No feature flags on steps | `feature_flag` field — skip disabled steps |
| Global hooks (no module scoping) | `module_id` in hook context for scoped execution |

### Claude Code mapping

| Claude Code Pattern | Jarvis Adaptation |
|---|---|
| `Tool.ts` interface (5 properties) | `ModuleStep` dataclass (10 fields) |
| `StreamingToolExecutor` (queue + concurrency) | `build_module_graph()` + `_wrap_step()` |
| `isConcurrencySafe(input)` | `concurrent_safe: bool` |
| `isReadOnly(input)` | `read_only: bool` |
| `checkPermissions(input, ctx)` | `hook_event` fires pre-step hook |
| `tool.call(args, ctx)` | `handler(state) -> dict` |
| Sub-agent isolation (own context, own hooks) | Module sub-graph isolation (own state, scoped hooks) |
| `AgentToolResult` (parent sees text + stats only) | Orchestrator wrapper sees output dict only |
| `while(needsFollowUp)` reasoning loop | `routes_to` with self-referencing edges |
| Feature flags (89, build-time DCE) | `feature_flag` field (runtime, env-based) |

### Dual execution model

Claude Code uses a single execution model: `while(true)` reasoning loop where the LLM decides what tools to call. This works for coding because coding is open-ended.

Jarvis needs BOTH patterns:
- **Deterministic pipelines** (planning) — graph topology guarantees exact execution order. Math must be correct, not LLM-guessed.
- **Autonomous reasoning loops** (research) — LLM decides: search -> evaluate -> search more -> summarize. `routes_to` with self-referencing edges.
- **Hybrid** (future) — autonomous pre-processing -> deterministic scheduling.

The `ModuleStep` framework unifies both under one declaration model:
- Deterministic modules: `depends_on` + `concurrent_safe` -> parallel DAG
- Autonomous modules: `routes_to` with self-loops -> reasoning loop
- Hybrid modules: combine both in the same step list

---

## 1. Core Data Model

Three dataclasses define the entire system. Located in `app/core/module_framework.py`.

### ModuleStep

```python
@dataclass
class ModuleStep:
    """A single execution step in a module.

    Mirrors Claude Code's Tool interface (Tool.ts):
      handler         = Tool.call(args, ctx)
      concurrent_safe = Tool.isConcurrencySafe(input)
      read_only       = Tool.isReadOnly(input)
      hook_event      = Tool.checkPermissions(input, ctx)
      timeout_ms      ~ Tool.maxResultSizeChars (different constraint, same concept)

    The _wrap_step() decorator mirrors StreamingToolExecutor.executeTool():
    it wraps every step with pre/post hooks, timeout, SSE emission, and error handling.
    """

    name: str                                           # unique within module
    handler: Callable[..., Awaitable[dict]]             # async (state) -> partial state update
    depends_on: list[str] = field(default_factory=list) # step names this waits for
    concurrent_safe: bool = False                       # can run parallel with other concurrent_safe steps
    read_only: bool = False                             # no state mutation (informational only)
    routes_to: dict[Callable, dict[str, str]] | None = None  # condition_fn -> {result: step_name_or_END}
    timeout_ms: int = 30_000                            # per-step timeout
    hook_event: str | None = None                       # which hook fires pre-step (L4/L5)
    feature_flag: str | None = None                     # skip if flag disabled (L8)
    module_name: str = ""                               # auto-set by registry (L3 scoping)
```

**Field semantics:**

- `depends_on` — list of step names that must complete before this step runs. Empty = entry point candidate. LangGraph waits for ALL incoming edges before executing a node (fan-in).
- `concurrent_safe` — when multiple steps share a dependency and are all `concurrent_safe=True`, the builder wires them as parallel fan-out edges. LangGraph runs all outgoing edges in parallel natively.
- `read_only` — marks steps that don't mutate state (e.g., `validate_goal`). Informational for now; future use in compaction (read-only step results can be safely discarded).
- `routes_to` — maps a condition function to a dict of `{result_value: step_name_or_END}`. Use the string `"__END__"` to represent LangGraph's `END` sentinel — the builder translates it automatically. Supports forward references, backward references, and self-loops. Only one `routes_to` per step (LangGraph constraint: one conditional edge set per node).
- `hook_event` — if set, `_wrap_step()` fires this hook event before execution and checks the result. ALLOW continues, DENY skips with error, ASK pauses for consent. Extension point for L5 permission pipeline.
- `feature_flag` — if set, `_wrap_step()` checks `is_feature_enabled(flag)` before execution. Disabled steps emit a `tool_use` event with `status: "skipped"` and return `{}`.

### ConditionalEdge

```python
@dataclass
class ConditionalEdge:
    """Typed escape hatch for edges that don't fit routes_to.

    Still declarative — not arbitrary graph mutation. Each edge declares
    a source step, condition function, and destination map.
    """

    from_step: str
    condition: Callable
    destinations: dict[str, str]  # {condition_result: step_name_or_END}
```

### ModuleDefinition

```python
@dataclass
class ModuleDefinition:
    """Complete module declaration. Replaces build_*_graph() functions.

    Mirrors Claude Code's sub-agent pattern:
    - state_class = sub-agent's own ToolUseContext
    - state_in = how the parent prepares the sub-agent's context
    - state_out = how the parent reads the sub-agent's AgentToolResult
    - steps = the filtered tool set the sub-agent can use
    """

    name: str                                                    # "planning", "research", "knowledge"
    state_class: type                                            # PlanningState, ResearchState, etc.
    steps: list[ModuleStep]
    extra_edges: list[ConditionalEdge] = field(default_factory=list)
    entry_step: str | None = None                                # auto-detected if None
    state_in: Callable | None = None                             # JarvisState -> module state dict
    state_out: Callable | None = None                            # module result dict -> JarvisState updates
```

---

## 2. The Builder — `build_module_graph()`

Single function that replaces `build_planning_graph()`, `build_research_graph()`, and `build_knowledge_graph()`. Takes a `ModuleDefinition`, returns a compiled LangGraph.

Located in `app/core/module_framework.py`.

### Algorithm

```
build_module_graph(definition: ModuleDefinition) -> CompiledGraph:
    1. Create StateGraph(definition.state_class)

    2. For each step in definition.steps:
       a. Set step.module_name = definition.name
       b. Wrap step.handler with _wrap_step(step)
       c. Add as graph.add_node(step.name, wrapped_handler)

    3. Detect entry point:
       - If definition.entry_step is set, use it
       - Else find steps with empty depends_on
       - If exactly one: set as entry point
       - If multiple: create synthetic __entry__ node that fans out to all
       - If zero: raise ValueError (circular dependency)

    4. Build step lookup: {name: step} for O(1) access

    5. Collect all steps that are destinations in some routes_to.
       These steps get their incoming edges from the conditional routing,
       NOT from depends_on. Build a set: routed_destinations.

    6. Translate "__END__" to LangGraph END:
       For all routes_to and extra_edges destinations, replace "__END__" with END.

    7. Wire edges for each step:
       For each step S:
         If S has routes_to:
           For each (condition_fn, destinations) in S.routes_to:
             graph.add_conditional_edges(S.name, condition_fn, destinations)
         Else if S has depends_on AND S.name not in routed_destinations:
           For each dep in S.depends_on:
             dep_step = lookup[dep]
             If dep_step has NO routes_to:
               graph.add_edge(dep, S.name)
             # If dep_step HAS routes_to, the conditional edge already
             # handles routing to S — don't add a duplicate plain edge

    8. Wire extra_edges:
       For each edge in definition.extra_edges:
         graph.add_conditional_edges(edge.from_step, edge.condition, edge.destinations)

    9. Detect terminal steps:
       Steps that: (a) nothing depends on them, AND (b) they have no routes_to
       Wire each to END via graph.add_edge(step.name, END)

    10. Return graph.compile()
```

### The Step Wrapper — `_wrap_step()`

This is the key piece that eliminates all manual `_emit_tool_use()` calls and integrates L3/L5/L7/L8. Mirrors Claude Code's `StreamingToolExecutor.executeTool()`.

```python
import asyncio
import json as _json
from app.core.jarvis_logger import JARVIS_LOGGER as logger


def _emit_tool_use(queue, module_name: str, step_name: str, status: str, detail: dict | None = None) -> None:
    """Emit a tool_use SSE event onto the progress queue."""
    if not queue:
        return
    event = {
        "_event_type": "tool_use",
        "module": module_name,
        "tool": step_name,
        "status": status,
    }
    if detail:
        event["detail"] = detail
    queue.put_nowait(_json.dumps(event))


def _wrap_step(step: ModuleStep) -> Callable:
    """Wrap a step handler with SSE emission, timeout, feature flags, and hooks.

    This decorator is applied by build_module_graph() to every step. Module
    authors never call it directly — they write pure business logic handlers.

    Mirrors Claude Code's StreamingToolExecutor.executeTool():
    - Emits tool_use events (started/done/error/skipped)
    - Applies per-step timeout (like AbortController per tool)
    - Checks feature flags (like feature() gates)
    - Fires pre-step hooks (like PreToolUse)
    """

    async def wrapped(state: dict) -> dict:
        queue = state.get("progress_queue")
        module_name = step.module_name

        # L8: Feature flag check
        if step.feature_flag:
            from app.core.config import is_feature_enabled
            if not is_feature_enabled(step.feature_flag):
                _emit_tool_use(queue, module_name, step.name, "skipped",
                               {"reason": f"feature '{step.feature_flag}' disabled"})
                return {}

        # L4/L5: Pre-step hook (if declared)
        if step.hook_event:
            from app.orchestrator.hooks import get_hooks
            hook_result = await get_hooks().execute(
                step.hook_event,
                module=module_name,
                module_id=module_name,
                step=step.name,
            )
            if hook_result.decision.value == "deny":
                _emit_tool_use(queue, module_name, step.name, "skipped",
                               {"reason": hook_result.reason or "denied by hook"})
                return {}

        # Emit started
        _emit_tool_use(queue, module_name, step.name, "started")

        try:
            # Execute with timeout
            result = await asyncio.wait_for(
                step.handler(state),
                timeout=step.timeout_ms / 1000,
            )

            # Extract optional detail for SSE event
            detail = result.pop("_tool_detail", None) if isinstance(result, dict) else None
            _emit_tool_use(queue, module_name, step.name, "done", detail)
            return result

        except asyncio.TimeoutError:
            logger.warning(f"Step {module_name}.{step.name} timed out after {step.timeout_ms}ms")
            _emit_tool_use(queue, module_name, step.name, "error", {"error": "timeout"})
            return {}

        except Exception as e:
            logger.error(f"Step {module_name}.{step.name} failed: {e}")
            _emit_tool_use(queue, module_name, step.name, "error", {"error": str(e)})
            raise

    wrapped.__name__ = f"{step.module_name}__{step.name}"
    return wrapped
```

### Fan-out logic

When the builder wires edges (step 6), it produces parallel fan-out automatically from `depends_on` relationships. LangGraph runs all outgoing edges from a node in parallel and waits for ALL incoming edges before executing the target node (fan-in).

Example from the planning module:

```
fetch_constraints (entry point)
    ├── translate_habits (depends_on: [fetch_constraints])
    │   └── expand_slots (depends_on: [translate_habits], concurrent_safe=True)
    ├── memory_to_constraints (depends_on: [fetch_constraints], concurrent_safe=True)
    └── validate_goal (depends_on: [fetch_constraints], concurrent_safe=True)
                         │
                         ▼ (routes_to: is_goal_clear -> True: decompose_goal)
                    decompose_goal (depends_on: [expand_slots, memory_to_constraints, validate_goal])
                         │ (fan-in: waits for all 3)
                         ▼
                    fuse_tasks → solve_schedule ⇄ handle_infeasible (retry loop)
```

The builder detects that `fetch_constraints` has three dependents and wires three outgoing edges. LangGraph runs them in parallel. `decompose_goal` has three `depends_on` entries, so LangGraph waits for all three before executing it.

### Self-loops and backward edges

The builder treats `routes_to` destinations as arbitrary step names — forward, backward, or self-referencing. LangGraph handles self-loops natively via `add_conditional_edges`.

Example from research module:
```
evaluate_results.routes_to = {needs_more: {True: "execute_search", False: "summarize"}}
```

This creates `graph.add_conditional_edges("evaluate_results", needs_more, {True: "execute_search", False: "summarize"})` — a self-loop back to `execute_search` when `needs_more` returns `True`. This is Claude Code's `while(needsFollowUp)` expressed as a LangGraph conditional edge.

### Feature flag infrastructure

`ModuleStep.feature_flag` references a string flag name. The `is_feature_enabled()` function in `app/core/config.py` checks environment variables:

```python
# app/core/config.py (addition)
def is_feature_enabled(flag: str) -> bool:
    """Check if a feature flag is enabled. Runtime check via env vars."""
    return os.environ.get(f"JARVIS_{flag}", "1") == "1"
```

Initial flags (all enabled by default):
- `ENABLE_PEARL` — PEARL behavioral inference in memory_to_constraints
- `ENABLE_RAG` — ChromaDB knowledge retrieval in decompose_goal
- `ENABLE_LOCAL_LLM` — local Qwen/Gemma model usage
- `ENABLE_COMPACTION` — future: context compaction between steps
- `ENABLE_OBSERVATION_LOOP` — observation loop after module execution

---

## 3. Existing Module Re-registration

All three current modules map to `ModuleDefinition` with zero handler logic changes. The handlers become pure business logic — all `_emit_tool_use()` calls and `progress_callback` calls are removed.

### Planning Module

Located in `app/modules/planning_graph.py` (refactored).

```python
from app.core.module_framework import ModuleStep, ModuleDefinition

planning_module = ModuleDefinition(
    name="planning",
    state_class=PlanningState,
    state_in=planning_state_in,
    state_out=planning_state_out,
    steps=[
        ModuleStep(
            name="fetch_constraints",
            handler=fetch_constraints,
            concurrent_safe=True,
        ),
        ModuleStep(
            name="translate_habits",
            handler=translate_habits,
            depends_on=["fetch_constraints"],
            timeout_ms=45_000,  # 27B LLM call
        ),
        ModuleStep(
            name="expand_slots",
            handler=expand_slots,
            depends_on=["translate_habits"],
            concurrent_safe=True,
            read_only=True,
        ),
        ModuleStep(
            name="memory_to_constraints",
            handler=memory_to_constraints,
            depends_on=["fetch_constraints"],
            concurrent_safe=True,
            feature_flag="ENABLE_PEARL",
        ),
        ModuleStep(
            name="validate_goal",
            handler=validate_goal,
            depends_on=["fetch_constraints"],
            concurrent_safe=True,
            read_only=True,
            routes_to={is_goal_clear: {True: "decompose_goal", False: "__END__"}},
        ),
        ModuleStep(
            name="decompose_goal",
            handler=decompose_goal,
            depends_on=["expand_slots", "memory_to_constraints", "validate_goal"],
            timeout_ms=60_000,  # 27B LLM call + RAG
        ),
        ModuleStep(
            name="fuse_tasks",
            handler=fuse_tasks,
            depends_on=["decompose_goal"],
        ),
        ModuleStep(
            name="solve_schedule",
            handler=solve_schedule,
            depends_on=["fuse_tasks"],
            routes_to={check_feasibility: {"OPTIMAL": "__END__", "INFEASIBLE": "handle_infeasible"}},
        ),
        ModuleStep(
            name="handle_infeasible",
            handler=handle_infeasible,
            routes_to={can_retry: {"retry": "solve_schedule", "exhausted": "__END__"}},
        ),
    ],
)
```

**Handler changes:**
- Remove all 13 `_emit_tool_use()` calls from `fetch_constraints`, `translate_habits`, `decompose_goal`, `solve_schedule`, `memory_to_constraints`, `handle_infeasible`
- Remove all `cb = state.get("progress_callback"); if cb: cb(...)` calls
- Handlers become pure: `async def fetch_constraints(state) -> dict` with only business logic
- `PlanningState` TypedDict unchanged (including `_merge_lists` reducer on `time_slots`)
- All condition functions (`is_goal_clear`, `check_feasibility`, `can_retry`) unchanged

**State mappers:**

```python
def planning_state_in(state: JarvisState) -> dict:
    user_model = state.get("user_model")
    brain_dump = state.get("brain_dump")
    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "planning_goal": (
            brain_dump.planning_goal
            if brain_dump and hasattr(brain_dump, "planning_goal")
            else state.get("user_message", "")
        ),
        "habits_text": "",
        "semantic_slots": [],
        "time_slots": [],
        "constraints": [],
        "task_chunks": [],
        "pending_tasks": [],
        "schedule": None,
        "horizon_minutes": 2880,
        "retry_count": 0,
        "clarification_request": None,
        "error": None,
        "progress_callback": state.get("progress_callback"),
        "progress_queue": state.get("progress_queue"),
    }


def planning_state_out(result: dict, module_name: str) -> dict:
    return {
        "schedule": result.get("schedule"),
        "execution_graph": (
            {"decomposition": result.get("task_chunks", [])}
            if result.get("task_chunks")
            else None
        ),
        "clarification_request": result.get("clarification_request"),
        "error": result.get("error"),
    }
```

### Research Module

Located in `app/modules/research_graph.py` (refactored).

```python
research_module = ModuleDefinition(
    name="research",
    state_class=ResearchState,
    state_in=research_state_in,
    state_out=research_state_out,
    steps=[
        ModuleStep(
            name="plan_research",
            handler=plan_research,
        ),
        ModuleStep(
            name="execute_search",
            handler=execute_search,
            depends_on=["plan_research"],
            timeout_ms=30_000,
        ),
        ModuleStep(
            name="evaluate_results",
            handler=lambda s: {},
            depends_on=["execute_search"],
            read_only=True,
            routes_to={needs_more: {True: "execute_search", False: "summarize"}},
        ),
        ModuleStep(
            name="summarize",
            handler=summarize,
            timeout_ms=45_000,
        ),
        ModuleStep(
            name="link_to_tasks",
            handler=link_to_tasks,
            depends_on=["summarize"],
        ),
    ],
)
```

**Self-loop:** `evaluate_results` routes back to `execute_search` when `needs_more` returns `True`. This is the autonomous reasoning loop — Claude Code's `while(needsFollowUp)` expressed declaratively.

**State mappers:**

```python
def research_state_in(state: JarvisState) -> dict:
    user_model = state.get("user_model")
    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "query": state.get("user_message", ""),
        "search_results": [],
        "iteration_count": 0,
        "max_iterations": 3,
        "summary": None,
        "linked_tasks": [],
        "error": None,
    }


def research_state_out(result: dict, module_name: str) -> dict:
    return {
        "research_results": result.get("search_results"),
        "error": result.get("error"),
    }
```

### Knowledge Module

Located in `app/modules/knowledge_graph.py` (refactored).

```python
knowledge_module = ModuleDefinition(
    name="knowledge",
    state_class=KnowledgeState,
    state_in=knowledge_state_in,
    state_out=knowledge_state_out,
    steps=[
        ModuleStep(
            name="classify_content",
            handler=classify_content,
            read_only=True,
            routes_to={content_type_router: {
                "calendar": "extract_calendar",
                "document": "ingest_document",
                "file_op": "file_operations",
            }},
        ),
        ModuleStep(
            name="extract_calendar",
            handler=extract_calendar,
        ),
        ModuleStep(
            name="ingest_document",
            handler=ingest_document,
            timeout_ms=60_000,  # Docling can be slow on large PDFs
        ),
        ModuleStep(
            name="link_to_tasks",
            handler=link_to_tasks,
            depends_on=["ingest_document"],
        ),
        ModuleStep(
            name="propose_actions",
            handler=propose_actions,
            depends_on=["link_to_tasks"],
        ),
        ModuleStep(
            name="file_operations",
            handler=file_operations,
        ),
    ],
)
```

**Three terminal steps:** `extract_calendar`, `propose_actions`, `file_operations` — nothing depends on them, no `routes_to`. Builder auto-wires them to `END`.

**State mappers:**

```python
def knowledge_state_in(state: JarvisState) -> dict:
    user_model = state.get("user_model")
    _file_bytes = None
    _file_b64 = state.get("file_base64")
    if _file_b64:
        import base64
        _file_bytes = base64.b64decode(_file_b64)

    _db_client = getattr(user_model, "_db", None) if user_model else None

    return {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "db_client": _db_client,
        "content": state.get("user_message", ""),
        "file_bytes": _file_bytes,
        "media_type": state.get("file_media_type"),
        "file_name": state.get("file_name"),
        "content_type": None,
        "ingestion_result": None,
        "calendar_result": None,
        "linked_tasks": [],
        "action_proposals": [],
        "error": None,
    }


def knowledge_state_out(result: dict, module_name: str) -> dict:
    return {
        "ingestion_result": result.get("ingestion_result"),
        "error": result.get("error"),
    }
```

---

## 4. Orchestrator Integration

### 4a: Module Registry

Located in `app/core/module_framework.py` (same file as data model).

```python
class ModuleRegistry:
    """Registry for cognitive modules. Compiles and caches LangGraph sub-graphs.

    Extends the BaseRegistry pattern from core/registry.py but specialized
    for ModuleDefinition objects with lazy compilation.
    """

    def __init__(self):
        self._modules: dict[str, ModuleDefinition] = {}
        self._compiled: dict[str, CompiledGraph] = {}

    def register(self, definition: ModuleDefinition) -> None:
        """Register a module definition. Invalidates cached compilation."""
        # Auto-set module_name on all steps
        for step in definition.steps:
            step.module_name = definition.name
        self._modules[definition.name] = definition
        self._compiled.pop(definition.name, None)

    def get_compiled(self, name: str) -> CompiledGraph:
        """Get compiled graph, building lazily on first access."""
        if name not in self._compiled:
            if name not in self._modules:
                raise KeyError(f"No module '{name}' registered")
            self._compiled[name] = build_module_graph(self._modules[name])
        return self._compiled[name]

    def get_definition(self, name: str) -> ModuleDefinition:
        """Get the raw module definition."""
        return self._modules[name]

    def registered_names(self) -> list[str]:
        """List all registered module names."""
        return list(self._modules.keys())
```

**Registration at startup:**

```python
# app/modules/__init__.py
from app.core.module_framework import ModuleRegistry

module_registry = ModuleRegistry()


def register_default_modules() -> None:
    """Register all built-in modules. Called during app lifespan startup."""
    from app.modules.planning_graph import planning_module
    from app.modules.research_graph import research_module
    from app.modules.knowledge_graph import knowledge_module

    module_registry.register(planning_module)
    module_registry.register(research_module)
    module_registry.register(knowledge_module)
```

Called from `app/main.py` lifespan alongside `register_default_intents()` and `register_default_document_types()`.

### 4b: Generic Orchestrator Wrapper

Located in `app/orchestrator/module_wrapper.py`.

Replaces `_planning_module_node`, `_knowledge_module_node`, and `_research_agent_node` in `graph.py`.

```python
from app.orchestrator.hooks import get_hooks
from app.orchestrator.state import JarvisState
from app.modules import module_registry


def create_module_wrapper(module_name: str) -> Callable:
    """Generate an orchestrator node that wraps a module sub-graph.

    Mirrors Claude Code's sub-agent isolation pattern:
    - Module gets its own state (like sub-agent's own ToolUseContext)
    - Hooks fire with module_id scope (like agentId-scoped session hooks)
    - Orchestrator sees only the output dict (like AgentToolResult)
    - progress_queue bridges SSE events (like AgentToolResult.stats)
    """

    async def wrapper(state: JarvisState) -> dict:
        hooks = get_hooks()

        # L3: Module-scoped hook — PreModuleExecution
        pre = await hooks.execute(
            "PreModuleExecution",
            module=module_name,
            module_id=module_name,
            initiated_by=state.get("initiated_by", "user"),
        )
        if pre.decision.value == "deny":
            return {
                "response_message": pre.reason or "Action blocked.",
                "modules_invoked": state.get("modules_invoked", []) + [module_name],
            }
        if pre.decision.value == "ask":
            return {
                "response_message": pre.reason or "Action requires consent.",
                "needs_consent": True,
                "modules_invoked": state.get("modules_invoked", []) + [module_name],
            }

        # State translation: JarvisState -> module state
        definition = module_registry.get_definition(module_name)
        if definition.state_in:
            module_state = definition.state_in(state)
        else:
            module_state = dict(state)  # pass-through fallback

        # Invoke compiled sub-graph (isolation boundary)
        compiled = module_registry.get_compiled(module_name)
        result = await compiled.ainvoke(module_state)

        # Extract results: module state -> JarvisState updates
        if definition.state_out:
            output = definition.state_out(result, module_name)
        else:
            output = {}

        output["modules_invoked"] = state.get("modules_invoked", []) + [module_name]
        return output

    wrapper.__name__ = f"{module_name}_node"
    return wrapper
```

### 4c: Simplified `build_jarvis_graph()`

Located in `app/orchestrator/graph.py` (refactored).

```python
from app.modules import module_registry
from app.orchestrator.module_wrapper import create_module_wrapper


def build_jarvis_graph(checkpointer=None):
    """Build the Jarvis orchestrator graph.

    Module nodes are generated from the registry — no per-module boilerplate.
    Adding a new module = registering a ModuleDefinition. The orchestrator
    automatically wraps it with hook checks, state translation, and isolation.
    """
    graph = StateGraph(JarvisState)

    # LLM-powered nodes (unchanged)
    graph.add_node("load_context", _load_context)
    graph.add_node("extract_brain_dump", _extract_brain_dump)
    graph.add_node("classify_intent", _classify_intent)

    # Module nodes — generic, from registry
    for name in module_registry.registered_names():
        graph.add_node(name, create_module_wrapper(name))

    # Single-function modules (no sub-graph, no registry needed)
    graph.add_node("conversation_module", run_general_chat)
    graph.add_node("coach_module", run_coaching_response)

    # Shared nodes
    graph.add_node("synthesize_response", voice_of_jarvis_synthesis)
    graph.add_node("observation_loop", run_observation_loop)

    # Entry + negotiation short-circuit
    graph.set_entry_point("load_context")
    graph.add_conditional_edges("load_context", check_negotiation_shortcut, {
        "negotiation_active": "planning_module",
        "normal": "extract_brain_dump",
    })
    graph.add_edge("extract_brain_dump", "classify_intent")

    # Intent routing (unchanged)
    graph.add_conditional_edges("classify_intent", route_to_module, {
        "planning_module": "planning_module",
        "research_agent": "research_agent",
        "coach_module": "coach_module",
        "knowledge_module": "knowledge_module",
        "conversation_module": "conversation_module",
    })

    # All modules -> synthesize -> observe
    for name in module_registry.registered_names():
        graph.add_edge(name, "synthesize_response")
    graph.add_edge("coach_module", "synthesize_response")
    graph.add_edge("synthesize_response", "observation_loop")
    graph.add_edge("conversation_module", "observation_loop")

    graph.add_conditional_edges("observation_loop", check_needs_followup, {
        "continue": "classify_intent",
        "done": END,
    })

    return graph.compile(checkpointer=checkpointer)
```

**What's eliminated from graph.py:**
- `_planning_module_node` (~45 lines)
- `_knowledge_module_node` (~50 lines)
- `_research_agent_node` (~35 lines)
- `_planning_compiled = build_planning_graph()` (module-level)
- `_knowledge_compiled = build_knowledge_graph()` (module-level)
- `_research_compiled = build_research_graph()` (module-level)

**What's preserved:**
- `JarvisState` schema — unchanged
- `_load_context`, `_extract_brain_dump`, `_classify_intent` — unchanged
- All routing logic — unchanged
- `conversation_module` and `coach_module` — remain as plain async functions (single-node, no sub-graph needed)

---

## 5. File Layout

### New files

| File | Contents |
|------|----------|
| `app/core/module_framework.py` | `ModuleStep`, `ConditionalEdge`, `ModuleDefinition`, `ModuleRegistry`, `build_module_graph()`, `_wrap_step()`, `_emit_tool_use()` |
| `app/orchestrator/module_wrapper.py` | `create_module_wrapper()` factory |
| `app/modules/__init__.py` | `module_registry` singleton, `register_default_modules()` |

### Modified files

| File | Changes |
|------|---------|
| `app/modules/planning_graph.py` | Remove `build_planning_graph()`, `_emit_tool_use()`. Remove all manual SSE calls from handlers. Add `planning_module = ModuleDefinition(...)`, `planning_state_in()`, `planning_state_out()`. Keep `PlanningState`, all handlers, all condition functions. |
| `app/modules/research_graph.py` | Remove `build_research_graph()`. Add `research_module = ModuleDefinition(...)`, `research_state_in()`, `research_state_out()`. Keep `ResearchState`, all handlers, `needs_more`. |
| `app/modules/knowledge_graph.py` | Remove `build_knowledge_graph()`. Add `knowledge_module = ModuleDefinition(...)`, `knowledge_state_in()`, `knowledge_state_out()`. Keep `KnowledgeState`, all handlers, `content_type_router`. |
| `app/orchestrator/graph.py` | Remove 3 wrapper functions + 3 compiled graph calls. Import `module_registry` and `create_module_wrapper`. Simplify `build_jarvis_graph()`. |
| `app/core/config.py` | Add `is_feature_enabled(flag: str) -> bool` |
| `app/main.py` | Add `register_default_modules()` call in lifespan |

### Unchanged files

All handler logic, condition functions, state TypedDicts, schemas, the orchestrator's routing logic, hook system (`hooks.py`), and all service files remain unchanged.

---

## 6. Backwards Compatibility

### What stays identical

- **All handler function signatures** — `async def handler(state: TypedDict) -> dict` — unchanged
- **All condition function signatures** — `def condition(state: TypedDict) -> str | bool` — unchanged
- **All State TypedDicts** — `PlanningState`, `ResearchState`, `KnowledgeState` — unchanged
- **JarvisState** — unchanged
- **Orchestrator graph topology** — identical edges, identical routing
- **SSE event format** — `{"_event_type": "tool_use", "module": "...", "tool": "...", "status": "..."}` — identical
- **Hook system** — `ActionHooks`, `HookResult`, `HookDecision` — unchanged (only extended with `module_id` in context)
- **All service files** — zero changes to `control_policy.py`, `habit_translator.py`, `solver.py`, etc.

### What changes

- **No more manual `_emit_tool_use()` calls** — handlers no longer need to emit SSE events
- **No more `progress_callback` checks** — the wrapper handles progress
- **`build_*_graph()` functions removed** — replaced by `ModuleDefinition` declarations
- **Wrapper nodes in graph.py removed** — replaced by generic `create_module_wrapper()`

### Migration path

Each module can be migrated independently. The migration for one module is:

1. Create `ModuleDefinition` with existing handlers + condition functions
2. Add `state_in` and `state_out` mappers (extract from existing wrapper node)
3. Remove `_emit_tool_use()` calls from handlers
4. Remove `build_*_graph()` function
5. Register in `register_default_modules()`
6. Remove wrapper node from `graph.py`, add to registry loop

No other code changes needed. Tests that call individual handlers continue to work since handler signatures are unchanged.

---

## 7. Flagged for Separate Specs

These capabilities are supported by the framework (extension points exist) but require their own design specs:

### L4: Hook System Expansion (7 -> ~25 hooks)

The `ModuleStep.hook_event` field and `module_id` in hook context are the extension points. The actual hook definitions (PreStepExecution, PostStepExecution, TaskCreated, ScheduleCreated, etc.) and their handlers are a separate spec. Current 7 hooks continue to work unchanged.

### L5: Full Permission Pipeline

The pre-step hook check in `_wrap_step()` is the extension point. A full 9-step permission pipeline (deny rules, ask rules, step-level check, safety, mode, allow rules, consent tracking) builds on top. Current ALLOW/DENY/ASK/MODIFY in `ActionHooks` continues to work.

### L6: Context Compaction (4 types)

The `PreCompact` and `PostCompact` hook events are declared in L4 expansion. Compaction logic (micro, auto, snip, reactive) is a separate Memory & Context spec. The framework supports it via hook events between steps.

### L8: Feature Flag Configuration

The `is_feature_enabled()` function and `feature_flag` field are in this spec. A Supabase config table, admin UI, and enterprise feature gates are separate.

### L9: AsyncGenerator Migration

The `asyncio.Queue` + SSE pattern works for single user. Migration to `AsyncGenerator` at module level (natural backpressure for concurrent users) is a scalability optimization for later.

---

## 8. Testing Strategy

### Unit tests for framework

| Test | What it verifies |
|------|-----------------|
| `test_build_module_graph__linear_steps__correct_order` | Steps with linear `depends_on` execute in order |
| `test_build_module_graph__parallel_fan_out__concurrent_execution` | Steps with `concurrent_safe=True` sharing a dependency run in parallel |
| `test_build_module_graph__fan_in__waits_for_all` | Step with multiple `depends_on` waits for all |
| `test_build_module_graph__routes_to__conditional_edge` | Conditional routing works (OPTIMAL -> END, INFEASIBLE -> retry) |
| `test_build_module_graph__self_loop__terminates` | Self-referencing `routes_to` loops and terminates |
| `test_build_module_graph__three_way_branch__correct_routing` | Knowledge module's 3-way content type routing |
| `test_wrap_step__emits_sse_started_done` | Wrapper emits tool_use events on queue |
| `test_wrap_step__timeout__emits_error` | Timed-out step emits error event and returns {} |
| `test_wrap_step__feature_flag_disabled__skips` | Disabled feature flag skips step |
| `test_wrap_step__hook_deny__skips` | Hook returning DENY skips step |
| `test_module_registry__lazy_compilation` | Graph compiled only on first `get_compiled()` |

### Integration tests

| Test | What it verifies |
|------|-----------------|
| `test_planning_module__via_framework__identical_to_hardcoded` | Planning module via `ModuleDefinition` produces same schedule as old `build_planning_graph()` |
| `test_research_module__self_loop__terminates_after_max_iterations` | Research loop respects `max_iterations` |
| `test_orchestrator__module_wrapper__fires_hooks` | Generic wrapper fires PreModuleExecution hook |

---

## 9. Adding a New Module (The Payoff)

After this framework is in place, adding a new module requires:

```python
# app/modules/meeting_scheduler_graph.py

class MeetingState(TypedDict):
    user_id: str
    user_model: Any
    calendar_events: list
    attendees: list
    proposed_times: list
    selected_time: dict | None
    error: str | None
    progress_queue: Any


async def fetch_calendar(state: MeetingState) -> dict:
    # ... fetch calendar events
    return {"calendar_events": events}


async def find_available_slots(state: MeetingState) -> dict:
    # ... compute available slots
    return {"proposed_times": slots}


async def propose_meeting(state: MeetingState) -> dict:
    # ... propose to user
    return {"selected_time": best_slot}


meeting_module = ModuleDefinition(
    name="meeting_scheduler",
    state_class=MeetingState,
    state_in=meeting_state_in,
    state_out=meeting_state_out,
    steps=[
        ModuleStep(name="fetch_calendar", handler=fetch_calendar),
        ModuleStep(name="find_slots", handler=find_available_slots,
                   depends_on=["fetch_calendar"], concurrent_safe=True),
        ModuleStep(name="propose", handler=propose_meeting,
                   depends_on=["find_slots"]),
    ],
)
```

Then register it:
```python
module_registry.register(meeting_module)
```

And add the intent routing in the orchestrator. No graph topology code. No manual SSE emission. No wrapper boilerplate. The framework handles parallelism, SSE events, timeouts, feature flags, and hook integration automatically.

That's the path from "productivity planner" to "Tony Stark's JARVIS" — one `ModuleDefinition` at a time.
