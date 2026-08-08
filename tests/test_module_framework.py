"""Tests for the ModuleStep execution framework."""

import pytest


def test_module_step__defaults():
    from app.core.module_framework import ModuleStep

    step = ModuleStep(name="fetch_data", handler=lambda s: {})
    assert step.name == "fetch_data"
    assert step.depends_on == []
    assert step.concurrent_safe is False
    assert step.read_only is False
    assert step.routes_to is None
    assert step.timeout_ms == 30_000
    assert step.hook_event is None
    assert step.feature_flag is None
    assert step.module_name == ""


def test_module_definition__auto_detects_entry_step():
    from app.core.module_framework import ModuleStep, ModuleDefinition

    steps = [
        ModuleStep(name="start", handler=lambda s: {}),
        ModuleStep(name="middle", handler=lambda s: {}, depends_on=["start"]),
        ModuleStep(name="end", handler=lambda s: {}, depends_on=["middle"]),
    ]
    defn = ModuleDefinition(name="test", state_class=dict, steps=steps)
    assert defn.entry_step is None
    entry_candidates = [s for s in defn.steps if not s.depends_on]
    assert len(entry_candidates) == 1
    assert entry_candidates[0].name == "start"


def test_conditional_edge__fields():
    from app.core.module_framework import ConditionalEdge

    edge = ConditionalEdge(
        from_step="solve",
        condition=lambda s: "ok",
        destinations={"ok": "__END__", "fail": "retry"},
    )
    assert edge.from_step == "solve"
    assert edge.destinations["ok"] == "__END__"


import os


def test_is_feature_enabled__default_enabled():
    from app.core.config import is_feature_enabled
    os.environ.pop("JARVIS_ENABLE_PEARL", None)
    assert is_feature_enabled("ENABLE_PEARL") is True


def test_is_feature_enabled__explicitly_disabled(monkeypatch):
    from app.core.config import is_feature_enabled
    monkeypatch.setenv("JARVIS_ENABLE_PEARL", "0")
    assert is_feature_enabled("ENABLE_PEARL") is False


def test_is_feature_enabled__explicitly_enabled(monkeypatch):
    from app.core.config import is_feature_enabled
    monkeypatch.setenv("JARVIS_ENABLE_PEARL", "1")
    assert is_feature_enabled("ENABLE_PEARL") is True


import asyncio
import json


@pytest.mark.asyncio
async def test_wrap_step__emits_started_and_done():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def handler(state):
        return {"result": 42}

    step = ModuleStep(name="my_step", handler=handler, module_name="test_mod")
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {"result": 42}
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert len(events) == 2
    assert events[0]["status"] == "started"
    assert events[0]["tool"] == "my_step"
    assert events[0]["module"] == "test_mod"
    assert events[1]["status"] == "done"


@pytest.mark.asyncio
async def test_wrap_step__timeout_emits_error():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def slow_handler(state):
        await asyncio.sleep(5)
        return {}

    step = ModuleStep(name="slow", handler=slow_handler, module_name="test_mod", timeout_ms=100)
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {}
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert events[-1]["status"] == "error"
    assert events[-1]["detail"]["error"] == "timeout"


