# Amathziah: sequence and activity diagrams, class diagrams, database diagrams

You present how Jarvis behaves over time (slide 6, after Murali opens with the chat turn), how the classes fit together (slide 7), and the database diagrams on slide 15 (the ER diagram and the state machines; Ravi presents the schema itself).
Your two design patterns are **Observer** (`EventBus`) and **Template Method** (`Trigger.run`).
Jarvis is a personal intelligence for one person: it learns how they work and has things ready before they ask. Never call it a calendar or a planner.

## Read in this order

| # | Document | Sections | Minutes |
|---|---|---|---|
| 1 | This file | all | 10 |
| 2 | `docs/capstone/05-design-patterns.md` | §0 (the Observer and Template Method parts), §7, §8 | 10 |
| 3 | `docs/capstone/06-sequence-and-activity.md` | S2a, S4, S6 (diagram, then skim the table), A3, "State machines" | 15 |
| 4 | `docs/capstone/03-class-diagrams.md` | §1.3, §2 (overview and its table), §3.4, §7.1 | 8 |
| 5 | `docs/capstone/08-database-schema.md` | §2 (ER diagram), §4.1, §4.2, §5 | 7 |
| 6 | `docs/capstone/00-team-study-guide.md` | §3, §4, §10 (C2 and C6) | 5 |

Total: about 55 minutes. You can skip the `path:line` columns. They are proof for questions, not for speaking.

## What to say

### Slide 6: sequence and activity diagrams

Open with: "Murali showed one chat turn. I will show three more: a double click on Accept, how a Skip becomes learning, and how Jarvis makes the first move in the morning."

**S2a: the double click on Accept.**
"Accepting a draft is the only way a plan becomes real tasks. Here two Accept requests for the same draft arrive together, from a double click or two tabs. Both go through `accept_on_time` and `DraftService.accept`, and both pass the quick check in Python, because both read the draft as pending. So Python alone cannot stop a double write. The real guard is one database function, `accept_draft`, which runs as one transaction. The first call locks the user's profile row, then the draft row. The second call waits on the profile row. The first call writes the tasks, marks the draft accepted and commits. The second call then reads 'accepted', fails with `DRAFT_NOT_OPEN`, and the user gets a 409. The tasks are written exactly once."

If asked why the profile row is locked first: the function also updates the user's other drafts, so locking the draft first could let two accepts deadlock (`tests/test_migrations.py::test_migrations__accept_draft__locks_the_user_before_the_draft`).

**S4: Complete or Skip, then learning in the background.**
"The user clicks Skip on the dashboard. `TaskService.skip` moves the task from pending to skipped with a compare-and-set write (the write happens only if the status is still the one we read). It appends one row to `task_events`, then publishes a `TaskSkipped` event on the `EventBus`. The `PatternObserver` hears it, asks for a background run of PEARL (our pattern learner), and returns at once. So the user gets the 200 answer and a blame-free message without waiting. In the background, PEARL reads the last 28 days of outcomes. If it finds a pattern, it saves it as a memory and holds a learning moment. On the user's next chat message, that moment is sent as a `pattern_detected` frame, before `complete`. This is the Observer pattern at work."

**S6: the morning brief.**
"Here the first actor is not the user. It is the clock. Every 60 seconds the `AnticipationScheduler` ticks. For each user, `AnticipationService.run_due` runs `MorningBriefTrigger` through `Trigger.run`, our Template Method. The trigger checks that now is between wake time and wake time plus 6 hours, and that no brief was made today. It gathers the pending tasks and the slots that passed since yesterday, and drafts today's plan with the same planning sub-graph a chat turn uses. The words are built from facts, with no model call. Then `ConsentPolicy` decides: this changes the schedule, so Jarvis asks first. The suggestion is stored as pending and the dashboard shows it. When the user accepts, the suggestion is claimed first, then `ApplyDraftAction` checks the draft is still current and accepts it through the same atomic accept as S2a."

Close with one line on activity diagrams: "A1, A2 and A3 are activity diagrams: the flow of steps and decisions inside one process. A3 shows every ending of `Trigger.run`, including deny, which stores nothing."

### Slide 7: class diagrams

