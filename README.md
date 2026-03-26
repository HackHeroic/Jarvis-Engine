# Jarvis Engine

An AI-powered proactive preparation engine that transforms brain dumps into optimized, psychologically-informed schedules — so students and professionals can focus on execution instead of planning.

## What It Does

You brain-dump everything — goals, deadlines, habits, preferences — and Jarvis handles the rest:

1. **Extracts** structured intents from natural language (goals, habits, deadlines, calendar events, action items)
2. **Decomposes** goals into micro-tasks using a Socratic chunking pipeline
3. **Schedules** optimally using OR-Tools CP-SAT constraint solver with adaptive daily caps
4. **Links** relevant study materials from your uploaded documents via RAG
5. **Recalibrates** automatically when plans break — no guilt, just data

## Tech Stack

| Layer | Technology |
|-------|-----------|
| AI Routing | LiteLLM Hybrid Router (local Qwen-27B/4B + Gemini cloud fallback) |
| Scheduling | Google OR-Tools CP-SAT Solver |
| Document Intelligence | IBM Docling + ChromaDB + MLX-Embed |
| Task Decomposition | Socratic Chunker (multi-phase goal breakdown) |
| Habit Tracking | SM-2 Spaced Repetition |
| Psychology | WOOP, CLT, TMT, Anti-guilt architecture |
| Backend | FastAPI + Supabase (PostgreSQL) |
| Frontend | Next.js 14 (see [jarvis-demo](../jarvis-demo)) |

## Project Structure

```
Jarvis-Engine/
├── app/
│   ├── api/v1/endpoints/     # FastAPI route handlers
│   ├── core/
│   │   └── or_tools/         # CP-SAT constraint solver
│   ├── db/
│   │   ├── migrations/       # Supabase SQL migrations
│   │   └── supabase_py.py    # Database client
│   ├── schemas/              # Pydantic models
│   ├── services/
│   │   ├── analytical/       # Voice of Jarvis, workspace, schedule modifier
│   │   ├── extraction/       # Brain dump parsing, action items, knowledge ingestion
│   │   └── scheduling/       # Socratic chunker, habit translation, fusion
│   └── utils/                # LLM routing, deadline parsing, ChromaDB, pacing
├── docs/
│   ├── POLICY_ENGINE_ARCHITECTURE.md
│   ├── PROJECT_STATUS.md
│   ├── NST-Startup-Foundry-2026-Application.md
│   └── superpowers/plans/    # 25 implementation plans
├── tests/                    # pytest + pytest-asyncio
├── main.py                   # FastAPI app entry point
└── pyproject.toml            # Dependencies
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/chat` | Main brain-dump endpoint — extracts intents, schedules, responds |
| GET | `/api/v1/schedule/today` | Today's optimized schedule |
| POST | `/api/v1/schedule/replan` | Trigger manual recalibration |
| GET | `/api/v1/workspace/{task_id}` | RAG-powered study materials for a task |
| POST | `/api/v1/documents/ingest` | Upload and process documents (PDF, DOCX) |
| GET | `/api/v1/tasks` | List tasks with filters |

## Quick Start

```bash
# Clone and setup
git clone git@github.com:HackHeroic/Jarvis-Engine.git
cd Jarvis-Engine
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure environment
cp .env.example .env  # Add your Supabase, Gemini, and other API keys

# Run
uvicorn main:app --reload --port 8000
```

## Key Design Decisions

- **Local-first AI**: Core intelligence runs on Apple Silicon via Qwen models. Cloud (Gemini) is a fallback, not a dependency.
- **Anti-guilt architecture**: Missed tasks trigger recalibration, not shame. No red "overdue" badges.
- **Psychological science**: Every schedule embeds WOOP implementation intentions, Temporal Motivation Theory urgency weighting, and spaced repetition.
- **Constraint programming over heuristics**: OR-Tools CP-SAT guarantees mathematically optimal schedules within bounds, with automatic horizon expansion when infeasible.

## Documentation

- [Architecture Deep Dive](docs/POLICY_ENGINE_ARCHITECTURE.md) — 9-layer stack, request flow, Mermaid diagrams
- [Project Status](docs/PROJECT_STATUS.md) — Implementation matrix, roadmap, decision log
- [Implementation Plans](docs/superpowers/plans/) — 25 dated design documents

## License

Proprietary. All rights reserved.