@pytest.mark.asyncio
async def test_wrap_step__feature_flag_disabled_skips(monkeypatch):
    from app.core.module_framework import ModuleStep, _wrap_step

    monkeypatch.setenv("JARVIS_ENABLE_TEST", "0")
    queue = asyncio.Queue()

    async def handler(state):
        return {"should_not": "run"}

    step = ModuleStep(name="gated", handler=handler, module_name="test_mod", feature_flag="ENABLE_TEST")
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {}
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert len(events) == 1
    assert events[0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_wrap_step__handler_exception_emits_error():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def bad_handler(state):
        raise ValueError("something broke")

    step = ModuleStep(name="bad", handler=bad_handler, module_name="test_mod")
    wrapped = _wrap_step(step)

    with pytest.raises(ValueError, match="something broke"):
        await wrapped({"progress_queue": queue})

    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert events[-1]["status"] == "error"
    assert "something broke" in events[-1]["detail"]["error"]


@pytest.mark.asyncio
async def test_wrap_step__no_queue_no_crash():
    from app.core.module_framework import ModuleStep, _wrap_step

    async def handler(state):
        return {"ok": True}

    step = ModuleStep(name="quiet", handler=handler, module_name="test_mod")
    wrapped = _wrap_step(step)
    result = await wrapped({})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_wrap_step__tool_detail_extracted():
    from app.core.module_framework import ModuleStep, _wrap_step

    queue = asyncio.Queue()

    async def handler(state):
        return {"data": [1, 2], "_tool_detail": {"rows": 2}}

    step = ModuleStep(name="detailed", handler=handler, module_name="test_mod")
    wrapped = _wrap_step(step)
    result = await wrapped({"progress_queue": queue})

    assert result == {"data": [1, 2]}  # _tool_detail stripped
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    assert events[1]["detail"] == {"rows": 2}


from typing import TypedDict, Optional, Any, Annotated


class SimpleState(TypedDict):
    value: int
    progress_queue: Any
    error: Optional[str]


@pytest.mark.asyncio
async def test_build_module_graph__linear_steps():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    call_order = []

    async def step_a(state):
        call_order.append("a")
        return {"value": state.get("value", 0) + 1}

    async def step_b(state):
        call_order.append("b")
        return {"value": state.get("value", 0) + 10}

    defn = ModuleDefinition(
        name="linear",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="a", handler=step_a),
            ModuleStep(name="b", handler=step_b, depends_on=["a"]),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"value": 0, "progress_queue": None, "error": None})

    assert call_order == ["a", "b"]
    assert result["value"] == 11


@pytest.mark.asyncio
async def test_build_module_graph__conditional_routes_to_end():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def check_value(state) -> str:
        return "done" if state.get("value", 0) > 5 else "continue"

    async def step_a(state):
        return {"value": 10}

    async def step_b(state):
        return {"value": state.get("value", 0) + 100}

    defn = ModuleDefinition(
        name="conditional",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="a", handler=step_a,
                       routes_to={check_value: {"done": "__END__", "continue": "b"}}),
            ModuleStep(name="b", handler=step_b),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"value": 0, "progress_queue": None, "error": None})

    assert result["value"] == 10


@pytest.mark.asyncio
async def test_build_module_graph__self_loop_terminates():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def should_loop(state) -> str:
        return "again" if state.get("value", 0) < 3 else "stop"

    async def increment(state):
        return {"value": state.get("value", 0) + 1}

    async def finalize(state):
        return {"error": None}

    defn = ModuleDefinition(
        name="looper",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="inc", handler=increment,
                       routes_to={should_loop: {"again": "inc", "stop": "fin"}}),
            ModuleStep(name="fin", handler=finalize),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"value": 0, "progress_queue": None, "error": None})

    assert result["value"] == 3


@pytest.mark.asyncio
async def test_build_module_graph__three_way_branch():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def route(state) -> str:
        v = state.get("value", 0)
        if v == 1:
            return "path_a"
        elif v == 2:
            return "path_b"
        return "path_c"

    async def classifier(state):
        return {}

    async def handler_a(state):
        return {"error": "took_a"}

    async def handler_b(state):
        return {"error": "took_b"}

    async def handler_c(state):
        return {"error": "took_c"}

    defn = ModuleDefinition(
        name="brancher",
        state_class=SimpleState,
        steps=[
            ModuleStep(name="classify", handler=classifier,
                       routes_to={route: {"path_a": "a", "path_b": "b", "path_c": "c"}}),
            ModuleStep(name="a", handler=handler_a),
            ModuleStep(name="b", handler=handler_b),
            ModuleStep(name="c", handler=handler_c),
        ],
    )
    graph = build_module_graph(defn)

    r1 = await graph.ainvoke({"value": 1, "progress_queue": None, "error": None})
    assert r1["error"] == "took_a"

    r2 = await graph.ainvoke({"value": 2, "progress_queue": None, "error": None})
    assert r2["error"] == "took_b"

    r3 = await graph.ainvoke({"value": 99, "progress_queue": None, "error": None})
    assert r3["error"] == "took_c"


