# Murali: the opening, the architecture, and the brain (anticipation, memory, CP-SAT, PEARL)

You open the presentation and you close the design part. Slides **1 to 6** (problem, what Jarvis is, a day with Jarvis, requirements, architecture, the chat turn) and slides **11, 12, 13** (anticipation, memory, the CP-SAT solver, with PEARL). Your two design patterns are **Strategy** and **State**.

The map of all the code, for any OOP, SOLID or pattern question, is [../where-everything-is.md](../where-everything-is.md). Keep it open.

## Read in this order (about 70 minutes)

| What | Where | Minutes |
|---|---|---|
| Jarvis in one page, the Meera story | `00-team-study-guide.md` §1 | 5 |
| The big picture and the layers | guide §2; `02-architecture.md` | 10 |
| The life of one chat turn | guide §3; `06-sequence-and-activity.md` S1 | 10 |
| Requirements: actors and use cases | `01-requirements.md` §4 and UC1–UC7 | 5 |
| The life of one anticipation tick; consent | guide §4, D17, D18, D19 | 10 |
| Memory | guide D13, D14, D35 | 10 |
| CP-SAT and the user's clock | guide D20, D21 | 10 |
| PEARL and the focus profile | guide D16, D33 | 5 |
| Your patterns in plain words | `05-design-patterns.md` §0 (Strategy, State) | 5 |

## What to say

**Slides 1–3: the problem and what Jarvis is.** Every tool today waits to be asked. The person with too much on their mind has to do the organising themselves. Jarvis is different: it builds a model of one person (when they focus, what they put off, what they told it) and uses that model to have things ready before they ask. It is not a calendar and not a planner. Planning is one tool it uses. The model of the person is the brain. Then tell the Meera story in four beats: a morning brief she did not ask for; a brain dump that becomes a draft and is saved only when she accepts; a pattern Jarvis noticed at 9 AM and offered as a rule; a memory she asked it to forget, which stays forgotten.

**Slide 4: requirements.** We have 37 functional and 16 non-functional requirements, eight use cases and six actors. The unusual actor is the **Clock**. It starts use cases with no user request, which is what makes Jarvis proactive. Every requirement is traced to the code and to a test.

**Slide 5: architecture.** One FastAPI process, built in layers. Dependencies point inward to the domain. The API layer only routes. Services hold the logic. The domain holds entities and abstract ports. Infrastructure implements the ports. One file, `app/container.py`, chooses every concrete class. A test reads every import and fails if a layer breaks the rule.

**Slide 6: the chat turn.** The browser posts to one endpoint and gets a stream of typed frames back. The steps: validate, take the conversation lock, read history and memories, run the graph (extract, classify, plan), reply, learn inside the turn, send one `complete` frame, release the lock. For a planning message the model only breaks the goal into tasks. The solver places them. The result is a draft; nothing is saved until the user accepts.

**Slide 11: anticipation.** The actor is a clock that ticks every 60 seconds. First it runs sweeps, for example marking a task missed when its slot passed with no outcome. Then it runs triggers for each user. Every trigger follows one fixed order in `Trigger.run`: is it due, was it already done today, gather the facts, build the suggestion, **ask consent**, store. Consent is a chain of four rules. Anything that changes the schedule asks first. When the user accepts, the suggestion is claimed first and then run, so a double click runs it once.

**Slide 12: memory.** Jarvis remembers what the user says, with the label "you said". A memory has a status: active, faded, dismissed or forgotten. When a new fact arrives, `remember` does one of four things: ignore it if the user already forgot it, replace a memory it contradicts, strengthen one it repeats, or create it. **Forget makes a tombstone**: the words are erased but a fingerprint is kept, so the same fact said again is recognised and not stored. Memories fade over weeks unless used. For each message, recall picks only the relevant memories, within a token budget, by similarity search inside Postgres.

