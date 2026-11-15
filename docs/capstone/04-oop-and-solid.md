# 04 — OOP and SOLID

This document shows where the four pillars of object-oriented programming (encapsulation, abstraction, inheritance, polymorphism) and the five SOLID principles are applied in the Jarvis backend, at commit `e76bff9`, now part of `main`. For each one it gives what it means in this code base in one sentence, two or three concrete examples with `file:line` and a short excerpt copied from the code, the test that would fail if the principle were broken, and one honest place where the code falls short and why. The class-level picture is in `03-class-diagrams.md`, and the patterns these principles produce (Strategy, Decorator, Chain of Responsibility, Template Method and others) are in `05-design-patterns.md`. The last section lists the ten largest source files and what would be split next.

**Owner: ______**

---

## Contents

1. [Encapsulation](#1-encapsulation)
2. [Abstraction](#2-abstraction)
3. [Inheritance](#3-inheritance)
4. [Polymorphism](#4-polymorphism)
5. [S — Single Responsibility](#5-s--single-responsibility)
6. [O — Open/Closed](#6-o--openclosed)
7. [L — Liskov Substitution](#7-l--liskov-substitution)
8. [I — Interface Segregation](#8-i--interface-segregation)
9. [D — Dependency Inversion](#9-d--dependency-inversion)
10. [The ten largest source files](#10-the-ten-largest-source-files)
11. [Summary](#11-summary)
12. [How to verify this document](#12-how-to-verify-this-document)

---

## 1. Encapsulation

**Here:** an object's rules travel with its data, so a draft, a memory or the consent chain cannot be put into a state its own class forbids, and collaborators are held in private fields set once in the constructor.

**Example 1 — a draft changes status only through `transition`.** `DraftSchedule.transition` checks the move against the draft transition table and returns a copy; an illegal move raises `InvalidStateTransition` (409). `app/domain/entities.py:377-383`:

```python
        to = DraftStatus(to)
        if to not in DRAFT_TRANSITIONS[self.status]:
            raise InvalidStateTransition(
                f"This draft is {self.status.value} and cannot become {to.value}.",
                details={"draft_id": self.id, "from": self.status.value, "to": to.value},
            )
        return self.model_copy(update={"status": to}, deep=True)
```

**Example 2 — the consent chain guards its own invariant.** A `ConsentPolicy` must end with a link that always decides; the constructor refuses any other chain, and the rules are kept in private fields. `app/domain/anticipation.py:221-227`:

```python
    def __init__(self, rules: list[ConsentRule]) -> None:
        if not rules or not isinstance(rules[-1], ModePolicyRule):
            raise ValueError(
                "The consent chain must end with a ModePolicyRule, which always decides."
            )
        self._rules = list(rules[:-1])
        self._last: ModePolicyRule = rules[-1]
```

**Example 3 — a memory validates itself.** Only a forgotten memory may have empty words. `app/domain/entities.py:546-550`:

```python
    @model_validator(mode="after")
    def _blank_only_when_forgotten(self) -> "Memory":
        if not self.content.strip() and self.status is not MemoryStatus.FORGOTTEN:
            raise ValueError("only a forgotten memory may have empty content")
        return self
```

**Would break:** `tests/test_domain.py::test_draft_transition__accepted_to_accepted__raises_invalid_state_transition`, `tests/test_consent.py::test_consent_policy__last_rule_not_mode__value_error`, `tests/test_domain.py::test_memory__blank_content__allowed_only_when_forgotten`.

**Where we fall short:** the entities are ordinary, mutable Pydantic models. Nothing stops code from writing `draft.status = DraftStatus.ACCEPTED` and skipping `transition`; even `draft.status = "accepted?"` is accepted, because `DraftSchedule` sets neither `frozen` nor `validate_assignment` (`app/domain/entities.py:333-383`, checked by hand: the assignment goes through). Why it is still safe in practice: every status that is stored is checked again where it is written (`accept_atomic` re-checks under the row lock, `app/domain/ports.py:216-227`; suggestion and memory status writes are compare-and-set, `ports.py:362-380`, `488-506`). The fix would be `model_config = ConfigDict(frozen=True)` on the entities; it was not done because several services build an entity and then adjust a field before saving it. Similarly, the task outcome table (`_Move`) lives in the service, not the domain (`app/services/task_service.py:65-84`).

---

## 2. Abstraction

**Here:** callers see a small interface that says what is done, never how: a service asks a port for "the user's pending tasks" or "a completion", not for a PostgREST query or an HTTP call to LM Studio.

**Example 1 — the language-model port.** Three methods (`is_available`, `complete`, `stream`), no provider, no transport, no retry; two of them, `app/domain/ports.py:800-806`:

```python
    @abstractmethod
    async def complete(self, req: LLMRequest) -> str | BaseModel:
        """Return the completion: an instance of req.response_schema when set, else text."""

    @abstractmethod
    def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        """Yield (event_type, token) pairs as they arrive; req.response_schema must be None."""
```

**Example 2 — one function for every model call.** `route_llm_call` hides roles, the cloud switch, the PRIMARY lock, the PII gate, fallback and metering behind one call; the caller passes a task name and a prompt. `app/core/model_router.py:405-412`:

```python
    chain = get_chain()
    gate = nullcontext() if hooks is None else redacting_with(PreCloudLLMRedactor(hooks))
    try:
        with gate:
            if stream:
                return await _open_stream(chain, req, skip_local)
            async with _one_primary_call(role, local=not skip_local):
                return await chain.complete(req, skip_local=skip_local)
```

**Example 3 — the user, as a turn reads them.** `UserModel` is a facade over three repository ports; a planning node asks it for the profile, the rules or the pending tasks and never sees a database handle. `app/core/user_model.py:26-33` (constructor):

```python
    def __init__(
        self,
        user_id: str,
        *,
        profiles: ProfileRepository,
        constraints: ConstraintRepository,
        tasks: TaskRepository,
    ) -> None:
```

**Would break:** `tests/test_llm_providers.py::test_route_llm_call__any_call__delegates_to_the_chain`, `tests/test_domain.py::test_ports__instantiated_directly__raise_type_error`, `tests/test_injection.py::test_user_model__over_ports__no_db_attribute`.

**Where we fall short:** `UserModel` is a legacy facade that was moved onto ports without changing its shape. It returns `list[dict]` (each entity `model_dump`ed, `app/core/user_model.py:77-111`) rather than entities, because the planning nodes that read it still work on dicts; and `get_estimated_energy` builds a `HeuristicEnergyEstimator` itself (`user_model.py:113-119`), so the facade knows a concrete strategy and ignores the learned focus profile the rest of the system uses. Changing either means touching every planning node (`app/modules/planning_graph.py`, 1,008 lines), which Plan 3 left for later.

---

## 3. Inheritance

**Here:** a subclass inherits behaviour it really shares (error codes, decorator plumbing, repository plumbing) and overrides only what differs; interfaces are inherited separately from implementation.

**Example 1 — the error hierarchy.** Every error inherits the constructor and overrides two class attributes; one handler turns any of them into the error envelope. `app/domain/errors.py:13-22` and `39-45`:

```python
class JarvisError(Exception):
    """Base of every error Jarvis raises on purpose."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
```

```python
class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class InvalidStateTransition(ConflictError):
    code = "invalid_state_transition"
```

**Example 2 — the decorator base.** The four LLM decorators inherit identity and availability from `_ProviderDecorator` and add one concern each. `app/infrastructure/llm/decorators.py:28-36` (the class goes on to delegate `location` and `is_available` the same way, `:38-43`):

```python
class _ProviderDecorator(LLMProvider):
    """Delegates identity and availability to the wrapped provider."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name
```

**Example 3 — implementation from one base, interface from another.** Each Supabase repository inherits shared plumbing (the client, running a query off the event loop, translating failures) and, separately, its port. `app/infrastructure/persistence/supabase_repos.py:165-173`, then `:176-177`:

```python
class _SupabaseRepository:
    """Shared plumbing: one table, calls off the event loop, failures translated."""

    table: str

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _run(self, build: Callable[[Any], Any], *, table: str | None = None) -> list[dict]:
```

```python
class SupabaseTaskRepository(_SupabaseRepository, TaskRepository):
    table = "user_tasks"
```

**Would break:** `tests/test_errors.py::test_errors__hierarchy__subclassing` and `::test_error_envelope__jarvis_error__maps_status_and_code`, `tests/test_llm_providers.py::test_factory__cloud_provider__redacted_and_metered`, `tests/test_repository_contracts.py::test_factory__both_families__implement_every_port`, and every inheritance arrow in `tests/test_design_conformance.py`.

**Where we fall short:** `PatternRuleTrigger` overrides a step of the `Trigger` template only to switch it off: `already_done` returns `False` because each pattern is proposed once, which its `gather` decides (`app/services/anticipation/triggers.py:327-329`). The base class's "once a day" step does not fit every trigger, so the subclass has to know about and disable it. A cleaner design would make the daily check an opt-in hook; it was left because there are only two triggers and the override is documented in place.

---

## 4. Polymorphism

**Here:** a caller holds a list of objects behind one interface and calls the same method on each; which code runs depends on the object, so new kinds are added without changing the loop.

**Example 1 — the provider chain.** The chain calls `complete` on whatever providers it was given; a local adapter, Claude wrapped in four decorators and Gemini all answer the same call. `app/infrastructure/llm/chain.py:89-95`:

```python
        for provider in self._candidates(skip_local):
            try:
                return await provider.complete(req)
            except LLMError as exc:
                self._fall_over(provider, req, exc)
                last_error = exc
        raise LLMUnavailableError(_NOTHING_REACHABLE) from last_error
```

**Example 2 — the consent chain.** Every rule answers `evaluate`; the first decision wins. `app/domain/anticipation.py:229-234`:

```python
    def evaluate(self, action: ProactiveAction, ctx: ConsentContext) -> ConsentDecision:
        for rule in self._rules:
            decision = rule.evaluate(action, ctx)
            if decision is not None:
                return decision
        return self._last.evaluate(action, ctx)
```

**Example 3 — the sweeps.** The scheduler runs task, memory, conversation and checkpoint sweeps through one method and one interval attribute. `app/services/anticipation/scheduler.py:72-78`:

```python
        for sweep in self._sweeps:
            last = self._last_swept.get(sweep.name)
            if last is not None and now - last < sweep.interval:
                continue
            self._last_swept[sweep.name] = now
            try:
                report["sweeps"][sweep.name] = await sweep.run(user_ids, now)
```

**Would break:** `tests/test_llm_providers.py::test_chain__timeout_on_first__second_answers`, `tests/test_consent.py::test_consent__dismissal_outranks_read_only__deny`, `tests/test_anticipation_scheduler.py::test_tick__sweep_runs_once_per_interval`.

**Where we fall short:** two type checks remain where the interface could carry the answer. `MeteredProvider` asks `isinstance(req, RedactedLLMRequest)` to read the redaction count (`app/infrastructure/llm/decorators.py:286`), and the factory asks `getattr(provider, "model_for_role", None)` because `model_for_role` is on every adapter but not on the `LLMProvider` port (`app/infrastructure/llm/factory.py:21-25`). Both work, but each is a small hole a new provider could fall through. Adding `model_for_role` to the port, and `pii_redactions` (default 0) to `LLMRequest`, would remove them.

---

## 5. S — Single Responsibility

**Here:** each class has one reason to change: the router parses, the turn service runs a turn, the encoder writes frames, each LLM decorator adds one concern.

**Example 1 — the chat router only routes.** The whole endpoint is 56 lines at `c956a8f` (61 at `e76bff9`, before a hidden alias was removed); the handler opens a turn and returns the stream. `app/api/v1/endpoints/chat.py:49-55`:

```python
    turn = await ChatTurnService.for_app(http_request.app.state).open_turn(
        request, request_id=request_id_of(http_request)
    )
    return EventStreamResponse(
        turn.frames(),
        headers=_SSE_HEADERS,
        background=BackgroundTask(turn.finish),
```

**Example 2 — one place writes a frame.** Every SSE frame is a Pydantic model serialised by `SseEncoder.frame`. `app/schemas/chat_stream.py:337-342`:

```python
    @staticmethod
    def frame(event: SseEvent, payload: BaseModel) -> str:
        """``event: <name>``, ``data: <json>``, a blank line. The JSON carries exactly the
        fields the payload was given (``exclude_unset``), as the emitter's dict did."""
        data = json.dumps(payload.model_dump(mode="json", exclude_unset=True))
        return f"event: {event.value}\ndata: {data}\n\n"
```

**Example 3 — one concern per decorator.** `TimeoutProvider` only bounds the call; redaction, retry and metering are other classes. `app/infrastructure/llm/decorators.py:194-198`:

```python
    async def complete(self, req: LLMRequest) -> str | BaseModel:
        try:
            return await asyncio.wait_for(self._inner.complete(req), timeout=req.timeout_s)
        except asyncio.TimeoutError as exc:
            raise self._timeout(req) from exc
```

**Would break:** `tests/test_chat_turn.py::test_chat_router__routing_and_parsing_only` (at most 80 lines, at most 12 statements per function, no imports of the turn's internals), `tests/test_sse_golden.py` (the bytes of 11 recorded turns), `tests/test_llm_providers.py::test_timeout__hung_complete__raises_llm_timeout`.

**Where we fall short:** some units are still too big. `ChatTurn.frames` is one 115-line method that takes the lock, reads the conversation, pumps the graph, frames node events, saves the reply and builds the `complete` payload (`app/services/chat_turn/service.py:123-237`); the helpers it calls were split out in Plan 3 (`context.py`, `frames.py`, `reply.py`), but the sequence itself stayed in one generator because its `try/finally` must cover every step. `app/modules/planning_graph.py` holds every planning step in one 1,008-line module, and `app/orchestrator/graph.py` mixes the graph's wiring with node bodies (draft actions, rule storage, extraction). See section 10.

---

## 6. O — Open/Closed

**Here:** new detectors, providers, triggers and action kinds are added by writing a new class and registering it in the composition root; the code that uses them does not change.

**Example 1 — a new pattern detector needs no trigger edit.** Whether a pattern may become a standing rule is the detector's own class attribute; the trigger asks the service, which asks the detector. `app/services/memory/patterns.py:589-592` and `app/services/anticipation/triggers.py:362-368`:

```python
    def proposes_rule(self, pattern: Pattern) -> bool:
        """Whether ``pattern`` may be offered as a standing rule: its detector says so."""
        detector = self._by_name.get(pattern.detector)
        return detector is not None and detector.proposes_rule and pattern.hour is not None
```

```python
    def _qualifies(self, view: PatternView) -> bool:
        """A remembered, unconfirmed pattern worth telling whose detector proposes rules."""
        return (
            self._patterns.proposes_rule(view.pattern)
            and self._patterns.worth_telling(view)
            and not view.confirmed
        )
```

**Example 2 — a new cloud provider slots into the chain.** `LLMProviderFactory.build` takes extra cloud providers and wraps them like the others. `app/infrastructure/llm/factory.py:33-40`:

```python
    @staticmethod
    def build(
        cfg: ModuleType,
        redactor: Redactor,
        bus: EventBus,
        extra_cloud: list[LLMProvider] | None = None,
    ) -> FallbackChainProvider:
        """Return ``[local, claude, *extra_cloud, gemini]``, each wrapped for its location.
```

**Example 3 — new action kinds and triggers are registered, not coded in.** `ActionFactory.register` maps a kind to a builder (`app/services/anticipation/service.py:90-92`); the container registers each trigger with `AnticipationService.register_trigger` (`app/container.py:208-216`):

```python
    def register(self, kind: str, build: Callable[[dict], ProactiveAction]) -> None:
        """Rebuild payloads of ``kind`` with ``build``."""
        self._builders[kind] = build
```

**Would break:** `tests/test_anticipation_design.py::test_pattern_rule__new_detector_that_proposes_rules__offered_without_a_trigger_edit`, `tests/test_patterns.py::test_detector__new_strategy__brings_its_own_insight_and_planning_use`, `tests/test_llm_providers.py::test_factory__extra_cloud_provider__order_is_local_extra_cloud_gemini`, `tests/test_anticipation_design.py::test_anticipation__triggers__the_registered_names_and_descriptions`.

**Where we fall short:** adding a cognitive module is not closed for modification. The intent-to-module table is a hand-written dict (`app/orchestrator/routing.py:12-28`) and the graph's edge map names every module (`app/orchestrator/graph.py:805-818`), so a new module means editing both. The spec's `CognitiveModule` registry, where a module declares its intents and the router is derived, was Plan 3 Task 9 and was dropped by ruling to protect the demo (`.superpowers/sdd/2026-11-14-capstone-plan-3-finish-the-restructure/progress.md`).

---

## 7. L — Liskov Substitution

**Here:** any implementation of a port can stand in for any other without the caller noticing: the in-memory and Supabase repositories, the two energy estimators, the real and the null event bus.

**Example 1 — one contract suite, two families.** Every repository test runs twice, once per family. `tests/test_repository_contracts.py:83-87`:

```python
@pytest.fixture(params=["in_memory", "supabase_fake"])
def repos(request, memory_store, fake_client) -> Repositories:
    if request.param == "in_memory":
        return RepositoryFactory.in_memory(memory_store)
    return RepositoryFactory.supabase(fake_client)
```

**Example 2 — the null event bus.** `NullEventBus` honours `publish` by doing nothing, so code built without a container (tests, scripts, the lazy default chain in `app/core/model_router.py:169`) runs unchanged. `app/domain/events.py:115-119`:

```python
class NullEventBus(EventBus):
    """An event bus that drops every event — for callers and tests with nobody listening."""

    async def publish(self, event: DomainEvent) -> None:
        """Do nothing."""
```

**Example 3 — two energy estimators, one contract.** `HeuristicEnergyEstimator` and `HistoryEnergyEstimator` both return a 24-hour `FocusProfile` from `estimate(user_id, zone, now=..., events=...)` (`app/services/energy.py:49-64`, `85-122`); with too little history the learned one reports `source="prior"` and the prior's scores, so a caller cannot tell a new user from the heuristic.

**Would break:** `tests/test_repository_contracts.py` (97 test functions; those taking the `repos` fixture run once per family), `tests/test_domain.py::test_null_event_bus__publish__no_error`, `tests/test_energy.py::test_history_estimator__no_events__prior_and_source_prior` and `::test_heuristic__matches_user_model_table_for_every_hour`.

**Where we fall short:** the provider chain widens the port it implements. `FallbackChainProvider.complete` and `.stream` take an extra `skip_local` argument and the class adds `has_available` (`app/infrastructure/llm/chain.py:62-64`, `87`, `97-99`), so `route_llm_call` depends on the concrete chain type (`get_chain() -> FallbackChainProvider`, `app/core/model_router.py:160`) rather than on `LLMProvider`. Substituting a plain provider for the chain would lose the cloud switch. Second, the Supabase family is contract-tested against `FakeSupabase` (`tests/fakes.py`), not a live PostgREST; the live end-to-end script (`scripts/live_e2e.py`) covers the real database, but it is not part of the offline suite.

---

## 8. I — Interface Segregation

**Here:** a client depends only on the methods it uses: eleven small repository ports instead of one data-access object, one-method strategy ports, and a planning sub-graph that receives a seven-field record instead of the whole container.

**Example 1 — the planning sub-graph's own record.** `PlanningServices` (a frozen dataclass, `app/services/planning/services.py:16-23`) lists exactly what planning uses. Its fields, `services.py:25-31`:

```python
    drafts: DraftService  # proposes the plan for review; accepting it is the only task write
    tasks: TaskService  # every task status change
    task_repo: TaskRepository  # the user's pending and set-aside tasks
    constraints: ConstraintRepository  # the user's stored rules
    profiles: ProfileRepository  # the user's timezone
    memory: MemoryService  # stated rules and learned patterns, as memories
    energy: EnergyEstimator  # the learned focus profile the solver leans on
```

**Example 2 — one-method ports.** `Sweep`, `TextEmbedder` and `EnergyEstimator` each have one abstract method; `UsageRepository` has two, `GoalRepository` three (`app/domain/ports.py:262-328`, `662-759`). `app/domain/ports.py:682-689`:

```python
class Sweep(ABC):
    """A periodic job over every user, owned by the subsystem whose data it maintains."""

    name: ClassVar[str]
    interval: ClassVar[timedelta]

    @abstractmethod
    async def run(self, user_ids: list[str], now: datetime) -> int:
```

**Example 3 — a module sees a projection of the turn state.** A module definition may declare `state_in` / `state_out`, so its sub-graph receives and returns only its own keys (`definition` is the module's `ModuleDefinition`, read on the line before). `app/orchestrator/module_wrapper.py:42-51`:

```python
        if definition.state_in:
            module_state = definition.state_in(state)
        else:
            module_state = dict(state)

        compiled = registry.get_compiled(module_name)
        result = await compiled.ainvoke(module_state)

        if definition.state_out:
            output = definition.state_out(result, module_name)
```

**Would break:** `tests/test_injection.py::test_planning_nodes__read_planning_services_from_state__none_means_no_slots_no_focus_no_scan`, `tests/test_planning_replan.py::test_planning_state_out__habits_could_not_be_read__the_reply_leads_with_it`, and the interface-completeness check in `tests/test_design_conformance.py` (every abstract method of a port is drawn, and a port has no concrete methods).

**Where we fall short:** the turn state is not segregated. `JarvisState` is one 44-key `TypedDict` that every top-level node receives (`app/orchestrator/state.py:31-118`), and the whole `AppContainer` rides in it under `container` (`app/services/chat_turn/context.py:63`), so any node can reach any service: inside a turn the container works like a service locator. Only the planning module projects its state; research and knowledge receive the full dict. Two ports are also wider than their clients need: `TaskRepository` mixes reads with the status writes (the spec's `TaskReader` / `TaskWriter` split was not made, `app/domain/ports.py:64-128`), and `MemoryRepository` has eight methods (`ports.py:405-506`).

---

## 9. D — Dependency Inversion

**Here:** high-level policy (services, triggers, the planner) depends on ports in the domain; concrete infrastructure depends on the same ports; and exactly one place, the composition root, connects the two.

**Example 1 — one place picks the repository family.** `app/container.py:131-139`:

```python
        if supabase_client is not None:
            repos = RepositoryFactory.supabase(supabase_client)
        else:
            logger.warning("running on in-memory repositories — nothing is persisted")
            repos = RepositoryFactory.in_memory()
```

**Example 2 — services take ports through the constructor.** `TaskService` knows two ports and the bus, never a table. `app/services/task_service.py:94-102`:

```python
    def __init__(
        self,
        tasks: TaskRepository,
        task_events: TaskEventRepository,
        bus: EventBus,
        *,
        clock: Callable[[], datetime] = utc_now,
        settings: TaskSettings | None = None,
    ) -> None:
```

**Example 3 — the anticipation layer depends on a planning abstraction.** `MorningBriefTrigger` needs a re-plan, so it takes the `DraftPlanner` port (`app/domain/ports.py:699-721`); the implementation that drives the LangGraph planning sub-graph lives in the modules layer (`PlanningGraphReplanner`, `app/modules/planning_replan.py:34`) and is injected by the container (`app/container.py:197`, `209-212`). `app/services/anticipation/triggers.py:99-108`:

```python
    def __init__(
        self,
        tasks: TaskRepository,
        task_events: TaskEventRepository,
        planner: DraftPlanner,
        energy: EnergyEstimator,
        patterns: PatternService,
        actions: ActionFactory,
        *,
        suggestions: SuggestionRepository,
```

**Would break:** `tests/test_layering.py::test_layering__only_container_builds_repositories`, `::test_layering__services_orchestrator_modules_core_api__do_not_import_infrastructure`, `::test_domain__imports_nothing_from_outer_layers`, `tests/test_injection.py::test_container_built_services__import_no_config_module`, `tests/test_container.py::test_container__no_supabase__uses_in_memory_family`.

**Where we fall short:**

- **Six raw-table leftovers.** Six files still call Supabase tables directly instead of through a port: `services/chat_history.py`, `api/v1/endpoints/sessions.py`, `api/v1/endpoints/ingestion.py`, `services/extraction/orchestrator.py` (no repositories yet for sessions, messages and pending calendar rows: Plan 3 Task 6 was dropped), plus `services/analytical/workspace_builder.py` and `services/user_preferences.py`. They are pinned by an allow-list that can only shrink (`tests/test_layering.py:451-462`, `514-531`); documents and their task links left it when they gained `DocumentRepository`. Because of them, `ChatTurnService` still takes a raw `db_client` for the chat history (`app/services/chat_turn/service.py:243-252`).
- **One import of infrastructure from core.** `app/core/model_router.py` imports the chain, decorators and factory to install and read the process-wide chain (the one entry on `_INFRASTRUCTURE_IMPORTS_ALLOWED`, `tests/test_layering.py:146-151`).
- **Process-wide singletons are still looked up.** The container is no longer looked up anywhere (`tests/test_injection.py::test_runtime_registry__gone_and_imported_nowhere`), but three collaborators still are: the hook pipeline through `get_hooks()` (`app/core/module_framework.py:120`, `app/orchestrator/module_wrapper.py:21`, `app/services/analytical/workspace_builder.py:52`), the installed LLM chain through `get_chain()` (`app/core/model_router.py:160-171`), and the module registry (`app/modules/__init__.py:9`, read by `build_jarvis_graph`). Each is built once per process and is replaceable in tests (`set_chain`, `reset_chain`), but a caller cannot be handed a different one through its constructor. These are in-process guards and wiring that predate the restructure; moving them into the container is the next step.

---

## 10. The ten largest source files

Line counts at `c956a8f` (`git ls-files 'app/*.py' | xargs wc -l | sort -n | tail`); `planning_graph.py`, `graph.py`, `memory_service.py` and `workspace_builder.py` grew after `e76bff9` (1,008, 810, 627 and 531 lines there). `supabase_repos.py`, `in_memory.py` and `ports.py` are counted after the documents repositories were added. The code style rule asks to consider a split past about 200–300 lines (`.claude/rules/code-style.md`).

| # | File | Lines | What it holds | What would be split next |
|---|---|---|---|---|
| 1 | `app/infrastructure/persistence/supabase_repos.py` | 1,120 | All twelve Supabase repositories and their shared base | One module per repository in a `persistence/supabase/` package, `_SupabaseRepository` in `base.py`; `RepositoryFactory` imports change, nothing else |
| 2 | `app/modules/planning_graph.py` | 1,015 | Every planning step (validate, constraints, habit translation, slot expansion, decomposition, fusion, solve, draft, infeasible ladder) and the module definition | A `modules/planning/` package: constraint steps, decomposition, solving and the horizon ladder, drafting, and the definition with `planning_state_in/out` (the dropped Plan 3 Task 9) |
| 3 | `app/infrastructure/persistence/in_memory.py` | 1,010 | The in-memory store, all twelve in-memory repositories and the in-memory file store | Mirror the Supabase split, one module per repository |
| 4 | `app/orchestrator/graph.py` | 846 | `build_jarvis_graph` plus the bodies of the top-level nodes (context, extraction, classification, draft actions, rule storage) | Keep only the wiring; move node bodies to `orchestrator/nodes/` (draft actions, understanding, rules) |
| 5 | `app/domain/ports.py` | 818 | Twelve repository ports, the document file store, plus the LLM, retry, embedding, energy, planner and sweep ports | `ports/repositories.py`, `ports/llm.py`, `ports/learning.py`, re-exported from `ports/__init__.py` so imports stay valid |
| 6 | `app/services/memory/patterns.py` | 724 | The four detectors, `PatternService`, its view models and `PatternObserver` | `patterns/detectors.py` (the strategies), `patterns/service.py`, `patterns/observer.py` |
| 7 | `app/domain/entities.py` | 701 | Every entity, enum and transition table | Entities by aggregate (planning, memory, anticipation, conversation, usage) and the transition tables in one `lifecycles.py` |
| 8 | `app/services/memory/memory_service.py` | 639 | Memory lifecycle (remember, confirm, dismiss, forget, edit, fade) and recall | A `MemoryRecall` class for `recall` and `drop_suppressed`; the lifecycle stays in `MemoryService` |
| 9 | `app/services/memory/conversation_memory.py` | 632 | The conversation window, compaction, idle summaries and "since we last spoke" | Separate the window builder from the summariser (compaction and idle summaries share the summary-writing code) |
| 10 | `app/services/analytical/workspace_builder.py` | 561 | The task workspace: retrieval, web search (with its 5-minute cooldown after a failure), practice assets, and its cache | `WorkspaceRepository` and `TaskMaterialRepository` ports (which also takes it off the raw-table allow-list), and the web-search helper in its own module |

---

## 11. Summary

| Principle | Strongest evidence | Main shortfall |
|---|---|---|
| Encapsulation | Transition tables checked by `transition`, `check_suggestion_move`, `check_memory_move`; private collaborators | Entities are mutable; assignment skips the checks (writes are re-checked) |
| Abstraction | 17 ABC ports in `app/domain/ports.py`, three more in the domain; one model-call function | `UserModel` returns dicts and builds its own energy heuristic |
| Inheritance | Error hierarchy; decorator base; repository base plus port | A template step overridden only to switch it off |
| Polymorphism | Provider chain, consent chain, sweeps, event listeners | Two `isinstance` / `getattr` checks around the LLM port |
| SRP | 56-line router; one frame encoder; one concern per decorator | `ChatTurn.frames`, `planning_graph.py`, `graph.py` |
| OCP | Detectors, providers, triggers, actions added by registration | Modules still need edits to the routing table and graph |
| LSP | One contract suite over two repository families | The chain widens `LLMProvider` with `skip_local` |
| ISP | `PlanningServices`; one-method ports | 44-key turn state carrying the whole container |
| DIP | One composition root; `test_layering.py` | 6 raw-table leftovers; three process-wide lookups |

---

## 12. How to verify this document

From `Jarvis-Engine/` on branch `main`:

```bash
# Every "Would break" test named above
.venv/bin/python -m pytest tests/test_domain.py tests/test_consent.py tests/test_errors.py \
  tests/test_llm_providers.py tests/test_injection.py tests/test_repository_contracts.py \
  tests/test_anticipation_scheduler.py tests/test_chat_turn.py tests/test_sse_golden.py \
  tests/test_anticipation_design.py tests/test_patterns.py tests/test_energy.py \
  tests/test_planning_replan.py tests/test_layering.py tests/test_container.py \
  tests/test_design_conformance.py -q

# The excerpts are copied verbatim: compare any of them with the file
sed -n '377,383p' app/domain/entities.py
sed -n '221,227p' app/domain/anticipation.py
sed -n '89,95p' app/infrastructure/llm/chain.py

# The encapsulation shortfall (section 1): assignment is not validated
.venv/bin/python -c "from app.domain.entities import DraftSchedule; d = DraftSchedule(id='d', user_id='u', status='pending'); d.status = 'accepted?'; print(d.status)"

# The ten largest files (section 10; tracked files only, so stray local files do not count)
git ls-files 'app/*.py' | xargs wc -l | sort -n | tail -11
```
