# Pugal: OOP, SOLID and design patterns

You present slide 8 (object-oriented design: the four pillars), slide 9 (SOLID) and slide 10 (the design patterns overview).
Your own two patterns, the ones you must answer questions on in depth, are **Singleton** and **Factory**.
Jarvis is a personal intelligence for one person. Never call it a calendar or a planner app.

## Read in this order

| # | Read | Why | Time |
|---|---|---|---|
| 1 | `docs/capstone/05-design-patterns.md` §0 "Start here" | All eleven patterns in plain words, with everyday comparisons | 12 min |
| 2 | `docs/capstone/04-oop-and-solid.md` §1 to §4 | The four OOP pillars, with code and the test for each | 10 min |
| 3 | `docs/capstone/04-oop-and-solid.md` §5 to §9, then §11 | SOLID, each with "where we fall short", and the summary table | 15 min |
| 4 | `docs/capstone/05-design-patterns.md` §9 and §13 (the Singleton row) | Factory and the composition root; why there is no classic Singleton | 6 min |
| 5 | `docs/capstone/00-team-study-guide.md` §7, §8 and §10 (C3, C4, C5) | Cheat sheets and short model answers for the viva | 8 min |
| 6 | `docs/capstone/02-architecture.md` §5, the "Process-wide singletons" row and the paragraph under that table | The honest list of single instances | 2 min |
| 7 | Slides 8 to 10 in `docs/capstone/presentation/Jarvis-Capstone.pptx`, with their speaker notes | What the audience will see | 4 min |

Total: about 57 minutes. If you have only 20 minutes, read rows 1, 4 and 5.

## What to say

Speak slowly. Say the class name on the slide when you reach it. If someone asks for proof, open the file named in the tables below.

### Slide 8: Object-oriented design (about 1 minute)

"This slide shows the four pillars of object-oriented programming, each with one real class from Jarvis.
Encapsulation is `DraftSchedule`. A draft plan can change its status only through its `transition` method, which checks a table of allowed moves, so accepting a plan twice is refused with a 409 error.
Abstraction is `LLMProvider`. It is an abstract class with three methods: `is_available`, `complete` and `stream`. The code that asks a question never sees which model, which network call or which retry is behind it.
Inheritance is `JarvisError`. Every error class inherits from it and only sets its own error code and HTTP status, so one handler turns any error into the same JSON reply.
Polymorphism is `FallbackChainProvider`. It calls `complete` on each provider it holds, and the local model, Claude or Gemini answers that same call in its own way.
Document 04 also says, for each pillar, where our code falls short."

### Slide 9: SOLID (about 1 minute)

"SOLID is five rules that keep classes easy to change. For each one we show a real example and a test that fails if we break it.
S, single responsibility: the chat router only routes. A test fails if it grows past 80 lines or imports the internals of a chat turn.
O, open/closed: to add a new pattern detector we write one new class and register it. The trigger that uses it is not edited.
L, Liskov substitution: we have two families of repositories, Supabase and in-memory, and the same contract tests run against both.
I, interface segregation: the planning sub-graph receives `PlanningServices`, a record of seven fields, not the whole container.
D, dependency inversion: services take abstract ports in their constructors, and only `AppContainer` picks the real classes.
Our weakest point is interface segregation: the state of a chat turn is one wide dictionary that carries the whole container."

### Slide 10: Design patterns (about 1.5 minutes)

"A design pattern is a well-known, named solution to a problem that keeps coming back. Document 05 lists only the patterns that really exist in our code, each with the test that pins it.
They sit in three groups. Persistence uses Repository, Adapter and Factory. The model chain uses Strategy, Decorator and Chain of Responsibility. Anticipation uses Template Method, the consent chain, State tables, Command and Observer.
At the bottom, `AppContainer.build` is the composition root: the one place that builds every object and connects them. `UserModelService.snapshot` is the facade the You page reads.
My own two patterns are Factory and Singleton. `RepositoryFactory` builds a whole matching family of repositories, so Supabase and in-memory ones are never mixed.
For Singleton, there is exactly one container per server process, but it is built once at startup and passed in, not fetched through a `getInstance` method.
Section 13 of document 05 lists the patterns our design named that we did not build."