@pytest.mark.asyncio
async def test_build_module_graph__parallel_fan_out_fan_in():
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    def _merge(left, right):
        return left + right

    class FanState(TypedDict):
        progress_queue: Any
        items: Annotated[list, _merge]
        error: Optional[str]

    async def start(state):
        return {"items": ["start"]}

    async def branch_a(state):
        return {"items": ["a"]}

    async def branch_b(state):
        return {"items": ["b"]}

    async def collect(state):
        return {"items": ["done"]}

    defn = ModuleDefinition(
        name="fanout",
        state_class=FanState,
        steps=[
            ModuleStep(name="start", handler=start),
            ModuleStep(name="branch_a", handler=branch_a,
                       depends_on=["start"], concurrent_safe=True),
            ModuleStep(name="branch_b", handler=branch_b,
                       depends_on=["start"], concurrent_safe=True),
            ModuleStep(name="collect", handler=collect,
                       depends_on=["branch_a", "branch_b"]),
        ],
    )
    graph = build_module_graph(defn)
    result = await graph.ainvoke({"progress_queue": None, "items": [], "error": None})

    assert "start" in result["items"]
    assert "a" in result["items"]
    assert "b" in result["items"]
    assert "done" in result["items"]


@pytest.mark.asyncio
async def test_module_registry__register_and_compile():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry

    async def noop(state):
        return {}

    defn = ModuleDefinition(
        name="test_mod",
        state_class=SimpleState,
        steps=[ModuleStep(name="only", handler=noop)],
    )
    registry = ModuleRegistry()
    registry.register(defn)

    assert "test_mod" in registry.registered_names()

    compiled = registry.get_compiled("test_mod")
    result = await compiled.ainvoke({"value": 0, "progress_queue": None, "error": None})
    assert isinstance(result, dict)


def test_module_registry__get_compiled_caches():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry

    async def noop(state):
        return {}

    defn = ModuleDefinition(
        name="cached",
        state_class=SimpleState,
        steps=[ModuleStep(name="only", handler=noop)],
    )
    registry = ModuleRegistry()
    registry.register(defn)

    c1 = registry.get_compiled("cached")
    c2 = registry.get_compiled("cached")
    assert c1 is c2


def test_module_registry__unknown_module_raises():
    from app.core.module_framework import ModuleRegistry

    registry = ModuleRegistry()
    with pytest.raises(KeyError, match="No module 'ghost'"):
        registry.get_compiled("ghost")


def test_module_registry__re_register_invalidates_cache():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry

    async def noop(state):
        return {}

    defn = ModuleDefinition(
        name="evolving",
        state_class=SimpleState,
        steps=[ModuleStep(name="v1", handler=noop)],
    )
    registry = ModuleRegistry()
    registry.register(defn)
    c1 = registry.get_compiled("evolving")

    defn2 = ModuleDefinition(
        name="evolving",
        state_class=SimpleState,
        steps=[ModuleStep(name="v2", handler=noop)],
    )
    registry.register(defn2)
    c2 = registry.get_compiled("evolving")
    assert c1 is not c2


@pytest.mark.asyncio
async def test_create_module_wrapper__invokes_module():
    from app.core.module_framework import ModuleStep, ModuleDefinition, ModuleRegistry
    from app.orchestrator.module_wrapper import create_module_wrapper

    executed = []

    async def my_handler(state):
        executed.append(True)
        return {"value": 99}

    defn = ModuleDefinition(
        name="wrapper_test",
        state_class=SimpleState,
        steps=[ModuleStep(name="only", handler=my_handler)],
        state_in=lambda s: {"value": 0, "progress_queue": None, "error": None},
        state_out=lambda r, n: {"schedule": r.get("value")},
    )

    registry = ModuleRegistry()
    registry.register(defn)

    wrapper = create_module_wrapper("wrapper_test", registry)
    result = await wrapper({
        "initiated_by": "user",
        "modules_invoked": [],
        "progress_queue": None,
    })

    assert executed == [True]
    assert result["modules_invoked"] == ["wrapper_test"]
    assert result.get("schedule") == 99


