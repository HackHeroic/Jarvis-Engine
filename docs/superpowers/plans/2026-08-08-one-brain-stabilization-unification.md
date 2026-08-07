# One Brain — Stabilization + v1/v2 Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the frontend's default chat path (`/api/v1/chat/v2/stream`) fully functional — resilient to DB/LLM outages, with drafts, persistence, TMT scheduling, and multi-turn negotiation — and deprecate the parallel v1 brain.

**Architecture:** Phase 0 fixes the outage-resilience bugs (stores silently bypassing the stub DB client, cloud fallback skipping unstructured calls, stale model detection) and turns the test suite green. Phase 1 ports the four missing v1 capabilities into the v2 planning sub-graph (mostly by calling the existing reusable `run_schedule()` — never copying its logic), makes `JarvisState` serializable so a SQLite checkpointer can revive the dead negotiation loop, extends intent classification, and deprecates v1.

**Tech Stack:** FastAPI, LangGraph (StateGraph + SqliteSaver), OR-Tools CP-SAT, Supabase, LiteLLM (LM Studio local / Gemini cloud), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-08-one-brain-stabilization-unification-design.md`

## Global Constraints

- Python venv: `/Users/madhav/Jarvis-cursor/Jarvis-Engine/.venv` (Python 3.13.9). Run tests as `source .venv/bin/activate && python -m pytest`.
- Never run two 27B-class LLM calls concurrently (`asyncio.gather`) — OOM risk on 24 GB M4 Pro.
- All Supabase queries filter by `user_id` (IDOR protection).
- `hybrid_route_query` / `route_llm_call` is the single LLM call site; never call LiteLLM directly from endpoints.
- Tests must not hit real LLMs or real Supabase — mock `route_llm_call`/`hybrid_route_query` and inject fake DB clients.
- No secrets in code or logs. `app/core/config.py` is the only reader of env vars.
- `run_schedule` in `app/api/v1/endpoints/schedule.py` is the reusable scheduling function — import and call it, never copy its logic.
- Anti-guilt on INFEASIBLE: solver failure is a scope problem, never a user error.
- Commit after every task (small commits). Branch: `spine-may1-wip`.
- Plans/specs live in `docs/superpowers/`; never `.cursor/plans/`.

---

### Task 1: Store resilience — explicit-client sentinel for MemoryStore and DraftStore

The bug: `MemoryStore.__init__` does `supabase_client or _get_supabase()` (`app/services/memory/store.py:34`) and `DraftStore` does the same, so when the app passes `None` in degraded mode (or a test seeds an in-memory store), the store silently builds a LIVE client from env and every call dies with `httpx.ConnectError` → chat 500s. Fix: distinguish "not passed" (default → build from env) from "explicitly passed None/falsy" (→ no client, degrade). Also guard the chat hot path against connection errors.

**Files:**
- Modify: `app/services/memory/store.py` (init, ~line 32)
- Modify: `app/services/draft_store.py` (init, ~line 47)
- Modify: `app/services/memory/retriever.py` (`build_memory_context`, ~line 131)
- Test: `tests/test_store_resilience.py` (create)

**Interfaces:**
- Produces: `MemoryStore(supabase_client=_UNSET)` and `DraftStore(supabase_client=_UNSET, ttl_seconds=None)` — passing `supabase_client=None` explicitly now yields a store whose methods return empty defaults (`[]` / `None` / `False`) without any network call. `build_memory_context(user_id, memory_store) -> str` returns `""` on any `Exception` from the store.
- Consumes: existing `_get_supabase()` helpers in both files (unchanged).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_resilience.py
"""Stores must respect an explicitly-passed None client (degraded mode)."""
from unittest.mock import patch


def test_memory_store__explicit_none_client__no_env_fallback():
    from app.services.memory import store as store_mod
    with patch.object(store_mod, "_get_supabase") as mock_get:
        s = store_mod.MemoryStore(supabase_client=None)
        mock_get.assert_not_called()
        assert s.get_active_memories("u1") == []


def test_memory_store__default_arg__uses_env_fallback():
    from app.services.memory import store as store_mod
    with patch.object(store_mod, "_get_supabase", return_value=None) as mock_get:
        store_mod.MemoryStore()
        mock_get.assert_called_once()


def test_draft_store__explicit_none_client__no_env_fallback():
    from app.services import draft_store as ds_mod
    with patch.object(ds_mod, "_get_supabase") as mock_get:
        s = ds_mod.DraftStore(supabase_client=None, ttl_seconds=300)
        mock_get.assert_not_called()
        assert s.get_pending_draft("u1") is None


def test_build_memory_context__store_raises__returns_empty_string():
    from app.services.memory.retriever import build_memory_context

    class ExplodingStore:
        def get_active_memories(self, user_id):
            raise ConnectionError("dns dead")

    assert build_memory_context("u1", ExplodingStore()) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store_resilience.py -v`
Expected: FAIL — `mock_get.assert_not_called()` fails (env fallback fires on explicit None), and the retriever test raises `ConnectionError`.

- [ ] **Step 3: Implement the sentinel in both stores**

In `app/services/memory/store.py`, above the class:

```python
_UNSET = object()  # sentinel: distinguish "arg not passed" from "explicitly None"
```

and change `__init__`:

```python
    def __init__(self, supabase_client=_UNSET):
        # Explicitly-passed None (degraded mode / tests) must NOT silently
        # build a live client from env — that defeats startup degradation.
        self._supabase = _get_supabase() if supabase_client is _UNSET else supabase_client
```

Apply the identical pattern in `app/services/draft_store.py` (`_UNSET` module constant; `def __init__(self, supabase_client=_UNSET, ttl_seconds=None):` with the same two-line body, preserving the existing `ttl_seconds` handling).

- [ ] **Step 4: Guard the retriever**

In `app/services/memory/retriever.py`, wrap the body of `build_memory_context` (the store-reading part) in:

```python
    try:
        memories = memory_store.get_active_memories(user_id)
    except Exception as e:  # ConnectError, DNS failure, timeouts — degrade, never 500
        logger.warning(f"build_memory_context degraded (store unavailable): {e}")
        return ""
```