## OOP: where each pillar is in the code

Words you will use:

- **Port:** an abstract class (a Python `ABC`, abstract base class) in `app/domain/ports.py`. It says what is done, not how.
- **Adapter:** a real class that implements a port over a library, for example `SupabaseTaskRepository` or `ClaudeProvider`.
- **Composition root:** the one place that builds every object and connects them. In Jarvis it is `AppContainer.build`.
- **Process-wide:** one copy for the whole running server, shared by every request.

| Pillar | Class | File | What to point at | One-line explanation |
|---|---|---|---|---|
| Encapsulation | `DraftSchedule` | `app/domain/entities.py` | `transition()` checks `DRAFT_TRANSITIONS` | An illegal move raises `InvalidStateTransition` (409) |
| Encapsulation | `ConsentPolicy` | `app/domain/anticipation.py` | The constructor; rules kept in the private fields `_rules` and `_last` | It refuses any chain that does not end with `ModePolicyRule`, the rule that always decides |
| Encapsulation | `Memory` | `app/domain/entities.py` | The validator `_blank_only_when_forgotten` | Only a forgotten memory may have empty content |
| Abstraction | `LLMProvider` | `app/domain/ports.py` | Three abstract methods: `is_available`, `complete`, `stream` | Callers never see the vendor, the network or retries |
| Abstraction | `route_llm_call` | `app/core/model_router.py` | One function for every model call | Hides roles, fallback, the PII gate (removes personal data before a cloud call) and cost metering |
| Abstraction | `UserModel` | `app/core/user_model.py` | The constructor takes three repository ports | A planning step asks for the profile or the rules and never sees a database handle |
| Inheritance | `JarvisError`, `ConflictError`, `InvalidStateTransition` | `app/domain/errors.py` | Subclasses override `code` and `http_status` | One handler renders every error as the same error envelope |
| Inheritance | `_ProviderDecorator` | `app/infrastructure/llm/decorators.py` | The base of the four model decorators | Each decorator inherits name and availability, and adds one concern |
| Inheritance | `SupabaseTaskRepository` | `app/infrastructure/persistence/supabase_repos.py` | `class SupabaseTaskRepository(_SupabaseRepository, TaskRepository)` | Shared plumbing from one base class, the interface from the port |
| Polymorphism | `FallbackChainProvider` | `app/infrastructure/llm/chain.py` | The loop that calls `provider.complete(req)` | The local model, Claude and Gemini answer the same call |
| Polymorphism | `ConsentPolicy.evaluate` | `app/domain/anticipation.py` | The loop over `rule.evaluate(...)` | Four rule classes, one method; the first decision wins |
| Polymorphism | `AnticipationScheduler` | `app/services/anticipation/scheduler.py` | `sweep.run(...)` for each `Sweep` | Four kinds of sweep (slots, memory decay, session summaries, checkpoints) run through one method |

## SOLID: where each principle is in the code