**Slide 13: CP-SAT.** A language model asked to schedule makes overlaps and breaks rules. So the model never schedules. A constraint solver, OR-Tools CP-SAT, does. Hard blocks (sleep, classes, the user's rules, the past part of today) can never be crossed. The solver puts urgent and valuable work first. Once Jarvis has learned the user's best hours, it leans hard tasks toward them, but a task due within 24 hours always wins. If nothing fits, it widens the window from 3 days up to 30, then says honestly that it is a scope problem. PEARL is what learns those best hours and the hours the user avoids.

## Anticipation: the points to know

| Point | Where | Why |
|---|---|---|
| A 60-second tick, sweeps first, then triggers per user | `app/services/anticipation/scheduler.py` (`AnticipationScheduler`) | Jarvis acts without being asked |
| The fixed order of a trigger | `app/domain/anticipation.py` (`Trigger.run`) | No trigger can skip consent |
| Two triggers built | `app/services/anticipation/triggers.py` (`MorningBriefTrigger`, `PatternRuleTrigger`) | The morning brief; a pattern offered as a rule |
| Consent: four rules in order | `app/domain/anticipation.py` (`ConsentPolicy`) | Dismissed before → deny; read-only → allow; already covered by a stated rule → deny; else the user's autonomy mode decides |
| Accept claims first, then runs | `app/services/anticipation/service.py` (`accept`) | A double click runs the action once; the second gets a 409 |
| A suggestion stores its action as data | `app/services/anticipation/actions.py` | It can be run hours later, after a restart |

## Memory: the points to know

| Point | Where | Why |
|---|---|---|
| Four statuses and the legal moves | `app/domain/entities.py` (`MEMORY_TRANSITIONS`) | A forgotten memory can never come back by accident |
| `remember`: suppress, supersede, reinforce or create | `app/services/memory/memory_service.py` (`MemoryService.remember`) | No duplicates, no stale facts |
| Forget is a tombstone | `MemoryService.forget` | Words erased, fingerprint kept: "forget sticks" |
| Forgetting a memory deletes the rule linked to it | `app/services/memory/stated_rules.py` (`StatedRules`) | The plan stops following a rule the user took back |
| Decay: strength falls over weeks; used memories are kept | `Memory.strength_at`, `initial_stability`, `note_used`, `MemoryDecaySweep` | Old unused facts fade; what the user stated lasts longest (a stated constraint starts at a 16-week stability, an inferred fact at 2) |
| Recall within a budget | `MemoryService.recall`; SQL `match_memories()` | The model sees only what matters: up to 12 memories, about 500 tokens in total |
| Vector search inside Postgres (pgvector) | `supabase/migrations/20261115000002_memory_lifecycle_and_vectors.sql` | One query filters by user and status, so a forgotten memory can never be returned |
| A correction replaces the old rule | `StatedRules.replace_for_superseded` | "Gym moved to 7 AM" leaves one rule, not two |
| Live proof | `scripts/memory_check.py` | Seven steps against the real backend: say, recall, correct, forget |

## CP-SAT and planning: the points to know

| Point | Where | Why |
|---|---|---|
| Planning steps | `app/modules/planning_graph.py` | validate goal → fetch constraints → translate habits → decompose → fuse with pending tasks → solve → create draft |
| The only door to the solver | `app/services/planning/scheduling.py` (`run_schedule`) | One place builds the model, so no copy can drift |
| The solver | `app/core/or_tools/solver.py` (`JarvisScheduler`) | Meets every hard constraint or reports that none fits |
| Hard blocks | sleep, meetings, the user's rules, the past part of today | Never overlapped |
| Soft blocks | "minimal work" windows | Only short, easy tasks (at most 15 minutes, difficulty 0.4) |
| The objective | shorter plan + TMT priority | Urgent, valuable tasks start earlier |
| The energy term | on only when the focus profile is learned (10+ outcomes in 28 days) | Hard tasks lean to peak hours; zero for a task due within 24 hours |
| Time limits | 10 seconds; stops 1 second after the last improvement | The user never waits long |
| Nothing fits | horizon 3 → 5 → 7 → 14 → 30 days | Then: "a scope problem, not a you problem" |
| The user's clock | `app/services/planning/local_time.py` | A plan starts at 08:00 local; from 22:00 it is already tomorrow |

## PEARL: the points to know

| Point | Where | Why |
|---|---|---|
| Four detectors | `app/services/memory/patterns.py` (`SkipTimeWindowDetector`, `PeakHourDetector`, `DeadlineCrunchDetector`, `RepeatedSkipDetector`) | Four ways to read the history |
| The thresholds | skip window: 3+ outcomes in an hour and 70%+ set aside; peak hour: 80%+ done | Enough evidence before saying anything |
| What it reads | `task_events`, last 28 days, one outcome per slot | Status alone would lose the history |
| Confidence capped at 0.9 | `patterns.py` | An inference is never certain |
| When it runs | after every outcome in the background (`PatternObserver`), and inside a turn for at most 0.5 seconds | Learning never slows the user down |
| A pattern becomes a rule only if the user accepts | `PatternRuleTrigger` | Infer, confirm, adapt |
| The focus profile | `app/services/energy.py` (`HistoryEnergyEstimator`) | Feeds the solver's energy term |
| Rule check-ins | `app/services/analytical/rule_check_ins.py` (`RuleCheckIns`) | "Still how you work?" on a spaced schedule: 1 day, 6 days, then longer |

## Your two patterns: Strategy and State

### Strategy

- **The problem.** Several jobs in Jarvis can be done in more than one way, and more ways will come: which model answers, how the history is read for patterns.
- **The pattern in one line.** Several interchangeable ways to do one job, behind one interface.
- **Compare it to.** A maps app: car, bus or walking. You ask for a route the same way each time.
- **Where.** `PatternDetector` in `app/services/memory/patterns.py` with four subclasses; the list is chosen in `default_pattern_detectors` in `app/container.py`. Also `LLMProvider` in `app/domain/ports.py` with the local model, Claude and Gemini, and `EnergyEstimator` with two versions in `app/services/energy.py`.
- **Which SOLID principle.** **Open/closed**: a new detector is a new class and one line in the list; PEARL and the trigger are not edited. Also **Dependency inversion**: PEARL holds the abstract `PatternDetector`, never a concrete one.
- **Why not the simpler way.** An `if/elif` on a name would be edited for every new detector or model. A table of thresholds cannot hold each detector's own wording and its effect on planning.
- **Admit.** Some strategies have only one real implementation today.

### State (as transition tables)

- **The problem.** A draft was accepted twice and every task was duplicated. A forgotten memory could come back.
- **The pattern in one line.** An object may only move between states along allowed paths; any other move is refused.
- **Compare it to.** An online order: you cannot cancel what was delivered.
- **Where.** `DRAFT_TRANSITIONS`, `SUGGESTION_TRANSITIONS` and `MEMORY_TRANSITIONS` in `app/domain/entities.py`. `DraftSchedule.transition()` checks the table and raises `InvalidStateTransition`, which is a 409.
- **Which SOLID principle.** **Single responsibility**: the rule "which move is legal" lives in one table and one method, not in `if` checks spread over endpoints. It is also **encapsulation**: nothing else changes a draft's status.
- **Why a table and not one class per state.** Our states have no behaviour of their own beyond which moves are legal. A table says that in a few lines, and the same table gives the database its guard (update only if the status is one I expect).
- **Admit.** The task moves live in `app/services/task_service.py`, not in the domain with the other three.

## Questions you may get

1. **Why is Jarvis not a planner?** A planner waits for you to enter things and shows them back. Jarvis builds a model of the person from what actually happened and acts first, with consent. Scheduling is one tool it uses.
2. **Why is the Clock an actor?** It starts use cases with no user request: the morning brief and the sweep that marks a passed slot as missed. Without it Jarvis could never make the first move.
3. **Why layers with ports?** So services do not know the database or the model vendor. We can test offline with in-memory repositories, and we added Claude without touching the callers.
4. **Why not microservices?** One person, one process, next to a local model. The atomic accept needs one database transaction; across services it would need a distributed one.
5. **Why CP-SAT and not let the model schedule?** A model makes overlaps and ignores rules. CP-SAT meets every hard constraint or proves none exists. The model only breaks the goal into tasks.
6. **What if the solver finds nothing?** It widens the window from 3 up to 30 days. Past that, Jarvis says it is a scope problem and never shows an error.
7. **How does learning change the plan?** PEARL reads the outcome history. The focus profile becomes a penalty in the solver's objective for placing a hard task in a weak hour. It is a soft preference: urgency and hard blocks always win.
8. **How does forget really forget?** The words are erased. The embedding is kept as a tombstone so the same fact said again is recognised and dropped. Recall filters by status inside the database, so a forgotten memory cannot be returned.
9. **Why keep memories in Postgres and not a vector database?** One query can filter by user and status and order by similarity. A second store would have to be kept in step with every forget.
10. **How do you stop Jarvis from acting on its own?** Every proactive action passes the consent chain inside `Trigger.run`, and that method cannot be overridden. A schedule change always asks.
11. **Which SOLID principle does Strategy use?** Open/closed: new behaviour is a new class. And Dependency inversion: the caller depends on the abstract class.
12. **Where is polymorphism in your part?** PEARL calls `detect` the same way on four detectors. The consent chain calls `evaluate` the same way on four rules.
13. **Is the demo data fake?** The history is seeded; the inference is live. The seed writes three weeks of outcomes through the real services, and the patterns, focus profile and brief are computed live from them.

## Say these honestly

- Only two triggers exist: the morning brief and the pattern rule. The pre-task prep and deadline-risk triggers are not built.
- The scheduler, its locks and the "once a day" memo live in one process. With two server processes, two briefs could be made. The data stays correct because the important guards are in the database.
- A stopped solver search gives a good plan, not a proven best one.
- Memories stored before the last memory fix keep their old, shorter lifespan until they are said again.
- The user's own words stay in the chat history; forgetting a memory does not rewrite old messages.
- There is no real login yet: the user id comes from the request.
