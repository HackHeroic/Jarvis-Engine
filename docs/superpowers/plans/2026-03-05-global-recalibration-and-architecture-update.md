---
name: Global Recalibration and Architecture Update
overview: "Implement Global Recalibration (multi-goal task fusion) and update POLICY_ENGINE_ARCHITECTURE.md to accurately reflect the current system: unified /chat, brain-dump extraction, habit translator, OR-Tools+TMT scheduler, ingestion pipeline, proactive workspace, multi-source deadlines, and (post-implementation) multi-goal fusion."
todos: []
isProject: false
---

# Global Recalibration, Multi-Goal Fusion & Architecture Doc Update

## Part A: Global Recalibration (Unchanged)

The implementation steps from the original plan remain: migration 007, `get_all_pending_tasks`, `_persist_decomposition_to_user_tasks` updates, `compute_horizon_from_deadlines` chunks parameter, fusion flow in `_run_plan_day_flow`, replace persistence. See the full plan for details.

---

## Part B: Update POLICY_ENGINE_ARCHITECTURE.md

**File**: [docs/POLICY_ENGINE_ARCHITECTURE.md](docs/POLICY_ENGINE_ARCHITECTURE.md)

**Rationale**: The architecture doc is out of date. It describes DKT/RL in the scheduling path; the actual system uses OR-Tools CP-SAT with TMT and adaptive pacing. It omits the brain-dump flow, ingestion pipeline, proactive workspace, multi-source deadlines, and (post-implementation) multi-goal fusion.

### B1. Replace Request Flow Diagram

**Current**: Generic API -> Router -> Local LLM; DKT -> RL -> CSP.

**New diagram** (mermaid):

```mermaid
flowchart TD
    User((User)) -->|JSON| ChatAPI[POST /api/v1/chat]
    User -->|File/Text| IngestAPI[POST /api/v1/ingestion/process]
    User -->|task_id| WorkspaceAPI[GET /api/v1/tasks/task_id/workspace]

    subgraph ChatFlow [Chat Flow]
        ChatAPI --> BrainDump[Brain Dump Extraction]
        BrainDump -->|planning_goal habits action_items search_queries| ControlPolicy[Control Policy]
        ControlPolicy -->|PLAN_DAY| PlanDayFlow[Plan-Day Flow]
        ControlPolicy -->|KNOWLEDGE_INGESTION| IngestAPI
        ControlPolicy -->|BEHAVIORAL| HabitsStore[behavioral_constraints]
    end

    subgraph PlanDayFlow [Plan-Day Flow]
        HabitsFetch[Fetch behavioral_constraints]
        HabitTranslate[translate_habits_to_slots]
        HorizonExpand[expand_semantic_slots_to_time_slots]
        Decompose[Socratic Chunker via hybrid_route_query]
        HorizonCalc[compute_horizon_from_deadlines]
        RunSchedule[run_schedule OR-Tools CP-SAT]
        Persist[_persist_decomposition_to_user_tasks]
        HabitsFetch --> HabitTranslate --> HorizonExpand
        Decompose --> HorizonCalc --> RunSchedule --> Persist
    end

    subgraph Deterministic [Deterministic Engine]
        RunSchedule --> TMT[TMT Priority from deadline_hint]
        TMT --> CP[Solver add_task]
        CP --> AdaptivePacing[compute_adaptive_daily_cap]
        AdaptivePacing --> Calendar[Schedule JSON]
    end

    subgraph Memory [Persistence]
        behavioral_constraints[(behavioral_constraints)]
        user_tasks[(user_tasks)]
        user_plan_updates[(user_plan_updates)]
        task_materials[(task_materials)]
        user_preferences[(user_preferences)]
        ChromaDB[(ChromaDB jarvis_knowledge)]
    end

    subgraph WorkspaceFlow [Workspace Flow]
        WorkspaceAPI --> WSB[Workspace Builder]
        WSB --> RAG[fetch_chunks_for_task]
        WSB --> WebSearch[perform_learning_style_search]
        WSB --> PracticeGen[generate_practice_assets]
        RAG --> ChromaDB
        WebSearch --> GeminiL9[Gemini + web_search_options]
    end

    subgraph IngestionFlow [Ingestion Flow]
        IngestAPI --> Orchestrator[process_ingestion]
        Orchestrator --> Docling[Docling extract]
        Orchestrator --> IngestKnowledge[ingest_knowledge]
        IngestKnowledge --> ChromaDB
        Orchestrator --> LinkTasks[link_document_to_tasks]
        LinkTasks --> task_materials
    end
```