"Doc 03 has one overview diagram and fourteen subsystem diagrams. The overview is centred on `AppContainer`, the composition root: the one place that creates the concrete classes. A filled diamond is composition: the container creates the event bus, holds it alone, and it lives as long as the container. An empty diamond is aggregation: an object is handed in and shared, like the `DraftService` inside `PlanningServices`. A solid triangle is inheritance, like `Trigger` and `MorningBriefTrigger`. A dotted triangle is realization: a class implementing an interface, like `PatternObserver` implementing `EventListener`. Every interface is a real Python abstract class. The most important point: these diagrams are tested. `tests/test_design_conformance.py` reads every diagram and checks each class, arrow, method and attribute against the code. If a diagram lies, the build fails."

### Slide 15: the database diagrams

"The ER diagram shows all 19 tables. `user_profiles` is the root: every other table has a `user_id` that points to it. Solid lines are real foreign keys. Dashed lines marked SOFT are links with no foreign key, on purpose. The best example is `task_events.task_id`. Every accept deletes and re-inserts the pending tasks, so a foreign key would either delete the history PEARL learns from or block the accept. The history must outlive the row. Each stored class in `app/domain/entities.py` maps to one table; some, like `Pattern` and `FocusProfile`, are computed and never stored. Then the four state machines: draft, task, suggestion and memory. Each is drawn from a transition table in the code. A move that is not in the table is refused with a 409 before anything is written, and the SQL CHECK constraints match the Python enums. A test checks that."

## The diagrams, one line each

| Id | What it shows | The one thing to point at |
|---|---|---|
| S1 | A brain dump becomes a proposed plan | It ends in a PENDING draft; `user_tasks` is not touched |
| S2a | Accept, and two accepts at once | The lock order in `accept_draft`; the second call gets 409 |
| S2b | An accept that comes after some times have passed | The draft's own tasks are re-solved from now first |
| S3 | The user teaches a rule, then plans again | The rule becomes a hard block for the solver |
| S4 | Complete or Skip, then PEARL in the background | The `par` block: the click returns before PEARL runs |
| S5 | Forget a fact, then say it again | The tombstone (words erased, row kept) makes the new copy suppressed |
| S6 | The morning brief | The Clock starts it; `ConsentPolicy` answers "ask" |
| S7 | A pattern becomes a rule proposal | `PatternDetected` reaches `AnticipationListener`; accept stores one rule |
| S8 | Every language-model call | Redact, retry, meter, timeout around each cloud model; `LLMCallCompleted` to `UsageLedger` |
| A1 | The chat turn graph | `classify_intent` uses rules, no model call; learning runs before the end |
| A2 | The planning sub-graph | `decompose_goal` waits for both branches (AND-join); if no plan fits, the horizon widens: 3, 5, 7, 14, 30 days |
| A3 | `Trigger.run` from start to end | Consent comes before storage; deny stores nothing |
| Draft | pending, modified, accepted, rejected, expired, superseded | `accepted` has no exits, so a second accept is illegal |
| Task | pending, completed, skipped, missed | missed to completed: "I actually did that" |
| Suggestion | pending, accepted, dismissed, expired, auto_applied | accepted back to pending when the action failed |
| Memory | active, faded, dismissed, forgotten | `forgotten` has no exits: nothing brings it back |

Note: doc 06's S4 diagram says "the last 30 days". The code uses `FOCUS_WINDOW_DAYS`, which is 28. If asked, say 28.

## How to read our class diagrams

| Kind | Arrow | Meaning | Real Jarvis example (doc 03) |
|---|---|---|---|
| Inheritance | `A <\|-- B` | B extends a class that has behaviour of its own | `Trigger <\|-- MorningBriefTrigger`; `DomainEvent <\|-- TaskCompleted` |
| Realization | `A <\|.. B` | B implements an interface (an abstract class whose methods are all abstract) | `EventListener <\|.. PatternObserver`; `LLMProvider <\|.. FallbackChainProvider` |
| Composition | `A *-- B` | A creates B, only A holds it, B lives as long as A | `AppContainer *-- EventBus : events` |
| Aggregation | `A o-- B` | A holds B, but B was handed in and is shared or outlives A | `PlanningServices o-- DraftService : drafts` |
| Association | `A --> B` | A keeps a lasting reference that is not whole and part, often an id | `Task --> Goal : goal_id`; `ChatTurn --> ChatTurnService : _service` |

