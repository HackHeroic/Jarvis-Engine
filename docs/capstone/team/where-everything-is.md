# Where everything is

A map of the code for the evaluation. Each row names one file to open and what to point at. All paths are inside `Jarvis-Engine/`. The long explanations are in `docs/capstone/04-oop-and-solid.md` and `05-design-patterns.md`.

## 1. The layers (architecture)

| Layer | Folder | What lives there |
|---|---|---|
| API | `app/api/` | Routers only: read the request, call a service, return a typed model |
| Services | `app/services/` | The application logic: chat turn, drafts, tasks, memory, anticipation |
| Orchestrator | `app/orchestrator/`, `app/modules/` | The LangGraph turn graph and the planning sub-graph |
| Core | `app/core/` | `route_llm_call`, the CP-SAT solver, settings |
| Domain | `app/domain/` | Entities, ports (abstract classes), events, errors, consent |
| Infrastructure | `app/infrastructure/` | Supabase and in-memory repositories, model providers, the embedder |
| Composition root | `app/container.py` | `AppContainer.build`: the one place that picks concrete classes |

The rule: dependencies point inward, toward the domain. `tests/test_layering.py` checks every import.

## 2. OOP: the four pillars

| Pillar | What it means | Class | File | Point at |
|---|---|---|---|---|
| Encapsulation | An object protects its own state | `DraftSchedule` | `app/domain/entities.py` | `transition()` is the only way to change a draft's status; an illegal move raises a 409 |
| Encapsulation | | `ConsentPolicy` | `app/domain/anticipation.py` | The constructor refuses a chain whose last rule is not `ModePolicyRule` |
| Abstraction | Callers see what, not how | `LLMProvider` | `app/domain/ports.py` | A few abstract methods; no vendor, no retry, no network detail |
| Abstraction | | `TaskRepository` and the other ports | `app/domain/ports.py` | Services store data without knowing Supabase exists |
| Inheritance | A subclass reuses and specialises | `JarvisError` tree | `app/domain/errors.py` | Each subclass carries its own `code` and HTTP status; handlers catch the base |
| Inheritance | | `_ProviderDecorator` | `app/infrastructure/llm/decorators.py` | The four decorators share one base |
| Inheritance | | `Trigger` | `app/domain/anticipation.py` | `MorningBriefTrigger` and `PatternRuleTrigger` (`app/services/anticipation/triggers.py`) extend it |
| Polymorphism | One call, many behaviours | `FallbackChainProvider.complete` | `app/infrastructure/llm/chain.py` | One loop calls `complete` on any provider |
| Polymorphism | | `ConsentPolicy.evaluate` | `app/domain/anticipation.py` | One loop calls `evaluate` on any rule |
| Polymorphism | | `PatternDetector` subclasses | `app/services/memory/patterns.py` | PEARL runs every detector the same way |

## 3. SOLID: one real example each

| Principle | In one line | Example | File | Where we fall short |
|---|---|---|---|---|
| **S** Single responsibility | One class, one reason to change | The chat router only routes; `SseEncoder` is the one place a frame is written | `app/api/v1/endpoints/chat.py`, `app/schemas/chat_stream.py` | `app/modules/planning_graph.py` holds every planning step in one long file |
| **O** Open/closed | Add a class, do not edit old code | A new `PatternDetector` is offered as a rule with no change to the trigger | `app/services/memory/patterns.py`, `app/container.py` (`default_pattern_detectors`) | A new chat module still edits the routing table in `app/orchestrator/routing.py` |
| **L** Liskov substitution | Any implementation can replace another | Supabase and in-memory repositories pass the same tests; `NullEventBus` replaces `EventBus` | `tests/test_repository_contracts.py`, `app/domain/events.py` | `FallbackChainProvider` adds methods that `LLMProvider` does not have |
| **I** Interface segregation | Depend only on what you use | The planning sub-graph gets `PlanningServices` (a small record), not the whole container; `Sweep`, `TextEmbedder`, `EnergyEstimator` have one abstract method each | `app/services/planning/services.py`, `app/domain/ports.py` | `JarvisState` (`app/orchestrator/state.py`) is large and carries the whole container |
| **D** Dependency inversion | Depend on abstractions; one root wires the real classes | `TaskService` takes repositories in its constructor; `MorningBriefTrigger` takes the `DraftPlanner` port and gets `PlanningGraphReplanner` | `app/services/task_service.py`, `app/modules/planning_replan.py`, `app/container.py` | A few files still call the database table directly; `get_chain` and `get_hooks` are process-wide lookups |

Tests that fail if a principle breaks: `tests/test_chat_turn.py` (S), `tests/test_patterns.py` (O), `tests/test_repository_contracts.py` (L), `tests/test_layering.py` and `tests/test_injection.py` (D).

## 4. Design patterns: where each one is