# ---------------------------------------------------------------------------
# Edge-wiring semantics (Task 9.5)
#
# `depends_on` is an AND-join. Three rules the builder must honour, each of
# which was violated by the original `if routes_to: ... elif depends_on and
# name not in routed_targets` branch:
#
#   1. routing OUT and depending IN are independent — a step may do both.
#   2. a step keeps its dependency edges even when some *other* step routes to
#      it (retry back-edges must not sever the forward path).
#   3. >=2 plain dependencies are ONE barrier, not N independent triggers.
#
# LangGraph facts these encode (measured, langgraph 1.2.10):
#   * `add_edge([a, b], c)` registers a `waiting_edge` — c fires once, after
#     BOTH a and b have completed, even at different depths.
#   * two separate `add_edge(a, c)` / `add_edge(b, c)` calls at different
#     depths fire c TWICE.
#   * a node that is both a conditional source to c and a member of a barrier
#     into c does NOT gate c — the barrier triggers c regardless of the
#     branch. Hence rule 4 below: that shape is rejected at build time.
# ---------------------------------------------------------------------------


def _log_state():
    """A state class whose `log` key accumulates every node that ran."""

    def _merge(left, right):
        return left + right

    class LogState(TypedDict):
        log: Annotated[list, _merge]
        gate: bool
        progress_queue: Any

    return LogState


def _recorder(name: str):
    async def handler(state):
        return {"log": [name]}

    return handler


@pytest.mark.asyncio
async def test_build_module_graph__step_with_routes_and_depends_on__gets_incoming_edge():
    """Defect (a): routing OUT must not cancel the dependency edge coming IN."""
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="both",
        state_class=LogState,
        steps=[
            ModuleStep(name="a", handler=_recorder("a")),
            ModuleStep(name="gate", handler=_recorder("gate"), depends_on=["a"],
                       routes_to={lambda s: "yes": {"yes": "b", "no": "__END__"}}),
            ModuleStep(name="b", handler=_recorder("b")),
        ],
    )
    result = await build_module_graph(defn).ainvoke(
        {"log": [], "gate": True, "progress_queue": None}
    )

    assert result["log"] == ["a", "gate", "b"]


@pytest.mark.asyncio
async def test_build_module_graph__dep_edge_survives_being_another_steps_route_target():
    """Defect (b): a retry back-edge must not sever the forward dependency.

    `b` is the retry target of `c`, which is exactly the
    handle_infeasible -> solve_schedule shape. The old `name not in
    routed_targets` guard dropped `a -> b`, leaving b reachable only from a
    node that was itself unreachable.
    """
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="retry",
        state_class=LogState,
        steps=[
            ModuleStep(name="a", handler=_recorder("a")),
            ModuleStep(name="b", handler=_recorder("b"), depends_on=["a"]),
            ModuleStep(name="c", handler=_recorder("c"), depends_on=["b"],
                       routes_to={
                           lambda s: "again" if len(s["log"]) < 4 else "done":
                               {"again": "b", "done": "__END__"},
                       }),
        ],
    )
    result = await build_module_graph(defn).ainvoke(
        {"log": [], "gate": True, "progress_queue": None}
    )

    assert result["log"] == ["a", "b", "c", "b", "c"]


@pytest.mark.asyncio
async def test_build_module_graph__two_plain_deps_at_different_depths__fire_target_once():
    """`depends_on` is an AND-join, not N independent triggers.

    Wired as two separate edges the target fires twice — once per branch
    completing — which is how the naive fix produced
    `InvalidUpdateError: At key 'task_chunks': Can receive only one value per
    step` and a duplicate 27B decomposition call.
    """
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="barrier",
        state_class=LogState,
        steps=[
            ModuleStep(name="start", handler=_recorder("start")),
            ModuleStep(name="deep_1", handler=_recorder("deep_1"), depends_on=["start"]),
            ModuleStep(name="deep_2", handler=_recorder("deep_2"), depends_on=["deep_1"]),
            ModuleStep(name="shallow", handler=_recorder("shallow"), depends_on=["start"]),
            ModuleStep(name="join", handler=_recorder("join"),
                       depends_on=["deep_2", "shallow"]),
        ],
    )
    result = await build_module_graph(defn).ainvoke(
        {"log": [], "gate": True, "progress_queue": None}
    )

    assert result["log"].count("join") == 1
    assert result["log"][-1] == "join"
    assert {"deep_2", "shallow"}.issubset(set(result["log"]))