There is also a dashed arrow, `A ..> B`, a dependency: A uses B for one call only (for example `RepositoryFactory ..> Repositories : creates`).

Two quick rules to remember:
- Composition or aggregation? Ask "who created it, and who else holds it?" Created and held only by A: composition. Handed in through the constructor and shared: aggregation.
- Every port (interface) is an `abc.ABC`, so the "implements" arrow is a real subclass, and a class that forgets a method fails when it is created (`tests/test_domain.py::test_ports__instantiated_directly__raise_type_error`).

How `tests/test_design_conformance.py` proves the diagrams match the code:
- It parses every Mermaid class diagram in doc 03. Every class named must import from the module in the class index.
- Every `<<interface>>` must be an `abc.ABC` with abstract methods. Every enum must list exactly its real members, in order.
- Every inheritance or realization arrow must be a real `issubclass`.
- Every composition, aggregation or association arrow must be labelled with a real attribute of the class it starts from.
- Every method drawn must exist with its real parameter names.
- A line the parser does not understand fails too (`test_conformance_parser__unknown_line__raises_rather_than_skips`), and `test_conformance_check__diagram_that_lies__every_lie_reported` proves the checker catches a wrong diagram.

## Your two patterns: Observer and Template Method

### Observer

- **The problem.** When the user completes or skips a task, PEARL should look for new patterns. The click must not wait for that, and must not fail if learning fails. The same need appears elsewhere: a finished model call must be recorded for cost, and a new pattern should lead to a rule proposal.
- **The pattern in one line.** When something happens, announce it; whoever is interested reacts. The announcer does not know who listens.
- **Everyday comparison.** A YouTube channel. The creator uploads once and the subscribers are notified. The creator does not message each one.
- **Where.** `EventBus` in `app/domain/events.py`. Listeners implement `EventListener.handle`. The subscriptions are made in `AppContainer.build` (`app/container.py`) with `events.subscribe(...)`.
- **Safety.** `EventBus.publish` wraps each listener in try/except. A failing listener is logged, and the original action still succeeds.

| Event | Published by | Listener | What the listener does |
|---|---|---|---|
| `TaskCompleted`, `TaskSkipped`, `TaskMissed` | `TaskService._record` | `PatternObserver` | Runs PEARL for that user in the background, one run at a time (`PerUserSingleFlight`) |
| `PatternDetected` | `PatternService._look_now` | `AnticipationListener` | Runs `PatternRuleTrigger` to propose a rule |
| `LLMCallCompleted` | `MeteredProvider` | `UsageLedger` | Writes the cost row in the background |
| `DraftAccepted` | `DraftService.accept`, after the commit | `DocumentLinkListener` | Links the new tasks to the user's documents |

- **SOLID.** *Open/Closed:* a new reaction is a new `EventListener` subclass plus one `subscribe` line; `TaskService` is not edited. *Single Responsibility:* `TaskService` only records the outcome. Learning belongs to `PatternService`, cost to `UsageLedger`. Each class has one reason to change.
- **Why not the simpler way?** If `TaskService` called PEARL directly, finishing a task would depend on the learning code, would be slower, and would break when learning broke. A message broker (Redis, Celery) is not needed for one server process.
- **Proof (tests).** `tests/test_domain.py::test_event_bus__failing_listener__others_still_run`; `tests/test_patterns.py::test_pattern_observer__complete__returns_without_waiting_for_observe` and `::test_container__task_outcomes__each_reaches_pattern_observer`.
- **What to admit.** `DeadlineMissed` is published but nothing in production listens to it. `publish` awaits each listener in turn: most hand their work to the background at once, but `DocumentLinkListener` works inline, so an accept answers after the linking.

### Template Method

