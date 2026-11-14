# Jarvis Engine

Jarvis is a hyper-personalized personal intelligence. It builds a model of one person (when they focus, what they avoid, how they respond when a plan slips) and uses it to have things ready before they are asked. It is not a calendar or a planner: the scheduler is a muscle, the user model and the observation loop are the brain.

This repository is the backend. The frontend is [jarvis-frontend](https://github.com/HackHeroic/jarvis-frontend).

## Where is what

| You want | Open |
|---|---|
| The capstone design documents (start here for the evaluation) | [`docs/capstone/`](docs/capstone/) |
| What is built and what is not, right now | [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) |
| The demo script, viva questions and who owns what | [`docs/capstone/10-demo-and-contributions.md`](docs/capstone/10-demo-and-contributions.md) |
| The design spec and its three plans | [`docs/superpowers/specs/`](docs/superpowers/specs/), [`docs/superpowers/plans/`](docs/superpowers/plans/) |
| Rules for working in this code | [`.claude/CLAUDE.md`](.claude/CLAUDE.md), [`.claude/rules/`](.claude/rules/) |
| Older documents | [`docs/INDEX.md`](docs/INDEX.md) |

### The ten capstone documents

| # | Document | Evaluation criterion |
|---|---|---|
| 01 | [Requirements](docs/capstone/01-requirements.md) | Functional and non-functional requirements |
| 02 | [Architecture](docs/capstone/02-architecture.md) | Layers and how a request moves through them |
| 03 | [Class diagrams](docs/capstone/03-class-diagrams.md) | Class diagram |
| 04 | [OOP and SOLID](docs/capstone/04-oop-and-solid.md) | OOP principles, SOLID |
| 05 | [Design patterns](docs/capstone/05-design-patterns.md) | Design patterns, each with its code location |
| 06 | [Sequence and activity diagrams](docs/capstone/06-sequence-and-activity.md) | Sequence and activity diagrams |
| 07 | [API design](docs/capstone/07-api-design.md) | API design |
| 08 | [Database schema](docs/capstone/08-database-schema.md) | Database schema |
| 09 | [Error handling](docs/capstone/09-error-handling.md) | Error handling, edge cases, concurrency |
| 10 | [Demo and contributions](docs/capstone/10-demo-and-contributions.md) | Working implementation, each member's part |

`tests/test_design_conformance.py` checks that the code matches what these documents claim.

### The code

```
app/api/v1/endpoints/   routers only: parse, call a service, return a typed model
app/services/           application services
  chat_turn/              one chat turn, start to finish
  planning/               scheduling, rule windows, the user's clock
  memory/                 long-term memory: remember, recall, forget, decay
  anticipation/           the scheduler, triggers, suggestions, consent
app/orchestrator/       the LangGraph turn graph, state, checkpointer
app/modules/            module sub-graphs (planning, research, knowledge, conversation, coach)
app/domain/             entities, ports (ABCs), events, errors; imports no outer layer
app/infrastructure/     Supabase and in-memory repositories, model providers, embedder
app/container.py        the composition root: the one place that wires everything
app/core/or_tools/      the CP-SAT solver and its constraints
supabase/migrations/    the database schema (source of truth)
tests/                  the offline suite; tests/behaviour/ drives whole chat turns
scripts/                seed_demo_user.py, live_e2e.py, smoke_claude.py
```

## Running it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
# create .env with the keys listed under "Environment" in .claude/CLAUDE.md (ask the repo owner for the values)
.venv/bin/uvicorn app.main:app --port 8000
```

A local model in LM Studio on port 1234 is optional. Without it every model call goes to Claude.

Then start the frontend (`npm install && npm run dev` in jarvis-frontend) and open:

- `http://localhost:3000/dashboard`: the morning brief and today
- `http://localhost:3000/chat`: talk to Jarvis
- `http://localhost:3000/you`: what Jarvis has learned about you, and the forget button
- `http://localhost:3000/schedule`: the plan

## Tests

```bash
.venv/bin/python -m pytest tests/ -q        # offline: never touches the network
.venv/bin/python scripts/live_e2e.py        # 18 live checks against real Supabase and Claude (backend must be running)
```

## Demo data

With the backend stopped:

```bash
.venv/bin/python scripts/seed_demo_user.py --reset
```

This seeds about three weeks of history for the user `demo`. Start the backend again afterwards.

## Not built

Ask-back questions, pre-task prep, deadline-risk alerts, the research summariser and real sign-in are not built. `docs/PROJECT_STATUS.md` has the full list.
