# Jarvis — Architecture Diagrams

**Last updated:** 2026-03-28
**Full spec:** [Architecture Reset Design Spec](superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md)

> Open any diagram in [Mermaid Live Editor](https://mermaid.live) for full-screen zoom, pan, and SVG/PNG export.

---

## 1. The Core Loop — What Jarvis Does

**One sentence:** User brain-dumps chaos, Jarvis returns a mathematically valid schedule, user negotiates until satisfied, system learns.

```mermaid
stateDiagram-v2
    [*] --> BrainDump: User types what's on their mind

    state "AI Layer" as AI {
        BrainDump --> IntentClassified: LLM extracts structure
        IntentClassified --> Decomposed: Socratic Chunker breaks into micro-tasks
        Decomposed --> MemoryInjected: Retrieve user memories + constraints
    }

    state "Deterministic Engine" as Engine {
        MemoryInjected --> Scheduled: OR-Tools CP-SAT solves optimal schedule
    }

    state "Negotiation UX" as UX {
        Scheduled --> DraftProposed: Schedule proposed as DRAFT
        DraftProposed --> UserReviews: User sees proposed schedule

        UserReviews --> Accepted: Accept All
        UserReviews --> Edited: Edit individual tasks
        UserReviews --> Rejected: Reject + explain why
        UserReviews --> ChatMore: Chat to modify

        Edited --> Scheduled: Re-solve with edits
        ChatMore --> Decomposed: Re-decompose if needed
        Rejected --> MemoryStored: Store rejection reason as memory
        MemoryStored --> Decomposed: Try different approach
    }

    state "Learning Loop" as Learn {
        Accepted --> TasksActive: Tasks persisted to calendar
        TasksActive --> BehaviorObserved: System observes: complete / skip / edit
        BehaviorObserved --> PatternsDetected: PEARL detects behavioral patterns
        PatternsDetected --> ConstraintsUpdated: Patterns become scheduling constraints
    }

    ConstraintsUpdated --> MemoryInjected: Next schedule is smarter

    note right of Engine
        OR-Tools CP-SAT = Integer Programming
        Not LLM guessing. Mathematically proven.
        No overlaps. Respects all constraints.
    end note

    note right of Learn
        System improves every day
        without user saying a word.
    end note
```

---

## 2. Memory Lifecycle — How Jarvis Remembers

**One sentence:** Memories are extracted from conversations, scored by relevance, reinforced by behavior, and decay naturally if not confirmed — like human memory.

```mermaid
stateDiagram-v2
    [*] --> NewMemory: Extracted from conversation or behavior

    NewMemory --> Active: Stored with confidence=0.5, stability=1.0

    Active --> Reinforced: User repeats preference OR behavior confirmed
    Reinforced --> Active: stability++, confidence += 0.1, strength = 1.0

    Active --> Decaying: Time passes without reinforcement
    Decaying --> Active: Reinforced again (user confirms)
    Decaying --> Archived: strength drops below 0.1

    Active --> Superseded: Contradiction detected
    Superseded --> [*]: Old memory marked superseded_by = new.id

    Archived --> [*]: Pruned from active queries but kept for history

    note right of Active
        Score = Relevance x Recency x Importance x Confidence
        Recency = strength x e^(-t / (stability x 7days))
        Higher stability = slower decay
        Stability capped at 20 (max half-life ~140 days)
    end note

    note right of Superseded
        Old memory is NOT deleted.
        Preserved for pattern analysis:
        "User changed from morning to night person in March"
    end note
```

---

## 3. Memory-to-Constraint Bridge — The Moat

**One sentence:** ChatGPT's memory affects what it says. Jarvis's memory changes the mathematical constraints in the scheduler. The system rewires itself.

```mermaid
stateDiagram-v2
    state "Observation" as Obs {
        [*] --> TaskSkipped: User skips morning tasks (5x)
        [*] --> TaskEdited: User shortens to 15 min (8x)
        [*] --> DraftRejected: User rejects cramped schedules (3x)
    }

    state "PEARL Inference" as Pearl {
        TaskSkipped --> MorningPattern: Pattern detected at 87% confidence
        TaskEdited --> DurationPattern: Pattern detected at 92% confidence
        DraftRejected --> BufferPattern: Pattern detected at 78% confidence

        MorningPattern --> SoftBlock: Becomes soft block before 10 AM
        DurationPattern --> ChunkDefault: Default chunk becomes 15 min
        BufferPattern --> CapReduction: Daily cap reduced by 30%
    }

    state "OR-Tools Solver" as Solver {
        SoftBlock --> OptimalSchedule: Schedule avoids mornings
        ChunkDefault --> OptimalSchedule: Tasks are 15 min blocks
        CapReduction --> OptimalSchedule: More buffer time built in
    }

    OptimalSchedule --> [*]: Schedule improves daily without user input

    note right of Pearl
        No ML needed for Phase 1.
        Count patterns, apply rules.
        ML (DKT/RL) adds sophistication in Phase 2.
    end note

    note right of Solver
        Memories don't just inform responses.
        They change the MATH.
        This is the breakthrough.
    end note
```

---

## 4. Three-Tier Memory Architecture

**One sentence:** Inspired by MemGPT's OS-like memory management — working memory for now, recall for past sessions, archival for permanent knowledge.

```mermaid
flowchart TD
    subgraph Working [Working Memory — Per Request]
        WM1[Current session messages]
        WM2[Top-K scored memories from archival]
        WM3[Active constraints — always included]
        WM4[Active goals — always included]
        WM5[Recent behavioral patterns — confidence > 0.6]
    end

    subgraph Recall [Recall Memory — Past Sessions]
        RM1[Session summaries — LLM generated]
        RM2[Goals discussed per session]
        RM3[Mood signals per session]
        RM4[Searchable by similarity + date range]
    end

    subgraph Archival [Archival Memory — Structured Knowledge]
        AM_Facts[Facts — CS student at VIT]
        AM_Prefs[Preferences — hates mornings]
        AM_Patterns[Behavioral Patterns — PEARL inferred]
        AM_Events[Temporal Events — finals June 15]
        AM_Goals[Goals — finish DSA by April]
        AM_Constraints[Constraints — class MWF 2-3 PM]
    end

    subgraph Scoring [SM-2 Decay Scoring]
        Score["Score = Relevance x Recency x Importance x Confidence"]
        Decay["Strength = Initial x e^(-t / (stability x halflife))"]
    end

    Archival -->|Score + Rank| Scoring
    Scoring -->|Top-K| Working
    Recall -->|Relevant summaries| Working
    Working -->|Injected into LLM prompt| LLM[LLM Context Window]

    NewTurn[New Conversation Turn] -->|Raw messages| Recall
    NewTurn -->|Extracted facts| Archival
    UserActions[User Actions — skip/edit/accept] -->|Pattern detection| Archival
```

---

## 5. Draft Negotiation Flow — The UX Differentiator

**One sentence:** No competitor lets you negotiate your schedule. Jarvis proposes, you review/edit/counter, Jarvis re-solves. True collaboration.

```mermaid
stateDiagram-v2
    [*] --> UserRequest: "Plan my day - I have DSA exam and essay due"

    UserRequest --> Decompose: Socratic Chunker creates 5+ micro-tasks
    Decompose --> Solve: OR-Tools schedules optimally
    Solve --> DraftCreated: Draft created (NOT persisted yet)

    state "User Reviews Draft" as Review {
        DraftCreated --> Reviewing

        Reviewing --> AcceptAll: Looks good!
        Reviewing --> EditTask: Change duration to 15 min
        Reviewing --> RejectDraft: This doesn't work
        Reviewing --> ChatModify: Move DSA to afternoon
        Reviewing --> Rearrange: Swap task order
    }

    AcceptAll --> Persisted: Tasks saved to calendar
    EditTask --> ReSolve: Modify + re-solve schedule
    ReSolve --> DraftCreated: Updated draft proposed
    ChatModify --> Decompose: Re-decompose if needed
    Rearrange --> ReSolve
    RejectDraft --> StoreReason: Why? (builds memory)
    StoreReason --> Decompose: Try different approach

    Persisted --> Replan: Background: replan remaining tasks
    Persisted --> PearlObserve: PEARL: observe what was accepted/edited
    Persisted --> [*]: Schedule active

    note right of Review
        This is the DIFFERENTIATOR.
        Motion/Reclaim: take it or leave it.
        Jarvis: true negotiation.
    end note
```

---

## 6. Document Intelligence Pipeline

**One sentence:** Upload a PDF and Jarvis classifies it, extracts problems, matches them to your tasks, and enriches your completion criteria. Not just storage — understanding.

```mermaid
stateDiagram-v2
    [*] --> DocumentArrives: Upload PDF / Slack / Email / API

    state "Classification" as Classify {
        DocumentArrives --> Extracted: Docling extracts structured text
        Extracted --> Classified: Registry-based classifier

        Classified --> PracticeProblems: practice_problems
        Classified --> LectureNotes: lecture_notes
        Classified --> Syllabus: syllabus
        Classified --> Assignment: assignment
        Classified --> Reference: reference
        Classified --> FutureType: ... extensible via registry
    }

    state "Practice Problem Flow" as ProbFlow {
        PracticeProblems --> ProblemsExtracted: Extract individual problems
        ProblemsExtracted --> Matched: Match each problem to existing tasks
        Matched --> CriteriaEnriched: Add as completion criteria
        CriteriaEnriched --> PracticeCreated: Create workspace practice assets
    }

    state "Syllabus Flow" as SylFlow {
        Syllabus --> TopicsExtracted: Extract topics + deadlines
        TopicsExtracted --> TaskCheck: Tasks exist for this topic?
        TaskCheck --> UpdateTasks: Yes — update deadlines + subtopics
        TaskCheck --> ProposeTasks: No — propose new task decomposition
    }

    state "Assignment Flow" as AssignFlow {
        Assignment --> RequirementsExtracted: Extract requirements + deadline
        RequirementsExtracted --> AddCriteria: Add as completion criteria
    }

    PracticeCreated --> Replan: trigger_replan
    UpdateTasks --> Replan
    ProposeTasks --> Replan
    AddCriteria --> Replan

    LectureNotes --> LinkedOnly: Link as study material
    Reference --> LinkedOnly
    LinkedOnly --> [*]: Surface in workspace when task active

    Replan --> [*]: Schedule updated with new tasks/criteria

    note right of Classify
        Registry-based: add new document types
        with one handler + one registration.
        No pipeline changes needed.
    end note
```

---

## 7. The Day-by-Day Scenario

**One sentence:** Shows how goal creation, habits, and document uploads integrate seamlessly over time.

```mermaid
stateDiagram-v2
    state "Day 1: Goal Created" as Day1 {
        [*] --> BrainDump: "DL contest on Friday"
        BrainDump --> Tasks5: Decomposed into 5 micro-tasks
        Tasks5 --> Scheduled: OR-Tools schedules across 4 days
    }

    state "Day 1: Habits Added" as Day1b {
        Scheduled --> HabitsAdded: "I study best after lunch + 30 min breaks"
        HabitsAdded --> Recalibrated: Tasks move to 1-5 PM with breaks
    }

    state "Day 2: PDF Uploaded" as Day2 {
        Recalibrated --> PDFUploaded: Upload DL_Practice_Problems.pdf
        PDFUploaded --> ProblemsExtracted2: 15 problems extracted
        ProblemsExtracted2 --> ProblemsMatched: Matched to existing tasks
        ProblemsMatched --> CriteriaUpdated: 10 problems become completion criteria
        ProblemsMatched --> NewTasks: 5 unmatched become new practice tasks
    }

    state "Day 2: User Works" as Day2b {
        CriteriaUpdated --> WorkspaceOpen: User starts "Study CNNs" task
        WorkspaceOpen --> SeesProblems: Workspace shows linked problems
        SeesProblems --> SolvesProblems: User solves 3 CNN problems
        SolvesProblems --> ProgressTracked: 3/5 criteria complete
    }

    state "Day 3: System Learns" as Day3 {
        ProgressTracked --> PearlDetects: PEARL: CNN problems solved in 8 min avg
        PearlDetects --> DifficultyAdjusted: CNN difficulty_weight lowered
        DifficultyAdjusted --> MoreBackprop: More time allocated to backprop (weak area)
    }

    MoreBackprop --> [*]: System optimizes without user intervention

    note right of Day3
        By Day 3, Jarvis knows:
        - User is strong at CNNs
        - User is weak at backprop
        - User works best 1-5 PM
        Without being told any of this.
    end note
```

---

## 8. Registry Framework — Extensible Everything

**One sentence:** Every subsystem uses the same pattern: define a handler, register it, done. The LLM classifier auto-discovers new types. No retraining, no pipeline changes.

```mermaid
flowchart TD
    subgraph Framework [BaseRegistry — Shared Framework]
        Register[register entry]
        Classify[classification_prompt auto-generated]
        Lookup[get_or_fallback handler]
    end

    subgraph IntentReg [Intent Registry]
        I1[PLAN_DAY]
        I2[EDIT_TASK]
        I3[REARRANGE]
        I4[ADD_CONSTRAINT]
        I5[ACCEPT_DRAFT]
        I6[REJECT_DRAFT]
        I7[INGEST_DOCUMENT]
        I8[CHECK_PROGRESS]
        I9[CHAT]
        I10[+ add new intent tomorrow]
    end

    subgraph DocReg [Document Type Registry]
        D1[practice_problems]
        D2[lecture_notes]
        D3[syllabus]
        D4[assignment]
        D5[reference]
        D6[+ meeting_transcript]
        D7[+ email_thread]
    end

    subgraph PatternReg [PEARL Pattern Registry]
        P1[skip_time_window]
        P2[duration_preference]
        P3[deadline_buffer]
        P4[+ post_lunch_reschedule]
    end

    Framework --> IntentReg
    Framework --> DocReg
    Framework --> PatternReg

    style I10 fill:#fff3e0,stroke:#f57c00
    style D6 fill:#fff3e0,stroke:#f57c00
    style D7 fill:#fff3e0,stroke:#f57c00
    style P4 fill:#fff3e0,stroke:#f57c00
```

---

## 9. PEARL Behavioral Pattern Detection

**One sentence:** The system observes user actions, detects patterns above a confidence threshold, and automatically applies them as scheduling constraints.

```mermaid
stateDiagram-v2
    state "User Actions (Signals)" as Signals {
        [*] --> Complete: Task completed
        [*] --> Skip: Task skipped
        [*] --> Edit: Task edited
        [*] --> Reject: Draft rejected
        [*] --> Accept: Draft accepted
    }

    state "Pattern Detection" as Detection {
        Complete --> Aggregate: Aggregate by category
        Skip --> Aggregate
        Edit --> Aggregate
        Reject --> Aggregate
        Accept --> Aggregate

        Aggregate --> TimeCheck: Time window patterns
        Aggregate --> DurationCheck: Duration patterns
        Aggregate --> DeadlineCheck: Deadline patterns
    }

    state "Threshold Gate" as Gate {
        TimeCheck --> ThresholdMet: 3+ observations AND rate > 70%?
        DurationCheck --> ThresholdMet
        DeadlineCheck --> ThresholdMet

        ThresholdMet --> CreatePattern: Yes — new pattern
        ThresholdMet --> ReinforcePattern: Yes — existing pattern
        ThresholdMet --> Discard: No — insufficient evidence
    }

    state "Apply to Scheduler" as Apply {
        CreatePattern --> SoftBlock: Add soft block in OR-Tools
        ReinforcePattern --> SoftBlock
        SoftBlock --> BetterSchedule: Next schedule respects pattern
    }

    BetterSchedule --> [*]: User gets better schedule automatically

    note right of Gate
        No ML needed.
        Count patterns, apply rules.
        DKT/RL adds sophistication in Phase 2.
    end note
```

---

## 10. Platform Roadmap — Phase 1 / 2 / 3

**One sentence:** Phase 1 delivers a working product with behavioral learning. Phase 2 adds ML-powered intelligence. Phase 3 scales to mobile and teams.

```mermaid
flowchart TD
    subgraph Phase1 [Phase 1 — NOW — Make It Work]
        direction TB
        P1_Brain[Brain Dump Extraction]
        P1_Schedule[Deterministic Scheduler — OR-Tools CP-SAT]
        P1_Draft[Draft Review UX — Accept / Edit / Reject]
        P1_Memory[3-Tier Memory — SM-2 Decay + PEARL]
        P1_Docs[Smart Document Integration]
        P1_Registry[Registry Framework — Extensible Everything]

        P1_Brain --> P1_Schedule --> P1_Draft
        P1_Memory --> P1_Schedule
        P1_Docs --> P1_Schedule
    end

    subgraph Phase2 [Phase 2 — NEXT — Make It Smart]
        direction TB
        P2_DKT[Deep Knowledge Tracing — LSTM Mastery]
        P2_RL[Reinforcement Learning — Optimal Path]
        P2_SARIMAX[Energy Forecasting — SARIMAX]
        P2_PII[Privacy Gateway — PII Anonymization]
        P2_Signals[Signals API — Time + Focus + Mood]

        P2_DKT --> P2_RL --> P1_Schedule
        P2_SARIMAX --> P1_Schedule
        P2_Signals --> P2_DKT
        P2_Signals --> P2_RL
    end

    subgraph Phase3 [Phase 3 — SCALE — Make It Big]
        direction TB
        P3_Mobile[Mobile App — iOS + Android]
        P3_Integrations[Slack + Email + Calendar — MCP]
        P3_Team[Team Scheduling — Multi-User]
        P3_Local[Full Local-First — Fine-Tuned Models]
    end

    Phase1 --> Phase2 --> Phase3

    style Phase1 fill:#e8f5e9,stroke:#2e7d32
    style Phase2 fill:#fff3e0,stroke:#f57c00
    style Phase3 fill:#f3e5f5,stroke:#7b1fa2
```

---

## The Moat — Competitive Comparison

| Dimension | Jarvis | ChatGPT / Claude | Motion / Reclaim |
|-----------|--------|-----------------|-----------------|
| **Scheduling** | OR-Tools CP-SAT (mathematically proven, no overlaps) | LLM generates text (hallucinates times) | Basic auto-scheduler |
| **Memory** | Changes the scheduler constraints (behavioral inference) | Changes responses only | No memory |
| **Psychology** | Anti-guilt (failure = recalibrate, not shame) | None | "Overdue" notifications |
| **Documents** | Extracts problems, enriches task criteria, surfaces in workspace | Reads and summarizes | No document support |
| **Negotiation** | Draft review: accept / edit / reject / chat to modify | Take it or leave it | Take it or leave it |
| **Extensibility** | Registry framework (new capability = 1 function + register) | N/A | Closed system |
| **Privacy path** | Local-first roadmap (cloud now, on-device Phase 2) | Cloud only | Cloud only |
| **Learning** | SM-2 memory decay + PEARL behavioral patterns | Static memory | No learning |

---

## How to Use These Diagrams

### For Slide Decks
1. Copy the Mermaid code block for the diagram you want
2. Paste into [Mermaid Live Editor](https://mermaid.live)
3. Export as SVG or PNG (top-right corner)
4. Drop into Google Slides / Keynote / PowerPoint

### For Batch Export
```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i docs/PITCH_ARCHITECTURE.md -o slides/ -e png
```

### For Animated Presentation
Consider using these tools to animate the flow:
- **Excalidraw** — hand-drawn style, great for whiteboard presentations
- **D2** (d2lang.com) — animated diagram transitions
- **Reveal.js** — slide framework with Mermaid plugin for step-by-step reveals
- **Mermaid in Notion/Obsidian** — renders live, good for interactive demos

### Recommended Pitch Flow (3 slides minimum)
1. **Slide 1:** Diagram 1 (Core Loop) — "What it does"
2. **Slide 2:** Diagram 3 (Memory-to-Constraint Bridge) — "Why it's different"
3. **Slide 3:** Diagram 10 (Platform Roadmap) — "Where it goes"

### If you have 7 slides:
1. Core Loop (Diagram 1)
2. Memory Lifecycle (Diagram 2)
3. Memory-to-Constraint Bridge (Diagram 3)
4. Draft Negotiation (Diagram 5)
5. Document Intelligence (Diagram 6)
6. Day-by-Day Scenario (Diagram 7)
7. Platform Roadmap (Diagram 10)