- **The problem.** Every proactive trigger must follow the same order and must never skip the consent step. If each trigger wrote its own order, one could forget a step.
- **The pattern in one line.** A base class fixes the order of the steps; subclasses fill in the steps but cannot change the order.
- **Everyday comparison.** A recipe card with blanks. Everyone follows "prepare, cook, serve" in that order; only the dish changes.
- **Where.** `Trigger.run` in `app/domain/anticipation.py`. The subclasses are in `app/services/anticipation/triggers.py`.
- **The fixed order in `Trigger.run`:**
  1. `should_fire`: is it time? If not, "not_due" (a forced run skips this step).
  2. `already_done`: was a suggestion already made today? If so, "already_done".
  3. `gather`: is there anything to say? `None` means "nothing_to_say": no model call, nothing stored.
  4. `build_suggestion`: the text and the action behind it.
  5. Load the consent context, then `consent.evaluate`. Deny: nothing is stored.
  6. Store the suggestion: pending (Jarvis asks, or it is read-only information), or already claimed (an allowed change).
  7. An allowed change runs at once and becomes auto_applied; if it fails, it goes back to pending.

| Step | `MorningBriefTrigger` | `PatternRuleTrigger` |
|---|---|---|
| `should_fire` | Wake time to wake time plus 6 hours, on the user's clock | Always true |
| `already_done` | Base version: a brief already made today | Overridden: always false (`gather` skips what was already proposed) |
| `gather` | Pending tasks and slots passed since yesterday | A qualifying pattern not yet proposed |
| `build_suggestion` | Usually an `ApplyDraftAction` for today's draft | An `AdoptRuleAction` that keeps the hour clear |

- **SOLID.** *Open/Closed:* a new trigger is a new subclass registered with `register_trigger` in `AppContainer.build`; `Trigger.run` and the API are not edited (`test_anticipation_api__run_a_newly_registered_trigger__no_api_edit_needed`). It also protects the consent step: consent lives in `run`, which subclasses never override, so no trigger can skip it.
- **Why not the simpler way?** Passing the steps as functions to one generic trigger has the same power, but the rule "consent before storage" would no longer be visible in one method of one class. A job library per trigger (APScheduler, Celery) adds moving parts.
- **Proof (tests).** `tests/test_trigger_template.py` has 13 tests on the skeleton, for example `test_trigger_run__already_done__gather_never_called` and `test_trigger_run__consent_loaded_only_when_a_run_reaches_it`.
- **What to admit.** Only two triggers are built. PreTaskPrep and DeadlineRisk are in the spec but not built.

## Where the code is

| Idea | File | What to point at |
|---|---|---|
| The event bus | `app/domain/events.py` | `EventBus.publish`: the try/except around each listener |
| Who listens to what | `app/container.py` | The `events.subscribe(...)` lines in `AppContainer.build` |
| PEARL listener | `app/services/memory/patterns.py` | `PatternObserver.handle` asks for a run and returns |
| Rule-proposal listener | `app/services/anticipation/triggers.py` | `AnticipationListener.handle` |
| Where outcomes are published | `app/services/task_service.py` | `TaskService._record` |
| The template | `app/domain/anticipation.py` | `Trigger.run`, and the abstract `should_fire`, `gather`, `build_suggestion` |
| The two triggers | `app/services/anticipation/triggers.py` | `MorningBriefTrigger`, `PatternRuleTrigger` |
| The consent chain | `app/domain/anticipation.py` | `ConsentPolicy.default` |
| Draft, suggestion, memory state machines | `app/domain/entities.py` | `DRAFT_TRANSITIONS`, `SUGGESTION_TRANSITIONS`, `MEMORY_TRANSITIONS`, `DraftSchedule.transition` |
| Task state machine | `app/services/task_service.py` | `_COMPLETE`, `_SKIP`, `_DEADLINE_MISS`, `_SLOT_MISS` |
| The atomic accept | `supabase/migrations/20261115000007_user_tasks_not_before.sql` | `for no key update` (user row), then `for update` (draft row) |
| Class to table mapping | `app/infrastructure/persistence/_mapping.py` | The shared rules both repository families use |
| Proof: class diagrams | `tests/test_design_conformance.py` | `test_conformance_check__diagram_that_lies__every_lie_reported` |
| Proof: template, double click | `tests/test_trigger_template.py`, `tests/test_draft_service.py` | `test_trigger_run__deny__nothing_stored_decision_returned`; `test_accept__concurrent_double_click__exactly_one_succeeds` |