| Principle | Meaning in one line | Real example class + file | The test that would fail if it broke | Where we fall short |
|---|---|---|---|---|
| **S** Single responsibility | A class has one reason to change | The chat router `app/api/v1/endpoints/chat.py` only routes; `SseEncoder.frame` (`app/schemas/chat_stream.py`) is the one place a stream frame is written; `TimeoutProvider` only bounds the time | `tests/test_chat_turn.py::test_chat_router__routing_and_parsing_only` | `ChatTurn.frames` (`app/services/chat_turn/service.py`) is one long method, over 100 lines; `app/modules/planning_graph.py` holds every planning step in about 1,000 lines; `app/orchestrator/graph.py` mixes wiring with node bodies |
| **O** Open/closed | Add a class; do not edit the callers | A `PatternDetector` subclass with `proposes_rule = True` (`app/services/memory/patterns.py`) is offered by `PatternRuleTrigger` with no edit; `LLMProviderFactory.build` takes `extra_cloud` providers | `tests/test_anticipation_design.py::test_pattern_rule__new_detector_that_proposes_rules__offered_without_a_trigger_edit` | A new cognitive module still means editing `INTENT_TO_MODULE` (`app/orchestrator/routing.py`) and the graph's edge map; the planned `CognitiveModule` registry was dropped |
| **L** Liskov substitution | Any implementation of a port can replace another | The two repository families from `RepositoryFactory`; `NullEventBus` (`app/domain/events.py`); `HeuristicEnergyEstimator` and `HistoryEnergyEstimator` (`app/services/energy.py`) | `tests/test_repository_contracts.py` (its `repos` fixture runs each test once per family) | `FallbackChainProvider` widens `LLMProvider` with `skip_local` and `has_available`, so `route_llm_call` depends on the concrete chain; the Supabase family is tested against `FakeSupabase`, not a live database |
| **I** Interface segregation | A client depends only on what it uses | `PlanningServices` (`app/services/planning/services.py`), seven fields; `Sweep`, `TextEmbedder` and `EnergyEstimator` have one abstract method each | `tests/test_injection.py::test_planning_nodes__read_planning_services_from_state__none_means_no_slots_no_focus_no_scan` | **Our weakest point.** `JarvisState` (`app/orchestrator/state.py`) has over 40 keys and carries the whole container, so any node can reach any service; `TaskRepository` mixes reads and writes |
| **D** Dependency inversion | High-level code depends on abstractions; one root picks the real classes | `TaskService` (`app/services/task_service.py`) takes `TaskRepository`, `TaskEventRepository` and `EventBus`; `MorningBriefTrigger` takes the `DraftPlanner` port | `tests/test_layering.py::test_layering__only_container_builds_repositories` | 6 files still call database tables directly (an allow-list in `tests/test_layering.py` that can only shrink); `get_hooks`, `get_chain` and the module registry are still process-wide lookups |

## Your two patterns: Singleton and Factory

Answer any pattern question in four sentences, in this order:

1. The problem we had.
2. The pattern, in one line.
3. Where it is in our code (name the class).
4. Why we did not do the simpler thing.

### Singleton

- **The problem.** Every service must share one event bus, one set of repositories and one model chain. If each part built its own copy, an event published on one bus would never reach the listeners on another.
- **The pattern in one line.** Make sure there is only one instance of something, and give everyone access to it.
- **Everyday comparison.** A college has one principal. The principal does not appoint themselves; the management appoints one and tells every department who it is.
- **Where it is.**
  - `AppContainer.build` (`app/container.py`) runs once, from the FastAPI `lifespan` in `app/main.py`, which keeps the result as `app.state.container`. An endpoint reaches it through `container_of` (`app/api/deps.py`); a chat turn carries it in its state under the key `container`.
  - `get_hooks()` (`app/orchestrator/hooks.py`) is a lazy module-level singleton ("lazy" means built on the first call). The first call builds `ActionHooks`, registers its handlers and stores it in `_default_hooks`; every later call returns the same object.
  - The model chain: `_chain` in `app/infrastructure/llm/factory.py` holds the one installed `FallbackChainProvider`. `AppContainer.build` installs it with `set_chain`; `get_chain()` in `app/core/model_router.py` reads it and builds a default one if none is installed. `reset_chain` forgets it, and `tests/conftest.py` calls it around every test.
- **What to show.** The lifespan in `app/main.py` builds the one container and keeps it:

  ```python
      container = AppContainer.build(supabase_client=supabase_client, cfg=_cfg, hooks=get_hooks())
      app.state.container = container
  ```

  And the lazy module-level singleton in `app/orchestrator/hooks.py`:

  ```python
  def get_hooks() -> ActionHooks:
      global _default_hooks
      if _default_hooks is None:
          _default_hooks = ActionHooks()
          register_all_hooks(_default_hooks)
      return _default_hooks
  ```