(keep the rest of the function that formats `memories` unchanged; if the file has no `logger`, use the existing import pattern `from app.core.jarvis_logger import JARVIS_LOGGER as logger`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_store_resilience.py tests/test_memory_store.py tests/test_draft_store.py -v`
Expected: all PASS (existing store tests must not regress).

- [ ] **Step 6: Commit**

```bash
git add app/services/memory/store.py app/services/draft_store.py app/services/memory/retriever.py tests/test_store_resilience.py
git commit -m "fix: stores respect explicit None client — chat degrades instead of 500 when DB is down"
```

---

### Task 2: Honor GEMINI_PRIMARY for unstructured LLM calls

The bug: in `app/models/brain/litellm_conf.py` (~line 114), the Gemini-primary redirect requires `response_schema is not None`, so unstructured calls (streaming chat, voice synthesis without schema) still target dead LM Studio and raise `litellm.InternalServerError`.

**Files:**
- Modify: `app/models/brain/litellm_conf.py:114-122`
- Test: `tests/test_routing_helpers.py` (add to existing file)

**Interfaces:**
- Produces: `hybrid_route_query(...)` routes to `_cfg.GEMINI_MODEL` whenever `_cfg.GEMINI_PRIMARY and _cfg.GEMINI_API_KEY and not prefer_local`, regardless of `response_schema`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routing_helpers.py`:

```python
@pytest.mark.asyncio
async def test_hybrid_route__gemini_primary_unstructured__routes_to_cloud(monkeypatch):
    """GEMINI_PRIMARY must redirect even when no response_schema is given."""
    import app.core.config as cfg
    from app.models.brain import litellm_conf

    monkeypatch.setattr(cfg, "GEMINI_PRIMARY", True)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "GEMINI_MODEL", "gemini/gemini-2.5-flash")

    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        class _Msg:  # minimal LiteLLM response shape
            content = "ok"
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
        return _Resp()

    monkeypatch.setattr(litellm_conf.litellm, "acompletion", fake_acompletion)
    await litellm_conf.hybrid_route_query(
        system_prompt="s", user_prompt="hello", response_schema=None
    )
    assert captured["model"] == "gemini/gemini-2.5-flash"
```

(match the existing file's import style; if config is accessed as `_cfg` inside `litellm_conf`, monkeypatch `litellm_conf._cfg` attributes instead — check the module's actual alias first.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_routing_helpers.py -k unstructured -v`
Expected: FAIL — `captured["model"]` is the local model, not Gemini.

- [ ] **Step 3: Fix the routing condition**

In `hybrid_route_query`, change the Gemini-primary block by deleting the schema condition:

```python
        # Gemini-primary routing: no local models loaded → ALL calls go to cloud
        # (structured AND unstructured — a dead LM Studio must never be targeted)
        if (
            model_override is None
            and _cfg.GEMINI_PRIMARY
            and not prefer_local
            and not force_cloud
            and _cfg.GEMINI_API_KEY
        ):
            force_cloud = True
```

- [ ] **Step 4: Run tests to verify pass + no regression**

Run: `python -m pytest tests/test_routing_helpers.py -v`
Expected: new test PASSES; pre-existing failures in this file (stale `GEMINI_API_KEY` patch targets) are handled in Task 4 — only ensure no NEW failures.

- [ ] **Step 5: Commit**

```bash
git add app/models/brain/litellm_conf.py tests/test_routing_helpers.py
git commit -m "fix: honor GEMINI_PRIMARY for unstructured calls — never target dead LM Studio"
```

---

### Task 3: Model detection refresh (12B-class primaries, non-Gemma candidates)

`detect_loaded_models()` (`app/core/config.py:43-77`) only recognizes `27b/26b/22b` as heavy and only models containing `"gemma"`. Current-gen local models (Gemma 4 12B — better than the old 27B-class at ~8 GB; Qwen 3.6) are misclassified or invisible.

**Files:**
- Modify: `app/core/config.py:43-77`
- Test: `tests/test_model_detection.py` (create)

**Interfaces:**
- Produces: `detect_loaded_models() -> dict` with keys `primary`, `fast`, `loaded` (and optional `warning`). Precedence: env override (`GEMMA_PRIMARY_MODEL`/`GEMMA_FAST_MODEL` set to non-default) > loaded Gemma > loaded non-Gemma (`qwen`) last resort. Heavy match: any of `27b, 26b, 22b, 14b, 12b`. Fast match: any of `e4b, e2b, -4b, 1b, 3b`.
- Consumes: LM Studio `GET {LOCAL_LLM_URL}/models` (mock in tests via `httpx`/`requests` — check which client the function uses and mock that).

- [ ] **Step 1: Extract a pure selection function + write failing tests**

The current function mixes HTTP probing with selection logic — untestable. First the tests, against a new pure function `_select_models(ids: list[str]) -> tuple[str | None, str | None]` returning `(primary_id, fast_id)`:

```python
# tests/test_model_detection.py
from app.core.config import _select_models


def test_select__12b_recognized_as_primary():
    primary, fast = _select_models(["google/gemma-4-12b", "google/gemma-4-e4b"])
    assert primary == "google/gemma-4-12b"
    assert fast == "google/gemma-4-e4b"


def test_select__heavy_and_fast_split():
    primary, fast = _select_models(["google/gemma-4-26b-a4b", "google/gemma-4-e4b"])
    assert primary == "google/gemma-4-26b-a4b"
    assert fast == "google/gemma-4-e4b"


def test_select__single_model_serves_both_roles():
    primary, fast = _select_models(["google/gemma-4-12b"])
    assert primary == fast == "google/gemma-4-12b"


def test_select__qwen_only__used_as_last_resort():
    primary, fast = _select_models(["qwen/qwen-3.6-14b"])
    assert primary == "qwen/qwen-3.6-14b"


def test_select__gemma_beats_qwen():
    primary, _ = _select_models(["qwen/qwen-3.6-14b", "google/gemma-4-12b"])
    assert primary == "google/gemma-4-12b"


def test_select__nothing_loaded():
    assert _select_models([]) == (None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_model_detection.py -v`
Expected: FAIL with `ImportError: cannot import name '_select_models'`.

- [ ] **Step 3: Implement `_select_models` and rewire `detect_loaded_models`**

In `app/core/config.py`, above `detect_loaded_models`:

```python
_HEAVY_MARKERS = ("27b", "26b", "22b", "14b", "12b")
_FAST_MARKERS = ("e4b", "e2b", "-4b", "1b", "3b")


def _select_models(ids: list[str]) -> tuple[str | None, str | None]:
    """Pick (primary, fast) from loaded model ids.

    Gemma family preferred; non-Gemma (qwen) accepted as last resort.
    A lone model serves both roles.
    """
    def _pick(pool: list[str]) -> tuple[str | None, str | None]:
        heavy = next((i for i in pool if any(m in i for m in _HEAVY_MARKERS)), None)
        fast = next((i for i in pool if any(m in i for m in _FAST_MARKERS)), None)
        if heavy and fast:
            return heavy, fast
        if heavy:
            return heavy, heavy
        if fast:
            return fast, fast
        return (pool[0], pool[0]) if pool else (None, None)

    gemmas = [i for i in ids if "gemma" in i.lower()]
    if gemmas:
        return _pick(gemmas)
    others = [i for i in ids if "qwen" in i.lower()]
    return _pick(others)
```

Then inside `detect_loaded_models`, replace the inline `gemmas`/`heavy`/`fast` selection block with:

```python
        primary, fast_model = _select_models(ids)
        if primary is None:
            return {"loaded": ids, "warning": "No usable model loaded in LM Studio"}
        print(f"[Model Detect] primary={primary} fast={fast_model} (from {len(ids)} loaded)")
```

keeping the existing global mutation of `GEMMA_PRIMARY_MODEL`/`GEMMA_FAST_MODEL`/`SLM_ROUTER_MODEL`/`LOCAL_LLM_MODEL` and the `openai/` prefixing exactly as it is today (read the surrounding lines and preserve them).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_model_detection.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_model_detection.py
git commit -m "feat: model detection recognizes 12B/14B primaries and qwen fallback candidates"
```

---

### Task 4: Test suite green — fixtures, stale mocks, offline guarantee

17 known failures in 4 clusters, plus one test that spends real money. Fix each cluster; suite must pass fully offline.

**Files:**
- Modify: `tests/test_orchestrator.py`, `tests/test_integration.py` (graph-build fixtures)
- Modify: `tests/test_routing_helpers.py` (5 stale `GEMINI_API_KEY` patch targets)
- Modify: `tests/test_draft_endpoints.py`, `tests/test_draft_integration.py` (inject in-memory client — mostly fixed by Task 1)
- Modify: `tests/test_modules.py` (mock the live coach LLM call)
- Modify: `requirements.txt` (chromadb; langgraph floor)

**Interfaces:**
- Consumes: Task 1's `_UNSET` sentinel semantics (`DraftStore(supabase_client=None, ...)` = pure in-memory legacy behavior — verify the legacy in-memory dict path still exists in `draft_store.py`; if the store is Supabase-only, seed tests with a fake client object implementing `table().select().eq()...execute()` chains instead).
- Produces: `python -m pytest tests/` → 0 failures with network disabled.

- [ ] **Step 1: Reproduce and enumerate**

Run: `python -m pytest tests/ -q 2>&1 | tail -30`
Record the exact failure list (expect ~17: 5 graph-build, 5 routing-helper, 4 draft-endpoint, 3 draft-integration).

- [ ] **Step 2: Fix graph-build fixtures**

The 5 `ValueError: ... unknown target 'planning_module'` failures: tests build the orchestrator graph with an empty module registry. Add to `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _registered_modules():
    """Orchestrator graph targets module nodes — registry must be populated."""
    from app.modules import module_registry, register_default_modules
    if not module_registry.list_modules():
        register_default_modules()
    yield
```

(Check the actual registration entry point name with `grep -n "def register" app/modules/__init__.py` and use what exists; if `list_modules()` doesn't exist, use the registry's actual introspection method or guard with try/except on double-registration.)

- [ ] **Step 3: Fix stale routing-helper mocks**

The 5 `AttributeError: module 'app.models.brain.litellm_conf' does not have the attribute 'GEMINI_API_KEY'` failures: the symbol moved to `app.core.config`. In `tests/test_routing_helpers.py`, replace every `patch("app.models.brain.litellm_conf.GEMINI_API_KEY", ...)` / `patch.object(litellm_conf, "GEMINI_API_KEY", ...)` with the config module target, e.g.:

```python
monkeypatch.setattr("app.core.config.GEMINI_API_KEY", "test-key")
```

(the module is imported as `_cfg` inside `litellm_conf`, and module attributes are read at call time, so patching `app.core.config` works).

- [ ] **Step 4: Fix draft endpoint/integration tests**

Re-run: `python -m pytest tests/test_draft_endpoints.py tests/test_draft_integration.py -v`
With Task 1 landed, `DraftStore(ttl_seconds=300)` (default arg) still builds a live client → seeded drafts vanish → 404. Change the test fixtures to pass an explicit in-memory setup: `DraftStore(supabase_client=None, ttl_seconds=300)` **if** the store retains a legacy in-memory dict when clientless; otherwise write a minimal fake:

```python
class FakeSupabase:
    """Chainable in-memory stand-in for the two tables draft tests touch."""
    def __init__(self):
        self.rows = {}  # table -> list[dict]
    # implement table(name) returning a query object supporting
    # .insert(row).execute() / .select("*").eq(k, v).execute() /
    # .update(d).eq(k, v).execute() / .delete().eq(k, v).execute()
    # backed by self.rows — ~40 lines, keep in tests/fakes.py
```

Diagnose which path `DraftStore` actually uses (`grep -n "self._supabase" app/services/draft_store.py`) before choosing; the acceptance criterion is all 7 draft tests pass with zero network.

- [ ] **Step 5: Kill the paid-API test + offline guarantee**

In `tests/test_modules.py::test_coach_module_returns_message`, mock the LLM: `monkeypatch` `route_llm_call` (imported inside `app/modules/coach.py`) with an async fake returning a canned coaching string. Then verify the whole suite is offline: run once with Wi-Fi off or `python -m pytest tests/ -q` while watching for any `[LiteLLM]` cloud log line; grep test output for `gemini` — zero hits expected.

- [ ] **Step 6: Deps**

```bash
source .venv/bin/activate && pip install chromadb
```

In `requirements.txt`: add `chromadb>=0.5.0` and raise `langgraph>=0.4.0` to the actually-tested floor `langgraph>=1.2.0`.

- [ ] **Step 7: Full suite green + commit**

Run: `python -m pytest tests/ -q`
Expected: 0 failed.

```bash
git add tests/ requirements.txt
git commit -m "test: green suite — graph fixtures, config mock targets, in-memory draft stores, no paid API calls"
```

---

### Task 5: Supabase restoration verification + migration consolidation

**BLOCKED ON USER:** Madhav must unpause/restore the Supabase project at supabase.com first. If still unreachable when you get here, do Step 1, report, and skip to Task 6 (nothing else in this plan hard-depends on live Supabase thanks to Task 1).

**Files:**
- Move: `migrations/2026-05-01_pearl_scheduled_hour.sql` → `supabase/migrations/`
- Move: `migrations/2026-05-01_task_workspaces.sql` → `supabase/migrations/`

**Interfaces:**
- Produces: reachable Supabase with all 7 migrations applied; empty top-level `migrations/` removed.

- [ ] **Step 1: Verify reachability**

```bash
python - <<'EOF'
import os, httpx
from dotenv import load_dotenv; load_dotenv()
url = os.environ["SUPABASE_URL"]
try:
    r = httpx.get(f"{url}/rest/v1/", headers={"apikey": os.environ["SUPABASE_SERVICE_KEY"]}, timeout=10)
    print("REACHABLE", r.status_code)
except Exception as e:
    print("UNREACHABLE", e)
EOF
```

Expected: `REACHABLE 200` (or 401 — reachable but auth-scoped is fine). If UNREACHABLE: stop, report to user, continue with Task 6.

- [ ] **Step 2: Consolidate migrations**

```bash
git mv migrations/2026-05-01_pearl_scheduled_hour.sql supabase/migrations/20260501000001_pearl_scheduled_hour.sql
git mv migrations/2026-05-01_task_workspaces.sql supabase/migrations/20260501000002_task_workspaces.sql
rmdir migrations
```

(rename to the `supabase/migrations/` timestamp convention already used there — check existing filenames and match.)

- [ ] **Step 3: Apply the two migrations**

If the `supabase` CLI is linked: `supabase db push`. Otherwise apply each SQL file through the dashboard SQL editor (paste contents) — confirm `task_workspaces` and the PEARL column exist by querying `information_schema.tables`/`columns` via the same httpx pattern as Step 1.

- [ ] **Step 4: Live smoke + commit**

Start `uvicorn app.main:app --port 8765`, `curl localhost:8765/health` → expect `{"status":"healthy",...}` now; `POST /api/v1/chat/v2/stream` with `{"user_prompt":"hi","user_id":"demo"}` → expect SSE fast-path response, no 500. Kill server.

```bash
git add -A migrations supabase/migrations
git commit -m "chore: consolidate May migrations into supabase/migrations, verify restored project"
```

---

### Task 6: Serializable JarvisState — user_id in state, UserModel hydrated in load_context

Prereq for the checkpointer (Task 7): `JarvisState` currently carries the non-serializable `UserModel` object — the documented reason checkpointing was skipped (`main.py:65-67`). Move to `user_id` + per-invocation hydration.

**Files:**
- Modify: `app/orchestrator/state.py` (add `user_id: str`)
- Modify: `app/orchestrator/graph.py` (`_load_context`, ~line 92)
- Modify: `app/api/v1/endpoints/chat.py` (v2 `initial_state`, ~line 1126)
- Test: `tests/test_orchestrator.py` (add)

**Interfaces:**
- Produces: `JarvisState` gains `user_id: str`; `_load_context(state) -> dict` returns `{"user_model": <UserModel>}` built from `state["user_id"]` when `state.get("user_model")` is None. `chat_stream_v2` keeps passing the pre-built `user_model` (it needs the shared `db_client`/`memory_store` wiring) AND sets `user_id` — hydration is the fallback for checkpoint-resumed turns where `user_model` was dropped.
- Consumes: `UserModel(user_id=..., db=...)` from `app/core/user_model.py`; app-state clients via a module-level accessor.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_orchestrator.py
import pytest


@pytest.mark.asyncio
async def test_load_context__hydrates_user_model_from_user_id():
    from app.orchestrator.graph import _load_context
    state = {"user_id": "u42", "user_model": None}
    result = await _load_context(state)
    assert result["user_model"] is not None
    assert result["user_model"].user_id == "u42"


@pytest.mark.asyncio
async def test_load_context__keeps_existing_user_model():
    from app.orchestrator.graph import _load_context

    class Prebuilt:
        user_id = "u42"

    state = {"user_id": "u42", "user_model": Prebuilt()}
    result = await _load_context(state)
    assert isinstance(result.get("user_model", state["user_model"]), Prebuilt) or result == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_orchestrator.py -k load_context -v`
Expected: FAIL — `_load_context` is currently a no-op returning `{}`.

- [ ] **Step 3: Implement**

`app/orchestrator/state.py` — add to `JarvisState` right above `user_model`:

```python
    # Serializable identity — the checkpointer persists this, never the facade
    user_id: str
```

`app/orchestrator/graph.py` — replace the no-op `_load_context`:

```python
async def _load_context(state: JarvisState) -> dict:
    """Hydrate the UserModel facade for checkpoint-resumed turns.

    Live requests pass a pre-wired user_model (shared db/memory clients);
    resumed turns have only user_id — rebuild the facade here.
    """
    if state.get("user_model") is not None:
        return {}
    from app.core.user_model import UserModel
    return {"user_model": UserModel(user_id=state["user_id"], db=None)}
```

`app/api/v1/endpoints/chat.py` v2 `initial_state` — add `"user_id": request.user_id,` as the first key.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator/state.py app/orchestrator/graph.py app/api/v1/endpoints/chat.py tests/test_orchestrator.py
git commit -m "feat: serializable JarvisState — user_id in state, UserModel hydrated in load_context"
```

---

### Task 7: SQLite checkpointer + persisted negotiation state

Compile the orchestrator with `AsyncSqliteSaver`, thread per chat session, and stop hardcoding `negotiation_state`/`conversation_phase` at request entry — load them from the checkpoint. This revives `check_negotiation_shortcut` (`routing.py:36-40`) with zero changes to it.

**Files:**
- Modify: `app/main.py:64-67` (graph compile)
- Modify: `app/api/v1/endpoints/chat.py` (v2: pass `thread_id`, stop resetting phase fields; strip non-serializable keys from checkpointed state)
- Modify: `app/orchestrator/graph.py` (mark `user_model`, `progress_callback`, `progress_queue` as transient)
- Test: `tests/test_checkpointer.py` (create)

**Interfaces:**
- Consumes: `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` (verify import path against installed langgraph 1.2.x: `python -c "from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver"`; if missing, `pip install langgraph-checkpoint-sqlite` and add to requirements).
- Produces: `app.state.jarvis_graph` compiled with checkpointer at `data/checkpoints.sqlite`; graph invocations pass `config={"configurable": {"thread_id": session_id}}`. Non-serializable state keys (`user_model`, `progress_callback`, `progress_queue`) are excluded via a custom serde or set to None before write — decide by testing; the simple robust approach: register these three keys with a reducer that always keeps the latest in-memory value and configure the saver's serializer to drop them (`SerializerProtocol` wrapper that pops the keys pre-serialize).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_checkpointer.py
"""Negotiation state must survive across graph invocations on one thread."""
import pytest


@pytest.mark.asyncio
async def test_negotiation_state__persists_across_turns(tmp_path):
    from app.orchestrator.graph import build_jarvis_graph
    from app.orchestrator.state import NegotiationPhase
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build_jarvis_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "sess-1"}}
        state1 = _minimal_state("plan my exam prep")  # helper below
        state1["negotiation_state"] = NegotiationPhase.REVIEWING
        await graph.ainvoke(state1, config=cfg)
        # Second turn: do NOT pass negotiation_state — must come from checkpoint
        loaded = await graph.aget_state(cfg)
        assert loaded.values["negotiation_state"] == NegotiationPhase.REVIEWING


def _minimal_state(msg: str) -> dict:
    return {
        "user_id": "u1", "user_model": None, "user_message": msg,
        "file_base64": None, "file_media_type": None, "file_name": None,
        "brain_dump": None, "intent": None, "initiated_by": "user",
        "execution_graph": None, "schedule": None, "draft_response": None,
        "research_results": None, "ingestion_result": None,
        "clarification_request": None, "thinking_process": None,
        "response_message": None, "modules_invoked": [],
        "needs_followup": False, "needs_consent": None, "error": None,
        "conversation_history": [], "memory_context": "",
        "progress_callback": None, "progress_queue": None,
        "trivial_input": None, "force_cloud_request": None,
    }
```

(Mock `route_llm_call` in this test via the conftest pattern from Task 4 so no LLM is hit; the "hi"-class trivial path also avoids LLMs — using `"plan my exam prep"` requires the planning mocks, so if that's heavy, switch the message to `"hi"` — the assertion only concerns checkpoint round-trip.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_checkpointer.py -v`
Expected: FAIL — `build_jarvis_graph()` takes no `checkpointer` argument today.

- [ ] **Step 3: Implement**

`app/orchestrator/graph.py` — change signature and compile call:

```python
def build_jarvis_graph(checkpointer=None):
    ...
    return workflow.compile(checkpointer=checkpointer) if checkpointer else workflow.compile()
```

Serialization of transient keys: wrap the saver's serde to drop the three live objects:

```python
# app/orchestrator/checkpoint.py  (new, ~30 lines)
"""SqliteSaver wiring — drops non-serializable per-turn objects before write."""
_TRANSIENT_KEYS = ("user_model", "progress_callback", "progress_queue")


def scrub_transients(values: dict) -> dict:
    return {k: (None if k in _TRANSIENT_KEYS else v) for k, v in values.items()}
```

Preferred integration (try first, it's simplest): keep the standard saver, and scrub in `chat.py` — since `chat_stream_v2` builds `initial_state` fresh each request, the only writes come from graph super-steps; test whether `AsyncSqliteSaver` chokes on the callable/queue values. If it does (expected), subclass:

```python
class ScrubbingSqliteSaver(AsyncSqliteSaver):
    async def aput(self, config, checkpoint, metadata, new_versions):
        if "channel_values" in checkpoint:
            checkpoint = {**checkpoint,
                          "channel_values": scrub_transients(dict(checkpoint["channel_values"]))}
        return await super().aput(config, checkpoint, metadata, new_versions)
```

`app/main.py` lifespan — replace the "no checkpointer" block:

```python
    from app.orchestrator.checkpoint import ScrubbingSqliteSaver
    import aiosqlite, os
    os.makedirs("data", exist_ok=True)
    _ckpt_conn = await aiosqlite.connect("data/checkpoints.sqlite")
    app.state._ckpt_saver = ScrubbingSqliteSaver(conn=_ckpt_conn)
    app.state.jarvis_graph = build_jarvis_graph(checkpointer=app.state._ckpt_saver)
```

(and close `_ckpt_conn` in lifespan shutdown; add `data/` to `.gitignore`).

`app/api/v1/endpoints/chat.py` v2: invoke with the session thread —

```python
    graph_config = {"configurable": {"thread_id": session_id}}
    ...
    async for event in jarvis_graph.astream(initial_state, config=graph_config):
```

and **delete** the hardcoded resets — remove `"conversation_phase": ConversationPhase.GREETING,` and `"negotiation_state": NegotiationPhase.NONE,` from `initial_state`; instead set them only when the thread has no checkpoint yet:

```python
    existing = await jarvis_graph.aget_state(graph_config)
    if not existing or not existing.values:
        initial_state["conversation_phase"] = ConversationPhase.GREETING
        initial_state["negotiation_state"] = NegotiationPhase.NONE
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_checkpointer.py tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator/ app/main.py app/api/v1/endpoints/chat.py .gitignore tests/test_checkpointer.py
git commit -m "feat: SQLite checkpointer — negotiation/conversation state survives across turns"
```

---

### Task 8: v2 planning uses run_schedule (TMT + adaptive cap + bio fallback for free)

Replace `solve_schedule`'s raw `JarvisScheduler` usage (`planning_graph.py:187-227`) with the reusable `run_schedule()` (`app/api/v1/endpoints/schedule.py:193`) that v1 uses — it already applies `_compute_tmt_priority`, `compute_adaptive_daily_cap`, hard/soft blocks, and biological fallback. Also adopt v1's longer horizon ladder.

**Files:**
- Modify: `app/modules/planning_graph.py` (`solve_schedule`, `handle_infeasible`)
- Test: `tests/test_planning_graph.py` (create or extend existing planning tests — check `ls tests/ | grep -i plan`)

**Interfaces:**
- Consumes: `run_schedule(execution_graph: ExecutionGraph, daily_context: list[TimeSlot], horizon_minutes: int, horizon_start: str | None, ...) -> ScheduleResponse` — raises `HTTPException(422)` on INFEASIBLE. `ExecutionGraph`, `TaskChunk` from `app/schemas/context.py` / `app/api/v1/endpoints/reasoning.py` (match `decompose_goal`'s existing import).
- Produces: `solve_schedule(state) -> dict` returning `{"schedule": <ScheduleResponse.model_dump()>, "error": None, "horizon_start": iso_str}` or `{"schedule": None, "error": "INFEASIBLE"}`; `handle_infeasible` ladder becomes `[4320, 7200, 10080, 20160, 43200]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_planning_graph.py (append or create with existing conftest fixtures)
import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
async def test_solve_schedule__delegates_to_run_schedule_with_tmt():
    from app.modules.planning_graph import solve_schedule
    fake_resp = MagicMock()
    fake_resp.model_dump.return_value = {"schedule": {"t1": {"start_min": 0, "end_min": 25}}}
    with patch("app.api.v1.endpoints.schedule.run_schedule", return_value=fake_resp) as m:
        state = {
            "task_chunks": [{"task_id": "t1", "title": "read", "duration_minutes": 25,
                             "difficulty_weight": 0.5, "dependencies": [],
                             "completion_criteria": "done", "deadline_hint": None}],
            "time_slots": [], "horizon_minutes": 2880, "user_id": "u1",
        }
        result = await solve_schedule(state)
    m.assert_called_once()
    assert result["error"] is None
    assert result["schedule"] is not None


@pytest.mark.asyncio
async def test_solve_schedule__422_maps_to_infeasible():
    from fastapi import HTTPException
    from app.modules.planning_graph import solve_schedule
    with patch("app.api.v1.endpoints.schedule.run_schedule",
               side_effect=HTTPException(status_code=422, detail="INFEASIBLE")):
        state = {"task_chunks": [{"task_id": "t1", "title": "x", "duration_minutes": 25,
                                  "difficulty_weight": 0.5, "dependencies": [],
                                  "completion_criteria": "done", "deadline_hint": None}],
                 "time_slots": [], "horizon_minutes": 2880, "user_id": "u1"}
        result = await solve_schedule(state)
    assert result["error"] == "INFEASIBLE"


def test_handle_infeasible__ladder_matches_v1():
    from app.modules.planning_graph import HORIZON_RETRY_SEQUENCE
    assert HORIZON_RETRY_SEQUENCE == [4320, 7200, 10080, 20160, 43200]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_planning_graph.py -v`
Expected: FAIL — run_schedule not called; ladder is `[4320, 7200]` local variable, not module constant.

- [ ] **Step 3: Implement**

Rewrite `solve_schedule` in `planning_graph.py`:

```python
HORIZON_RETRY_SEQUENCE = [4320, 7200, 10080, 20160, 43200]  # v1 parity (48h → 30d)


async def solve_schedule(state: PlanningState) -> dict:
    """Delegate to the reusable run_schedule — TMT priority, adaptive cap,
    hard/soft blocks and biological sleep fallback all live there (never copy)."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from app.api.v1.endpoints import schedule as schedule_ep
    from app.api.v1.endpoints.reasoning import ExecutionGraph
    from app.schemas.context import TaskChunk, TimeSlot

    chunks = state.get("task_chunks", [])
    if not chunks:
        return {"error": "No tasks to schedule", "schedule": None}
    graph = ExecutionGraph(
        goal_metadata=None,
        decomposition=[TaskChunk.model_validate(c) for c in chunks],
        cognitive_load_estimate=0.5,
    )
    daily_context = [TimeSlot.model_validate(s) for s in state.get("time_slots", [])]
    horizon = state.get("horizon_minutes", 2880)
    horizon_start = datetime.now(timezone.utc).isoformat()
    try:
        resp = schedule_ep.run_schedule(
            graph, daily_context,
            horizon_minutes=horizon, horizon_start=horizon_start,
        )
    except HTTPException as exc:
        if exc.status_code == 422:
            return {"schedule": None, "error": "INFEASIBLE",
                    "_tool_detail": {"status": "INFEASIBLE", "horizon_h": horizon // 60}}
        raise
    return {
        "schedule": resp.model_dump(mode="json"),
        "horizon_start": horizon_start,
        "error": None,
        "_tool_detail": {"status": "OPTIMAL", "task_count": len(chunks),
                         "horizon_h": horizon // 60, "tmt_applied": True},
    }
```

**Verify signatures first** — `sed -n '193,260p' app/api/v1/endpoints/schedule.py` for `run_schedule`'s exact parameters and whether `ExecutionGraph.goal_metadata=None` validates (if not, build a minimal `GoalMetadata`); `grep -n "class TimeSlot" app/schemas/context.py` for field names vs the slot dicts (`start_min`/`end_min`/`availability`/`name` — map `minimal_work` slots' extra keys if `TimeSlot` lacks them, falling back to passing dicts if `run_schedule` accepts them). Adjust the implementation to the real signatures — the tests define the contract.

Update `handle_infeasible` to use the module constant:

```python
async def handle_infeasible(state: PlanningState) -> dict:
    retry_count = state.get("retry_count", 0)
    if retry_count < len(HORIZON_RETRY_SEQUENCE):
        return {"horizon_minutes": HORIZON_RETRY_SEQUENCE[retry_count],
                "retry_count": retry_count + 1, "error": None}
    return {
        "error": "INFEASIBLE_EXHAUSTED",
        "clarification_request": (
            "I couldn't fit everything in even with a 30-day window. "
            "This is a scope problem, not a you problem. "
            "Want to reduce scope or extend the deadline?"
        ),
    }
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_planning_graph.py tests/test_scheduler.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/modules/planning_graph.py tests/test_planning_graph.py
git commit -m "feat: v2 planning delegates to run_schedule — TMT, adaptive cap, bio fallback, full horizon ladder"
```

---

### Task 9: Draft creation in the v2 planning graph

After a feasible solve, create a draft exactly like v1 — so `chat.py:1413`'s `draft_id` stops being a lie. Persistence does NOT happen here (only on accept — Task 10).

**Files:**
- Modify: `app/modules/planning_graph.py` (new `create_draft` step; `PlanningState` + `planning_state_out`)
- Modify: `app/api/v1/endpoints/chat.py` (pass `draft_store` into state; read `draft_id` from final state)
- Test: `tests/test_planning_graph.py` (append)

**Interfaces:**
- Consumes: `DraftStore.create_draft(user_id, tasks: list, horizon_start: str, goal_id=None) -> dict | None` (`app/services/draft_store.py:50` — returns dict with `draft_id`; verify key name with `sed -n '50,60p' app/services/draft_store.py`).
- Produces: `PlanningState` gains `draft_store: Any` and `draft_id: Optional[str]`; new step `create_draft` runs after `solve_schedule` on the OPTIMAL branch; `planning_state_out` returns `"draft_id"`; `JarvisState` gains `draft_id: Optional[str]` (add to `state.py`), and `chat_stream_v2`'s payload uses `final_state.get("draft_id")` (already does — now it's actually set).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_create_draft__stores_tasks_and_sets_draft_id():
    from app.modules.planning_graph import create_draft

    class FakeDraftStore:
        def __init__(self):
            self.calls = []
        def create_draft(self, user_id, tasks, horizon_start, goal_id=None):
            self.calls.append((user_id, tasks, horizon_start))
            return {"draft_id": "d-123"}

    store = FakeDraftStore()
    state = {"user_id": "u1", "draft_store": store,
             "schedule": {"schedule": {"t1": {"start_min": 0}}},
             "horizon_start": "2026-08-08T00:00:00+00:00",
             "task_chunks": [{"task_id": "t1", "title": "read"}]}
    result = await create_draft(state)
    assert result["draft_id"] == "d-123"
    assert store.calls[0][0] == "u1"


@pytest.mark.asyncio
async def test_create_draft__no_store__returns_none_draft_id():
    from app.modules.planning_graph import create_draft
    state = {"user_id": "u1", "draft_store": None,
             "schedule": {"schedule": {}}, "task_chunks": []}
    result = await create_draft(state)
    assert result["draft_id"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_planning_graph.py -k create_draft -v`
Expected: FAIL — `create_draft` doesn't exist.

- [ ] **Step 3: Implement**

In `planning_graph.py`:

```python
async def create_draft(state: PlanningState) -> dict:
    """v1 parity: schedule is returned as a DRAFT for user review.
    _persist_fused_tasks runs only on accept — never here."""
    draft_store = state.get("draft_store")
    if not draft_store or not state.get("schedule"):
        return {"draft_id": None}
    try:
        draft = draft_store.create_draft(
            user_id=state.get("user_id", "demo"),
            tasks=state.get("task_chunks", []),
            horizon_start=state.get("horizon_start"),
        )
        draft_id = draft.get("draft_id") if draft else None
        return {"draft_id": draft_id, "_tool_detail": {"draft_id": draft_id}}
    except Exception as e:
        logger.warning(f"Draft creation failed (non-fatal): {e}")
        return {"draft_id": None}
```

`PlanningState`: add `draft_store: Any`, `draft_id: Optional[str]`, `horizon_start: Optional[str]`. `planning_state_in`: add `"draft_store": state.get("draft_store"), "draft_id": None, "horizon_start": None`. `planning_state_out`: add `"draft_id": result.get("draft_id"),`.

Module wiring — reroute solve's OPTIMAL branch through the new step:

```python
        ModuleStep(name="solve_schedule", handler=solve_schedule,
                   depends_on=["fuse_tasks"],
                   routes_to={check_feasibility: {"OPTIMAL": "create_draft", "INFEASIBLE": "handle_infeasible"}}),
        ModuleStep(name="create_draft", handler=create_draft),
```

`app/orchestrator/state.py`: add `draft_id: Optional[str]` and `draft_store: Any` to `JarvisState` (draft_store is transient — add it to `_TRANSIENT_KEYS` in `app/orchestrator/checkpoint.py` from Task 7). `chat.py` v2 `initial_state`: add `"draft_store": getattr(http_request.app.state, "draft_store", None), "draft_id": None`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_planning_graph.py tests/test_orchestrator.py -v`
Expected: PASS (graph rebuild validates the new edge — the conftest autouse fixture from Task 4 re-registers modules; if the registry caches compiled graphs, call its invalidation method — `grep -n "invalidate\|cache" app/core/module_framework.py`).

- [ ] **Step 5: Commit**

```bash
git add app/modules/planning_graph.py app/orchestrator/state.py app/orchestrator/checkpoint.py app/api/v1/endpoints/chat.py tests/test_planning_graph.py
git commit -m "feat: v2 planning creates a real draft — draft_id no longer fabricated"
```

---

### Task 10: v2 intent coverage — draft accept/reject/edit reach the graph

`_classify_intent` (`graph.py:135-165`) never emits `ACCEPT_DRAFT`/`REJECT_DRAFT`/`EDIT_TASK`/`REARRANGE`, making `routing.py:8-12`'s entries unreachable. Add rule-based detection (these are short, imperative messages — rules beat an LLM call here) and a handler node that performs accept-with-persistence.

**Files:**
- Modify: `app/orchestrator/graph.py` (`_classify_intent`; new `handle_draft_action` node + routing)
- Modify: `app/orchestrator/routing.py` (point the 4 intents at `draft_action`)
- Test: `tests/test_orchestrator.py` (append)

**Interfaces:**
- Consumes: `DraftStore.accept_draft(draft_id, user_id) -> bool`, `.reject_draft(draft_id, user_id, reason=None) -> bool`, `.edit_task_in_draft(draft_id, user_id, task_id, edits) -> dict | None`, `.get_pending_draft(user_id) -> dict | None`; `_persist_fused_tasks(user_id, chunks, supabase_client, schedule=None, horizon_start=None)` from `control_policy.py:312`.
- Produces: `_classify_intent` returns the 4 new intents when `negotiation_state` is `REVIEWING`/`PROPOSED` and the message matches accept/reject/edit patterns; node `handle_draft_action(state) -> dict` sets `response_message`, updates `negotiation_state` (`ACCEPTED`/`NONE`), and on accept calls `_persist_fused_tasks` with the draft's tasks.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_orchestrator.py
from app.orchestrator.state import NegotiationPhase


def test_classify__accept_during_review__accept_draft():
    from app.orchestrator.graph import _classify_intent
    state = {"user_message": "accept", "negotiation_state": NegotiationPhase.REVIEWING,
             "brain_dump": None}
    assert _classify_intent_value(_classify_intent(state)) == "ACCEPT_DRAFT"


def test_classify__reject_during_review__reject_draft():
    from app.orchestrator.graph import _classify_intent
    state = {"user_message": "no, scrap that plan", "negotiation_state": NegotiationPhase.REVIEWING,
             "brain_dump": None}
    assert _classify_intent_value(_classify_intent(state)) == "REJECT_DRAFT"


def test_classify__accept_without_active_negotiation__not_accept():
    from app.orchestrator.graph import _classify_intent
    state = {"user_message": "accept", "negotiation_state": NegotiationPhase.NONE,
             "brain_dump": None}
    assert _classify_intent_value(_classify_intent(state)) != "ACCEPT_DRAFT"


def _classify_intent_value(res):
    # _classify_intent may return an enum, a string, or a dict {"intent": ...} —
    # normalize; check the real return shape at graph.py:135 and simplify this.
    if isinstance(res, dict):
        res = res.get("intent")
    return res.value if hasattr(res, "value") else str(res)
```

(**Check first** how `_classify_intent` is invoked and what it returns — `sed -n '130,170p' app/orchestrator/graph.py` — and align the tests to the real shape. Also verify `IntentType` in `app/schemas/context.py` contains `ACCEPT_DRAFT`/`REJECT_DRAFT`/`EDIT_TASK`/`REARRANGE` members — `grep -n "class IntentType" -A 20 app/schemas/context.py`; add missing members to the enum.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_orchestrator.py -k classify -v`
Expected: FAIL — new intents never returned.

- [ ] **Step 3: Implement classification**

At the top of `_classify_intent`, before existing rules:

```python
    # Draft negotiation verbs — only meaningful while a draft is under review
    neg = state.get("negotiation_state")
    if neg in (NegotiationPhase.PROPOSED, NegotiationPhase.REVIEWING, NegotiationPhase.EDITING):
        msg = state.get("user_message", "").lower().strip()
        if re.search(r"\b(accept|approve|confirm|looks good|lgtm|yes,? (do|go|lock) (it|that))\b", msg):
            return IntentType.ACCEPT_DRAFT
        if re.search(r"\b(reject|scrap|discard|cancel (the )?(plan|draft)|start over|no,? redo)\b", msg):
            return IntentType.REJECT_DRAFT
        if re.search(r"\b(move|shift|push|swap|rearrange|reorder)\b", msg):
            return IntentType.REARRANGE
        if re.search(r"\b(edit|change|rename|shorten|extend|make .* (longer|shorter))\b", msg):
            return IntentType.EDIT_TASK
```

(match the function's actual return style — if it returns route-name strings, return the strings `routing.py` maps.)

- [ ] **Step 4: Implement `handle_draft_action` node**

In `graph.py`:

```python
async def handle_draft_action(state: JarvisState) -> dict:
    """Accept/reject/edit the pending draft. Accept = the ONLY place v2 persists tasks."""
    from app.services.analytical.control_policy import _persist_fused_tasks
    from app.schemas.context import TaskChunk

    draft_store = state.get("draft_store")
    user_id = state.get("user_id", "demo")
    intent = state.get("intent")
    if not draft_store:
        return {"response_message": "I don't have a draft system available right now, sir.",
                "negotiation_state": NegotiationPhase.NONE}
    draft = draft_store.get_pending_draft(user_id)
    if not draft:
        return {"response_message": "There's no draft awaiting review, sir.",
                "negotiation_state": NegotiationPhase.NONE}

    intent_val = intent.value if hasattr(intent, "value") else str(intent)
    if intent_val == "ACCEPT_DRAFT":
        draft_store.accept_draft(draft["draft_id"], user_id)
        chunks = [TaskChunk.model_validate(t) for t in draft.get("tasks", [])]
        supabase = state["user_model"].db.supabase if state.get("user_model") and getattr(state["user_model"], "db", None) else None
        _persist_fused_tasks(user_id, chunks, supabase,
                             schedule=draft.get("schedule"),
                             horizon_start=draft.get("horizon_start"))
        return {"response_message": "Locked in. Your schedule is live, sir.",
                "negotiation_state": NegotiationPhase.ACCEPTED, "draft_id": None}
    if intent_val == "REJECT_DRAFT":
        draft_store.reject_draft(draft["draft_id"], user_id)
        return {"response_message": "Draft discarded. Tell me what to aim for instead, sir.",
                "negotiation_state": NegotiationPhase.NONE, "draft_id": None}
    # EDIT_TASK / REARRANGE: keep negotiation open; v2 minimal viable edit — route
    # the message through planning again with the draft context (full NL edit is Spec 3)
    return {"response_message": "Which task should I adjust, sir? You can also accept or reject the draft.",
            "negotiation_state": NegotiationPhase.EDITING}
```

Register the node + edges where the other terminal nodes are wired (follow `graph.py:168-220`'s pattern: `workflow.add_node("draft_action", handle_draft_action)` and route the 4 intents to `"draft_action"` → synthesis/END, matching how CHAT routes). Update `routing.py:8-12`'s `INTENT_TO_MODULE` so all four map to `"draft_action"`. Verify the draft dict's actual keys (`tasks`, `schedule`, `horizon_start`, `draft_id`) against `sed -n '50,80p' app/services/draft_store.py` and adapt.

Also set `negotiation_state` when planning creates a draft: in `planning_state_out` (Task 9), when `draft_id` is set, the orchestrator should transition — add to `handle_draft_action`'s sibling: in `graph.py`, after planning module returns, if `final draft_id`, set `negotiation_state = NegotiationPhase.REVIEWING` (do this in the synthesis node or a small `post_planning` update — wherever module output merges into `JarvisState`; check `module_wrapper.py:41-56` `state_out` merge and add `"negotiation_state": NegotiationPhase.REVIEWING` to `planning_state_out` when a draft was created).

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_orchestrator.py tests/test_planning_graph.py -v`
Expected: PASS.

- [ ] **Step 6: End-to-end negotiation test**

```python
@pytest.mark.asyncio
async def test_accept_flow__persists_draft_tasks(monkeypatch):
    """ACCEPT_DRAFT with a pending draft → _persist_fused_tasks called."""
    from app.orchestrator import graph as graph_mod
    calls = {}
    monkeypatch.setattr(
        "app.services.analytical.control_policy._persist_fused_tasks",
        lambda user_id, chunks, sb, schedule=None, horizon_start=None: calls.update(
            {"user_id": user_id, "n": len(chunks)}),
    )

    class FakeDraftStore:
        def get_pending_draft(self, user_id):
            return {"draft_id": "d1", "tasks": [{"task_id": "t1", "title": "read",
                    "duration_minutes": 25, "difficulty_weight": 0.5,
                    "dependencies": [], "completion_criteria": "done"}],
                    "schedule": None, "horizon_start": None}
        def accept_draft(self, draft_id, user_id):
            return True

    state = _minimal_state("accept")  # reuse Task 7 helper — move it to conftest.py
    state["draft_store"] = FakeDraftStore()
    state["intent"] = None
    state["negotiation_state"] = NegotiationPhase.REVIEWING
    from app.orchestrator.state import IntentType  # or app.schemas.context
    state["intent"] = "ACCEPT_DRAFT"
    result = await graph_mod.handle_draft_action(state)
    assert calls["n"] == 1
    assert result["negotiation_state"] == NegotiationPhase.ACCEPTED
```

Run: `python -m pytest tests/test_orchestrator.py -k accept_flow -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add app/orchestrator/ app/schemas/context.py tests/
git commit -m "feat: draft accept/reject/edit intents live on v2 — persistence on accept only"
```

---

### Task 11: Real token streaming in v2 synthesis

v2 emits the whole message as one token (`chat.py:1293,1308` — generation metrics report `total_tokens: 1`). Stream the synthesis output incrementally.

**Files:**
- Modify: `app/api/v1/endpoints/chat.py` (v2 `event_gen` token emission)
- Test: manual SSE verification (streaming through LangGraph events is integration-level; unit-test the chunker only)

**Interfaces:**
- Consumes: the existing `hybrid_route_query(..., stream=True)` async-generator path (`litellm_conf.py` — yields `("reasoning"|"content", token)` tuples) — check how the v1 `/chat/stream` endpoint consumes it (`sed -n '243,340p' app/api/v1/endpoints/chat.py`) and mirror.
- Produces: v2 SSE emits one `token` event per model chunk. Where synthesis happens inside the graph (voice_of_jarvis in the synthesis node), tokens must flow through the existing `progress_queue` bridge: the synthesis node pushes `{"_event_type": "token", "token": ...}` entries; `event_gen`'s drain loop forwards them as `event: token` frames.

- [ ] **Step 1: Locate the synthesis call**

`grep -n "voice_of_jarvis\|synthesize" app/orchestrator/graph.py app/modules/conversation.py` — find where the final message is generated in v2 and whether it already receives `progress_queue`.

- [ ] **Step 2: Write the chunk-forwarding test**

```python
# append to tests/test_orchestrator.py
def test_progress_queue__token_events_forwarded():
    """Synthesis pushes token events through the progress bridge."""
    import asyncio, json
    q = asyncio.Queue()
    from app.orchestrator.graph import make_token_emitter  # new helper
    emit = make_token_emitter(q)
    emit("Hel"); emit("lo")
    first = json.loads(q.get_nowait())
    assert first == {"_event_type": "token", "token": "Hel"}
```

Run: `python -m pytest tests/test_orchestrator.py -k token_events -v` → FAIL (helper missing).

- [ ] **Step 3: Implement**

`graph.py`:

```python
def make_token_emitter(progress_queue):
    """Bridge: synthesis streams tokens → SSE via the progress queue."""
    import json as _json
    def _emit(token: str) -> None:
        if progress_queue is not None:
            progress_queue.put_nowait(_json.dumps({"_event_type": "token", "token": token}))
    return _emit
```

In the synthesis node, switch its LLM call to `stream=True`, iterate the token generator, call `emit(token)` per content token, accumulate the full text for `response_message` (preserve `<think>` extraction into `thinking_process` — mirror how v1's streaming endpoint does it). In `event_gen`'s drain loop (chat.py:1171-1190 pattern), add before the `else` fallback:

```python
                        elif evt_type == "token":
                            yield f"event: token\ndata: {json_mod.dumps(parsed)}\n\n"
```

and delete/bypass the whole-message single-token emission at 1293/1308 when tokens were already streamed (guard with a `tokens_streamed` flag set when any token event passed through).

- [ ] **Step 4: Verify live**

Run server, `curl -N -X POST localhost:8765/api/v1/chat/v2/stream -H 'Content-Type: application/json' -d '{"user_prompt":"hi","user_id":"demo"}'` — fast path may legitimately emit one token; a real LLM turn (with LM Studio up or Gemini) must show many `event: token` frames. Kill server.

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator/graph.py app/api/v1/endpoints/chat.py tests/test_orchestrator.py
git commit -m "feat: real incremental token streaming on v2 — no more single-token messages"
```

---

### Task 12: Deprecate v1 + retire the frontend flag

**Files:**
- Modify: `app/api/v1/endpoints/chat.py` (v1 endpoint deprecation)
- Modify: `app/services/analytical/control_policy.py` (deprecation header comment)
- Modify: `jarvis-frontend/lib/api.ts:10-12` and `jarvis-frontend/.env.local` (remove `NEXT_PUBLIC_USE_V2` branching — v2 unconditional)
- Delete: `POST /test-chat` in `app/main.py:148` (leftover dev endpoint)

**Interfaces:**
- Produces: `/api/v1/chat` and `/api/v1/chat/stream` still respond (draft accept endpoints like `/confirm-schedule` remain live until Spec 3 migrates their UI), but carry `deprecated=True` in FastAPI route metadata and log a deprecation warning per call. Frontend always calls `/v2/stream`.

- [ ] **Step 1: Mark v1 routes deprecated**

On the two v1 route decorators: `@router.post("/", deprecated=True)` / `@router.post("/stream", deprecated=True)` and first line of each handler:

```python
    logger.warning("DEPRECATED endpoint hit — migrate caller to /api/v1/chat/v2/stream")
```

Top of `control_policy.py` docstring: append `DEPRECATED (2026-08-08): superseded by app/orchestrator/. Accept-schedule endpoints still call into _persist_fused_tasks; do not add features here.`

- [ ] **Step 2: Frontend — v2 unconditional**

In `jarvis-frontend/lib/api.ts`, replace the `NEXT_PUBLIC_USE_V2` conditional with the v2 path constant; remove the variable from `.env.local`. Run `cd ../jarvis-frontend && npx tsc --noEmit` → clean.

- [ ] **Step 3: Remove `/test-chat`**

Delete the route in `app/main.py:148` block.

- [ ] **Step 4: Tests + commit**

Run: `python -m pytest tests/ -q` → 0 failed. `cd ../jarvis-frontend && npm run build` → succeeds.

```bash
git add app/ ../jarvis-frontend/lib/api.ts
git commit -m "chore: deprecate v1 chat path, frontend always uses v2, remove /test-chat"
```

(`.env.local` is gitignored — just edit it.)

---

### Task 13: Doc truth pass

**Files:**
- Rewrite: `docs/PROJECT_STATUS.md`
- Modify: `docs/INDEX.md`, `docs/POLICY_ENGINE_ARCHITECTURE.md`, `docs/superpowers/specs/2026-04-05-claude-code-architecture-adaptation-design.md`, `.claude/CLAUDE.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: PROJECT_STATUS.md rewrite**

Rewrite against post-plan reality. Required content: v2 LangGraph orchestrator as THE path (v1 deprecated); current test count from `python -m pytest tests/ -q --collect-only | tail -1`; component inventory (implemented: orchestrator, ModuleStep framework, planning with drafts/TMT/persistence-on-accept, checkpointer, memory+PEARL+bridge, observation loop; stubbed: research/knowledge node internals, 5/7 hook events; deferred: DKT/RL/SARIMAX); known issues that remain (research module has no LLM; hooks tier system absent — pointer to upcoming Spec 3); storage map (Supabase tables + `data/checkpoints.sqlite` + ChromaDB cloud).

- [ ] **Step 2: Mark 04-05 spec superseded**

Add as line 3 of the 04-05 spec: `> **SUPERSEDED (2026-08-08):** implemented instead as the smaller ModuleStep framework — see 2026-04-13-module-step-framework-design.md. Kept for historical reference.`

- [ ] **Step 3: POLICY_ENGINE_ARCHITECTURE.md surgery**

Delete the pasted user prompt at lines 1156-1160 (verify content first — it starts "Architecture verification for Jarvis is required"). Add a banner after the title: `> **NOTE (2026-08-08):** This document describes the v1 pipeline, now deprecated. The live architecture is the LangGraph orchestrator — see PROJECT_STATUS.md and the 2026-04-12 v2 spec.` Fix the "Routing Behavior" contradiction (line ~746): replace the "Gemini 2.5 Flash is the primary model" paragraph with the real behavior: local Gemma via LM Studio auto-detect primary; Gemini is fallback and GEMINI_PRIMARY only when no local model is loaded.

- [ ] **Step 4: INDEX.md + CLAUDE.md refresh**

INDEX.md: point "master spec" to the 2026-04-12 v2 spec; add the 04-05 (marked superseded), 04-13 ×2, 05-01, and both 2026-08-08 specs to the spec table; delete the past-tense VC-pitch date reference. CLAUDE.md: fix the stale L2 row (Qwen → Gemma 4 via LM Studio auto-detect) and LLM Routing Rules table (Qwen → Gemma; add GEMINI_PRIMARY behavior), and add `app/orchestrator/` + `app/modules/` to the Repository Layout tree. Do not rewrite CLAUDE.md wholesale — targeted fixes only.

- [ ] **Step 5: Commit**

```bash
git add docs/ .claude/CLAUDE.md
git commit -m "docs: truth pass — v2 is the architecture, v1 marked deprecated, junk removed, INDEX/CLAUDE current"
```

---

### Task 14: End-to-end demo verification

**Files:** none (verification only; fixes go through the task they belong to).

- [ ] **Step 1: Full suite**

`python -m pytest tests/ -q` → 0 failed.

- [ ] **Step 2: DB-down resilience demo**

Temporarily set `SUPABASE_URL=https://dead.invalid` in the shell env (not `.env`), boot the server, POST "hi" to `/v2/stream` → SSE response, no 500. Unset, restart.

- [ ] **Step 3: The money demo (requires Supabase up; LM Studio or Gemini)**

Scripted SSE session against `/api/v1/chat/v2/stream`, same `conversation_id` throughout: (1) "prepare a 10-minute talk on graph algorithms by Sunday" → expect phase events, token stream, schedule payload, non-null `draft_id`; (2) "accept" → expect ACCEPT_DRAFT path, "Locked in" response; (3) verify rows: `user_tasks` has the persisted chunks (httpx REST query, user_id-filtered); (4) `POST /api/v1/tasks/{task_id}/complete` on one task → `replan_triggered: true`; (5) restart the server, send "accept" again on the same conversation → "There's no draft awaiting review" (checkpointer survived restart, draft correctly consumed).

- [ ] **Step 4: Record results**

Append a "Verified 2026-08-XX" section with the transcript summary to `docs/PROJECT_STATUS.md`; commit:

```bash
git add docs/PROJECT_STATUS.md
git commit -m "docs: record one-brain end-to-end verification"
```

---

## Plan Self-Review (completed)

- **Spec coverage:** 0.1→Task 5, 0.2→Task 1, 0.3→Task 2, 0.4→Task 3, 0.5/0.6→Task 4, 1.1→Task 9, 1.2→Tasks 9+10 (persist-on-accept), 1.3→Task 8, 1.4→Tasks 6+7, 1.5→Task 10, 1.6→Task 12, 1.7→Task 11, 1.8→Task 13. Error-handling section: DB-down (Task 1/14), LLM-down (Task 2), INFEASIBLE ladder (Task 8), checkpoint corruption — **covered inline in Task 7's saver (deserialize failure → fresh thread) — implementer: wrap `aget_state` in try/except in chat.py, fall back to fresh state.**
- **Placeholders:** none — every code step has concrete code; steps that depend on verifying real signatures say exactly which command reveals them and what the contract (test) is.
- **Type consistency:** `_UNSET` sentinel used identically in both stores; `HORIZON_RETRY_SEQUENCE` constant name consistent between Tasks 8 tests and implementation; `draft_id`/`draft_store`/`user_id` state keys consistent across Tasks 6, 7, 9, 10; `make_token_emitter` defined (11) where used (11); `_minimal_state` helper defined in Task 7, reused in Task 10 with a note to move it to conftest.