## Questions you may get

1. **How do you know the diagram matches the code?** `tests/test_design_conformance.py` parses every class diagram in doc 03 and checks each class, interface, arrow, method and attribute against the real code. A line it cannot parse also fails. So a diagram that lies breaks the build.
2. **Composition or aggregation: one example each.** Composition: `AppContainer *-- EventBus`. The container creates the bus, holds it alone, and it lives as long as the container. Aggregation: `PlanningServices o-- DraftService`. The `DraftService` is created elsewhere, handed in, and shared (the container and the action factory hold the same object).
3. **Walk through a planning chat turn.** The router validates the request, then `ChatTurnService.open_turn` opens the turn and the conversation lock is taken. Under the lock the context is read; then the graph runs: extract (one fast model call), classify by rules, plan, solve with CP-SAT, propose a draft. The reply streams, the observation loop sends learning frames, the message is saved, one `complete` frame goes out, and a shielded cleanup releases the lock.
4. **What does the observation loop do, and when?** It runs inside the turn, before `complete`. It remembers the facts the user stated (from the same extraction call, so no extra model call), lets PEARL look for at most 0.5 seconds, and sends `memory_extracted` and `pattern_detected` frames.
5. **Why Observer and not a direct call?** A Complete click should not wait for learning or fail because of it. The bus hands the event to listeners; a failing listener is logged and the click still succeeds. The emitter also never needs editing when a new reaction is added.
6. **Why Template Method for triggers?** So no trigger can skip a step. Consent always runs before storage, and a claim always comes before execution. The order lives in `Trigger.run`, which subclasses never override.
7. **What happens on a double click?** On a draft Accept, both requests reach the database; row locks let the first commit, and the second reads "accepted" and gets a 409, so tasks are written once. On a suggestion Accept, the first click claims it (pending to accepted by compare-and-set), so the action runs once and the second click gets a 409. On a task Complete, the second click writes nothing.
8. **Sequence diagram or activity diagram: what is the difference?** A sequence diagram shows which objects send which messages to each other, in time order from top to bottom (S1 to S8). An activity diagram shows the flow of steps and decisions in one process, like a flowchart (A1 the chat graph, A2 the planning sub-graph, A3 `Trigger.run`).
9. **Why is the Clock an actor?** The `AnticipationScheduler` starts use cases with no user request: the morning brief and the slot sweep. Without it, Jarvis could never make the first move.
10. **Which SOLID principle does Observer use?** Mainly Open/Closed: a new reaction is a new listener class plus one `subscribe` line, with no edit to the emitter. It also supports Single Responsibility: `TaskService` records outcomes, and learning and cost tracking live in their own classes.
11. **Why does `task_events.task_id` have no foreign key?** Each accept deletes and re-inserts the pending tasks. A foreign key would either cascade-delete the outcome history PEARL learns from, or block the accept. The history must outlive the task row.
12. **Why are the state machines tables, not one class per state?** The states have no behaviour of their own; the only question is "is this move allowed?". A table answers that in a few lines, and the same table gives each compare-and-set guard in the database writes.

## Say these honestly

1. `DeadlineMissed` is published but has no production listener, and the spec's `TelemetryListener` was not built.
2. Only `MorningBriefTrigger` and `PatternRuleTrigger` exist. PreTaskPrep and DeadlineRisk are not built. Ask-back is not built, so it is not drawn.
3. Everything runs in one server process: the event bus, the scheduler loop, the per-user locks and the single-flight runs live in memory. A second worker could make a second brief. The cases that could corrupt data are still guarded in the database (the accept transaction, compare-and-set writes, unique keys).
4. The class diagrams leave some things out on purpose (doc 03 §10): functions such as `route_llm_call` and `run_schedule`, and process-wide singletons such as the installed model chain.
5. Three older foreign keys do not include `user_id` (`chat_messages` to `chat_sessions`, `habit_trackers` to `behavioral_constraints`, `user_memories.superseded_by`), so for those the database does not check that both rows belong to the same user. Also, a draft past its `expires_at` keeps the status `pending`; expiry is checked only when someone tries to accept it.