- **Which SOLID principle it supports, and why.** Dependency inversion, because of how we do it. A service never calls `AppContainer.getInstance()`; it receives what it needs through its constructor, typed as a port. A classic Singleton goes the other way: every caller names the concrete class.
- **Why not the simpler way (a classic class with `getInstance()`).** It hides what a class depends on, and tests could not build their own copy. Our tests do: `build_test_container` in `tests/fakes.py` calls `AppContainer.build` with its own `ActionHooks()` and `install_chain=False`. The old global registry was deleted, and `tests/test_injection.py::test_runtime_registry__gone_and_imported_nowhere` fails if it comes back.
- **Tests that pin it.** `tests/test_container.py::test_container__installs_its_chain_for_route_llm_call` and `tests/test_injection.py::test_runtime_registry__gone_and_imported_nowhere`.
- **What to admit.** We have a single instance by construction at one composition root, not a class that polices its own instance. Three process-wide objects are still looked up instead of passed in: `get_hooks()`, `get_chain()` and `module_registry` (`app/modules/__init__.py`). A caller cannot be handed a different one through its constructor. Docs 02 §5 and 04 §9 list this as a known shortfall; moving them into the container is the next step.

### Factory

- **The problem.** Which concrete classes to build depends on the environment: is Supabase reachable, is there a key for Claude? And a stored suggestion's action must be rebuilt from data, maybe hours later. If services chose for themselves, every service would import the database and model libraries.
- **The pattern in one line.** One place decides which concrete classes get created; everyone else just receives them.
- **Everyday comparison.** A restaurant kitchen. You order a meal; you do not choose the stove or the cook.
- **Where it is.**
  - **Abstract Factory:** `RepositoryFactory.supabase(client)` and `RepositoryFactory.in_memory()` in `app/infrastructure/persistence/factory.py`. Each returns a `Repositories` record holding a whole family (tasks, drafts, memories and the rest), so the two families can never be mixed.
  - **Factory:** `LLMProviderFactory.build` in `app/infrastructure/llm/factory.py` returns the `FallbackChainProvider`: the local model, then Claude (only when its key is configured), any `extra_cloud` providers, then Gemini, each wrapped in the decorators its location needs.
  - **Factory that rebuilds a stored command:** `ActionFactory` in `app/services/anticipation/service.py`. `register(kind, build)` adds a builder; `from_payload(payload)` rebuilds the action when the user accepts. An unknown kind raises `ValidationError`. `build_action_factory` (`app/services/anticipation/actions.py`) fills it.
  - All three run inside `AppContainer.build`, the composition root. The model chain is built there through `build_chain` in `app/core/model_router.py`, which calls `LLMProviderFactory.build`.
- **What to show.** The one place that picks the repository family, in `AppContainer.build` (shortened):

  ```python
          if supabase_client is not None:
              repos = RepositoryFactory.supabase(supabase_client, ...)
          else:
              repos = RepositoryFactory.in_memory()
  ```

- **Tests that pin it.** `tests/test_container.py::test_container__no_supabase__uses_in_memory_family`, `::test_container__with_fake_supabase__uses_supabase_family`, `tests/test_repository_contracts.py::test_factory__both_families__implement_every_port` and `tests/test_anticipation_service.py::test_action_factory__unknown_kind__validation_error`.
- **Which SOLID principle it supports, and why.** Dependency inversion first: services depend on ports, and only the factories, called from the root, name concrete classes (`tests/test_layering.py::test_layering__only_container_builds_repositories`). Open/closed second: a new action kind is one `register` call, and a new cloud provider goes in through `extra_cloud`.
- **Why not the simpler way.** An `if supabase: ... else: ...` inside each service would make every service import infrastructure, allow mixed families and stop tests from running offline. A global service locator hides dependencies. A DI framework (a library that wires objects for you) was rejected because a hand-written root is easier to read and explain.
- **What to admit.** Our design named a `TriggerFactory`; it does not exist. Triggers are registered directly with `AnticipationService.register_trigger` in the container. And the chain built by `LLMProviderFactory.build` is still installed process-wide with `set_chain`.