### B2. Add "Implemented APIs" Section

List the actual routes:

- `POST /api/v1/chat` – Unified entry; brain-dump extraction, plan-day, ingestion, habits
- `GET /api/v1/tasks/{task_id}/workspace` – Proactive workspace (RAG + web search + practice)
- `POST /api/v1/ingestion/process` – Document ingestion, task-material linking
- `POST /api/v1/schedule/generate-schedule` – Direct OR-Tools schedule from ExecutionGraph
- `GET/POST /api/v1/habits/*` – Habits CRUD
- `POST /api/v1/reasoning/decompose-goal` – Socratic Chunker

### B3. Update Component Definitions Table


| Component             | Definition                                                                                                                         |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| **Control Policy**    | Master orchestrator for /chat: brain-dump extraction, multi-intent execution, plan-day flow, ingestion routing.                    |
| **LiteLLM Router**    | Local-first: Qwen 27B (decompose), Qwen 4B (router/SLM), Gemini (L9 search). Offloads Real-Time Research and last-resort fallback. |
| **Habit Translator**  | Converts natural-language constraints (behavioral_constraints) to SemanticTimeSlots.                                               |
| **Horizon Expander**  | Replicates semantic slots across multi-day horizon into concrete TimeSlots.                                                        |
| **Socratic Chunker**  | Decomposes goals into TaskChunks (25-min ceiling) via LLM.                                                                         |
| **OR-Tools CP-SAT**   | Deterministic scheduler; TMT priorities from deadline_hint; AddNoOverlap, dependencies.                                            |
| **Adaptive Pacing**   | compute_adaptive_daily_cap: slack-ratio-driven daily cap; prevents cramming.                                                       |
| **ChromaDB**          | Vector store for ingested knowledge; task_materials links to user_tasks.                                                           |
| **Workspace Builder** | Fetches RAG chunks, learning-style web search, dynamic practice assets for task focus mode.                                        |


Remove or mark as "Future" the DKT and RL rows if they are not implemented.

### B4. Add "Data Model" Section

Brief table of persistent stores:

- `behavioral_constraints` – Habits, preferences (raw_text, constraint_type)
- `user_tasks` – Task decomposition (task_id, goal_id, title, …)
- `user_plan_updates` – Goal-level deadlines (goal_id, deadline_date)
- `user_preferences` – learning_style for workspace
- `task_materials` – Links documents to tasks (source_id -> ChromaDB)
- `pending_calendar_updates` – Extracted timetables awaiting approval

### B5. Add "Global Recalibration (Planned)" Section

When multi-goal fusion is implemented:

- `get_all_pending_tasks` fetches pending chunks from user_tasks
- Fusion merges new decomposition with pending; namespaces by goal_id
- `compute_horizon_from_deadlines` runs on fused chunks
- Single schedule across all goals; TMT prioritizes by deadline

Include a small mermaid subgraph in the Plan-Day Flow showing: Decompose -> get_all_pending_tasks -> Fusion -> RunSchedule.

### B6. Keep Routing Behavior Section

The "Local-First" and "Cloud Gemini L9" behavior is accurate; only adjust model names (Qwen 27B, Qwen 4B) if desired.

---

## File Change Summary (Architecture Update)


| File                                                                     | Change                                                                                                      |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| [docs/POLICY_ENGINE_ARCHITECTURE.md](docs/POLICY_ENGINE_ARCHITECTURE.md) | Replace flow diagram; add APIs, component definitions, data model; add Global Recalibration planned section |


---

## Implementation Order

**Phase 1 – Global Recalibration** (existing plan):

1. Migration 007
2. `get_all_pending_tasks`
3. Update `_persist_decomposition_to_user_tasks`
4. Extend `compute_horizon_from_deadlines`
5. Fusion logic in `_run_plan_day_flow`
6. (Optional) Task completion endpoint

**Phase 2 – Architecture Doc** (can run in parallel or after Phase 1):

1. Update [docs/POLICY_ENGINE_ARCHITECTURE.md](docs/POLICY_ENGINE_ARCHITECTURE.md) with B1–B6