| Pattern | Class to name | File | Presented by |
|---|---|---|---|
| Repository | `TaskRepository` and ten more ports; `SupabaseTaskRepository`, `InMemory*` | `app/domain/ports.py`, `app/infrastructure/persistence/` | Ravi |
| Adapter | `ClaudeProvider`, `SupabaseTaskRepository`, `MiniLMEmbedder` | `app/infrastructure/llm/claude.py`, `app/infrastructure/persistence/supabase_repos.py`, `app/infrastructure/embedding.py` | Ravi |
| Strategy | `LLMProvider` (three providers), `PatternDetector` (four detectors), `EnergyEstimator` (two) | `app/domain/ports.py`, `app/services/memory/patterns.py`, `app/services/energy.py` | Murali |
| State (transition tables) | `DRAFT_TRANSITIONS`, `SUGGESTION_TRANSITIONS`, `MEMORY_TRANSITIONS` | `app/domain/entities.py` | Murali |
| Observer | `EventBus`, `PatternObserver`, `UsageLedger`, `AnticipationListener` | `app/domain/events.py`, `app/services/memory/patterns.py` | Amathziah |
| Template Method | `Trigger.run` | `app/domain/anticipation.py` | Amathziah |
| Singleton (one instance, built once) | `AppContainer` on `app.state`; `get_hooks()`; `get_chain()` | `app/container.py`, `app/main.py`, `app/orchestrator/hooks.py`, `app/core/model_router.py` | Pugal |
| Factory | `RepositoryFactory`, `LLMProviderFactory`, `ActionFactory` | `app/infrastructure/persistence/factory.py`, `app/infrastructure/llm/factory.py`, `app/services/anticipation/service.py` | Pugal |
| Decorator | `RedactingProvider`, `RetryingProvider`, `MeteredProvider`, `TimeoutProvider` | `app/infrastructure/llm/decorators.py` | anyone |
| Chain of Responsibility | `FallbackChainProvider`; `ConsentPolicy` | `app/infrastructure/llm/chain.py`, `app/domain/anticipation.py` | anyone |
| Facade | `UserModelService.snapshot`, `ChatTurnService` | `app/services/user_model_service.py`, `app/services/chat_turn/service.py` | anyone |
| Command | `ProactiveAction` and its three actions | `app/domain/anticipation.py`, `app/services/anticipation/actions.py` | anyone |

## 5. Which pattern supports which SOLID principle

The question "which SOLID principle does this pattern use?" has the same shape of answer every time: name the principle, then say what would break without the pattern.

| Pattern | Main principle | Why, in Jarvis |
|---|---|---|
| Strategy | **Open/closed**, also Dependency inversion | Claude was added as a new `LLMProvider` class; the code that asks a question did not change. Callers hold the abstract class, not Claude. |
| State tables | **Single responsibility** | The rule "which status change is legal" lives in one table and one method, not in `if` checks spread over endpoints. |
| Repository | **Dependency inversion**, also Liskov | Services depend on the abstract `TaskRepository`; the container gives them Supabase or in-memory. Both pass the same tests, which is Liskov shown, not claimed. |
| Adapter | **Dependency inversion**, also Open/closed | Our ports never change to fit a library. A new library is a new adapter class. |
| Observer | **Open/closed**, also Single responsibility | A new listener subscribes to the bus; `TaskService` is not edited. `TaskService` records outcomes and nothing else. |
| Template Method | **Open/closed** | A new trigger is a new subclass that fills three steps; `Trigger.run` is never edited, so consent can never be skipped. |
| Factory and the composition root | **Dependency inversion** | Only `AppContainer.build` names concrete classes; every service receives abstractions. |
| Singleton (one instance, built once) | **Dependency inversion**, by how we do it | The single `AppContainer` is built at the root and passed in. Nothing calls a global `getInstance()`, so a test can build its own. |
| Decorator | **Single responsibility**, also Open/closed | Redact, retry, meter and timeout are one class each. A new concern is a new wrapper; no provider is edited. |
| Chain of Responsibility | **Open/closed**, also Single responsibility | A new consent rule is a new class added to the list. Each rule decides one thing. |
| Facade | **Interface segregation** (the caller sees one small surface) | The You page endpoint calls `snapshot()` and knows nothing about the six subsystems behind it. |
| Command | **Single responsibility**, also Open/closed | An action knows how to describe, store and run itself. A new kind of action is a new class. |

## 6. The topics people ask about most

| Topic | File | Function or class |
|---|---|---|
| The one door to any AI model | `app/core/model_router.py` | `route_llm_call` |
| One chat turn | `app/services/chat_turn/service.py` | `ChatTurnService`, `ChatTurn.frames` |
| The turn graph | `app/orchestrator/graph.py` | `build_jarvis_graph` |
| Planning steps | `app/modules/planning_graph.py` | `decompose_goal`, `fuse_tasks`, `solve_schedule`, `create_draft` |
| The CP-SAT solver | `app/core/or_tools/solver.py`, `app/services/planning/scheduling.py` | `JarvisScheduler`, `run_schedule` |
| Draft then atomic accept | `app/services/draft_service.py`, `supabase/migrations/` | `DraftService.propose`, `DraftService.accept`, SQL `accept_draft()` |
| Task outcomes and history | `app/services/task_service.py` | `TaskService`, `SlotPassedSweep` |
| Memory | `app/services/memory/memory_service.py` | `MemoryService.remember`, `recall`, `forget`; `MemoryDecaySweep` |
| PEARL (pattern detection) | `app/services/memory/patterns.py` | `PatternDetector`, `PatternService`, `PatternObserver` |
| Focus profile | `app/services/energy.py` | `HistoryEnergyEstimator` |
| Anticipation | `app/services/anticipation/` | `AnticipationScheduler`, `MorningBriefTrigger`, `PatternRuleTrigger` |
| Consent | `app/domain/anticipation.py` | `ConsentPolicy`, `Trigger.run` |
| Errors | `app/domain/errors.py`, `app/api/error_handlers.py` | `JarvisError` and its subclasses |
| The task workspace | `app/services/analytical/workspace_builder.py` | `build_task_workspace` |