## Which pattern supports which SOLID principle

| Pattern | Main SOLID principle(s) | One-sentence reason, in Jarvis |
|---|---|---|
| Repository | Dependency inversion, Liskov | `TaskService` depends on the abstract `TaskRepository`, and the Supabase and in-memory families pass one contract suite, so either can stand in for the other. |
| Strategy | Open/closed, Dependency inversion | A new `LLMProvider` or `PatternDetector` is a new class, and callers hold the abstract class, so nothing that calls it is edited. |
| Decorator | Single responsibility, Open/closed | `RedactingProvider`, `RetryingProvider`, `MeteredProvider` and `TimeoutProvider` each add one concern by wrapping, without editing `ClaudeProvider`. |
| Chain of Responsibility | Open/closed, Single responsibility | A new consent rule is a new class added to `ConsentPolicy`'s list, and each rule decides one thing. |
| State (transition tables) | Single responsibility | The rule "which status change is legal" lives in `DRAFT_TRANSITIONS` and `DraftSchedule.transition`, not in checks spread over services. |
| Observer | Open/closed, Single responsibility | A new listener subscribes to `EventBus` without editing `TaskService`, which only records outcomes while `PatternObserver` does the learning. |
| Template Method | Open/closed | A new trigger is a new `Trigger` subclass that fills in the steps; `Trigger.run` is never edited, so consent is never skipped. |
| Factory / composition root | Dependency inversion | Only `AppContainer.build` and the factories it calls name concrete classes; every service receives abstractions. |
| Facade | Interface segregation (least knowledge) | The You page endpoint knows one method, `UserModelService.snapshot`, not the six subsystems behind it ("least knowledge" means a class talks only to its close collaborators). |
| Adapter | Dependency inversion, Open/closed | `ClaudeProvider` and `SupabaseTaskRepository` make a library fit our port, so the port never changes to fit a library, and a new vendor is a new adapter. |
| Command | Single responsibility, Open/closed | `ProactiveAction` keeps "decide now" apart from "run later", and a new kind is a new subclass plus one `register` call. |
| Singleton (our form) | Dependency inversion | One `AppContainer` is built at the root and passed in, and nothing calls a global `getInstance()`, so a test can build its own. |

## Questions you may get

1. **What is the difference between abstraction and encapsulation?**
   Abstraction hides *how* a job is done behind a small interface: `LLMProvider` shows three methods and hides the vendor, the network and retries. Encapsulation protects an object's *own data and rules*: `DraftSchedule` changes its status only through `transition`, which refuses illegal moves. In short, abstraction is about what the caller sees; encapsulation is about what the object guards.
2. **Composition or inheritance: which do you use, and where?**
   Composition by default. A cloud model is wrapped as Redacting(Retrying(Metered(Timeout(Claude)))) instead of subclasses like `RetryingRedactedClaude`, because four behaviours times three models would be too many classes. Inheritance is kept for truly shared behaviour: the `JarvisError` tree, the `_ProviderDecorator` base and the `_SupabaseRepository` base.
3. **Where is polymorphism?**
   Wherever a loop calls one method on many objects. `FallbackChainProvider` calls `complete` on each provider, `ConsentPolicy` calls `evaluate` on each rule, and `AnticipationScheduler` calls `run` on each `Sweep`. The loop never checks the type, so a new kind needs no change to the loop.
4. **Why are ports abstract classes (ABC) and not Python Protocols?**
   Decision ADR-2: an adapter must subclass its port, so "implements" in the class diagram is a real subclass, and a class that forgets a method cannot even be created. `tests/test_domain.py::test_ports__instantiated_directly__raise_type_error` pins it. One small exception: the `Redactor` port in `app/infrastructure/llm/decorators.py` is a Protocol.