@pytest.mark.asyncio
async def test_build_module_graph__conditional_dep__gate_is_honoured():
    """A dep that routes to the step contributes its edge CONDITIONALLY only.

    Adding a plain edge as well would let the step run on the branch that was
    not taken — the measured `decompose_goal ran even when validate_goal
    routed __END__` bypass.
    """
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="gated",
        state_class=LogState,
        steps=[
            ModuleStep(name="gate", handler=_recorder("gate"),
                       routes_to={lambda s: "yes" if s["gate"] else "no":
                                  {"yes": "work", "no": "__END__"}}),
            ModuleStep(name="work", handler=_recorder("work"), depends_on=["gate"]),
        ],
    )
    graph = build_module_graph(defn)

    opened = await graph.ainvoke({"log": [], "gate": True, "progress_queue": None})
    closed = await graph.ainvoke({"log": [], "gate": False, "progress_queue": None})

    assert opened["log"] == ["gate", "work"]
    assert closed["log"] == ["gate"]


@pytest.mark.asyncio
async def test_build_module_graph__flag_disabled_barrier_member__does_not_deadlock(monkeypatch):
    """A feature-flagged-off step still COMPLETES (returns {}), so it satisfies
    an AND-join. Pinned because switching `_wrap_step` to genuinely skip the
    node would silently deadlock every barrier the step sits in.
    """
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    monkeypatch.setenv("JARVIS_ENABLE_BARRIER_TEST", "0")
    LogState = _log_state()

    defn = ModuleDefinition(
        name="flagged_barrier",
        state_class=LogState,
        steps=[
            ModuleStep(name="start", handler=_recorder("start")),
            ModuleStep(name="deep_1", handler=_recorder("deep_1"), depends_on=["start"]),
            ModuleStep(name="deep_2", handler=_recorder("deep_2"), depends_on=["deep_1"]),
            ModuleStep(name="flagged", handler=_recorder("flagged"), depends_on=["start"],
                       feature_flag="ENABLE_BARRIER_TEST"),
            ModuleStep(name="join", handler=_recorder("join"),
                       depends_on=["deep_2", "flagged"]),
        ],
    )
    result = await build_module_graph(defn).ainvoke(
        {"log": [], "gate": True, "progress_queue": None}
    )

    assert "flagged" not in result["log"]  # the flag really did skip the body
    assert result["log"].count("join") == 1  # ...and the barrier still opened


def test_build_module_graph__conditional_dep_mixed_with_plain_deps__is_rejected():
    """The one shape LangGraph cannot express — rejected loudly, not silently.

    A barrier ignores its members' branches: `add_edge([plain, router], step)`
    fires `step` even when `router` routed elsewhere (measured). Declaring
    both a routing dep and a plain dep therefore has no honest wiring, so the
    builder refuses instead of quietly dropping the gate.
    """
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="ambiguous",
        state_class=LogState,
        steps=[
            ModuleStep(name="start", handler=_recorder("start")),
            ModuleStep(name="plain", handler=_recorder("plain"), depends_on=["start"]),
            ModuleStep(name="gate", handler=_recorder("gate"), depends_on=["start"],
                       routes_to={lambda s: "yes": {"yes": "work", "no": "__END__"}}),
            ModuleStep(name="work", handler=_recorder("work"),
                       depends_on=["plain", "gate"]),
        ],
    )

    with pytest.raises(ValueError, match="both a routing dependency"):
        build_module_graph(defn)


def test_build_module_graph__unknown_dependency__is_rejected():
    """A typo in `depends_on` used to orphan the step in silence."""
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="typo",
        state_class=LogState,
        steps=[
            ModuleStep(name="start", handler=_recorder("start")),
            ModuleStep(name="next", handler=_recorder("next"), depends_on=["strat"]),
        ],
    )

    with pytest.raises(ValueError, match="unknown step 'strat'"):
        build_module_graph(defn)


def test_build_module_graph__orphaned_step__is_rejected():
    """LangGraph compiles an unreachable node in silence — the builder must not.

    Six planning steps (decompose_goal through create_draft) sat dead in the
    compiled graph for the whole life of the framework because nothing ever
    said so out loud.
    """
    from app.core.module_framework import ModuleStep, ModuleDefinition, build_module_graph

    LogState = _log_state()

    defn = ModuleDefinition(
        name="orphanage",
        state_class=LogState,
        steps=[
            ModuleStep(name="start", handler=_recorder("start")),
            ModuleStep(name="stranded", handler=_recorder("stranded")),
        ],
    )

    with pytest.raises(ValueError, match=r"unreachable from entry 'start'.*stranded"):
        build_module_graph(defn)