5. **Show me the open/closed principle.**
   Write a `PatternDetector` subclass with `proposes_rule = True` and add it in `default_pattern_detectors` (`app/container.py`). `PatternRuleTrigger` then offers its pattern as a rule with no edit. `tests/test_anticipation_design.py::test_pattern_rule__new_detector_that_proposes_rules__offered_without_a_trigger_edit` proves it.
6. **Which SOLID principle is weakest in your code?**
   Interface segregation. `JarvisState` has over 40 keys and carries the whole container, so inside a turn any node can reach any service; it works like a service locator (an object you ask for any service, which hides what a class really needs). Next come the 6 raw-table files (dependency inversion) and the routing table a new module must edit (open/closed).
7. **Is there a Singleton? Why not the classic one?**
   Yes, by construction, not by a class. `AppContainer.build` runs once in the FastAPI lifespan, the result is kept on `app.state.container`, and it is passed in. A classic `getInstance()` hides dependencies and stops tests from building their own; our tests build a fresh container with `build_test_container` (`tests/fakes.py`).
8. **Factory or Abstract Factory: what is the difference here?**
   A Factory builds one product: `LLMProviderFactory.build` returns the model chain, and `ActionFactory.from_payload` rebuilds one stored action from its `kind`. An Abstract Factory builds a whole family of related objects: `RepositoryFactory.supabase` or `.in_memory` returns every repository at once, so a Supabase task repository is never mixed with an in-memory draft repository.
9. **Which SOLID principles does Strategy use?**
   Open/closed and dependency inversion. Callers depend on the abstract `LLMProvider` or `PatternDetector`, not on a concrete class. A new model or detector is a new class, and no `if model == "claude": ... elif ...` block has to be edited.
10. **Which SOLID principles does Decorator use, and why not subclasses?**
    Single responsibility and open/closed: each of `RedactingProvider`, `RetryingProvider`, `MeteredProvider` and `TimeoutProvider` adds one concern, and we add behaviour by wrapping, without editing `ClaudeProvider`. Subclasses would multiply, and the local model needs only two of the four (metering and timeout).
11. **Which SOLID principles does Repository use?**
    Dependency inversion: services such as `TaskService` depend on `TaskRepository`, never on Supabase. Liskov substitution: both families pass the same contract tests in `tests/test_repository_contracts.py`, so it is shown by a test, not just claimed.
12. **Why Template Method for the triggers?**
    So no trigger can skip a step. `Trigger.run` (`app/domain/anticipation.py`) fixes the order: is it due, already done today, gather facts, build the suggestion, ask consent, store. `MorningBriefTrigger` and `PatternRuleTrigger` fill in the steps but never override `run`, so consent always runs before anything is stored.

## Say these honestly

1. **Entities can be changed directly.** `DraftSchedule` is an ordinary, changeable Pydantic model, so code could write `draft.status = ...` and skip `transition`. It is safe in practice because every stored status is checked again where it is written: the accept runs under a row lock, and suggestion and memory writes are compare-and-set (the write succeeds only if the row is still in the state we read). The fix, `frozen=True`, was not done because some services adjust a field before saving.
2. **Interface segregation is our clearest gap.** `JarvisState` has over 40 keys and carries the whole container, so inside a turn any node can reach any service. Only the planning module gets a smaller record (`PlanningServices`).
3. **Three process-wide lookups remain.** `get_hooks()`, `get_chain()` and `module_registry` are single instances, but they are looked up, not passed in. Tests can replace the chain (`set_chain`, `reset_chain`), but a constructor cannot be given a different one.
4. **Six files still call database tables directly**, for data that has no repository yet. `tests/test_layering.py` holds the exact list, and the list can only shrink.
5. **Some planned design was not built.** The `CognitiveModule` registry (so a new module edits `INTENT_TO_MODULE` and the graph), the `TriggerFactory` and a general Unit of Work do not exist. And `FallbackChainProvider` adds `skip_local` to the `LLMProvider` methods, a small Liskov gap.
