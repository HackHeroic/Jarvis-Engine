# Jarvis — Architecture for VC Pitch

**Pitch date:** Wednesday, April 1, 2026

> Open these diagrams in [Mermaid Live Editor](https://mermaid.live) for full-screen, zoomable SVG/PNG export.

---

## Slide 1: "What It Does" — The Core Loop

**Talking point:** "User brain-dumps what's on their plate. Jarvis turns it into a mathematically valid schedule using OR-Tools (not LLM guessing). The user reviews, edits, negotiates — then accepts. The system learns from every interaction."

```mermaid
flowchart LR
    User((User)) -->|Brain dump| Chat[Jarvis Chat]

    subgraph Intelligence [AI Layer]
        Chat --> Extract[Understand Intent]
        Extract --> Decompose[Break Into Micro-Tasks]
        Decompose --> Memory[Recall User Preferences]
    end

    subgraph Engine [Deterministic Engine]
        Memory --> Solver[OR-Tools Scheduler\nMathematically Optimal]
    end

    subgraph UX [Negotiation UX]
        Solver --> Draft[Proposed Schedule]
        Draft --> Review{User Reviews}
        Review -->|Accept| Calendar[Calendar]
        Review -->|Edit| Solver
        Review -->|Chat| Decompose
    end

    subgraph Learning [Behavioral Learning]
        Calendar --> Observe[Observe Behavior]
        Observe --> Patterns[Detect Patterns]
        Patterns -->|Adapts| Memory
    end
```

---

## Slide 2: "Why It's Different" — The Memory-to-Constraint Bridge

**Talking point:** "ChatGPT's memory affects what it *says*. Jarvis's memory changes the *mathematical constraints* in the scheduler. When we detect you skip morning tasks, we don't just tell you — we stop scheduling deep work before 10 AM. The system literally rewires itself."

```mermaid
flowchart TD
    subgraph Observe [System Observes User Behavior]
        Skip[User skips 5 morning tasks]
        Edit[User always shortens to 15 min]
        Reject[User rejects cramped schedules]
    end

    subgraph Infer [PEARL: Behavioral Inference]
        Skip --> Pattern1[Pattern: Avoids work before 10 AM\nConfidence: 87%]
        Edit --> Pattern2[Pattern: Prefers 15-min blocks\nConfidence: 92%]
        Reject --> Pattern3[Pattern: Needs 30% buffer time\nConfidence: 78%]
    end

    subgraph Bridge [Memory Changes the Math]
        Pattern1 --> Constraint1[Soft block: no deep work before 10 AM]
        Pattern2 --> Constraint2[Default chunk: 15 min instead of 25]
        Pattern3 --> Constraint3[Daily cap: reduced by 30%]
    end

    subgraph Solver [OR-Tools CP-SAT Solver]
        Constraint1 --> Schedule[Mathematically Optimal Schedule\nthat FITS the user]
        Constraint2 --> Schedule
        Constraint3 --> Schedule
    end

    Schedule --> Better[Schedule improves every day\nwithout user saying a word]

    style Bridge fill:#f0f7ff,stroke:#1a73e8
    style Better fill:#e8f5e9,stroke:#2e7d32
```

---

## Slide 3: "Where It Goes" — Platform Vision

**Talking point:** "Phase 1 is live: reliable scheduling with behavioral learning. Phase 2 adds Deep Knowledge Tracing to track what you've mastered, Reinforcement Learning to optimize your learning path, and energy forecasting. Every piece plugs into the same extensible framework — adding a new capability is one function and one registration call."

```mermaid
flowchart TD
    subgraph Phase1 [Phase 1 — NOW]
        direction TB
        P1_Brain[Brain Dump Extraction]
        P1_Schedule[Deterministic Scheduler\nOR-Tools CP-SAT]
        P1_Draft[Draft Review UX\nAccept / Edit / Reject]
        P1_Memory[3-Tier Memory\nSM-2 Decay + PEARL]
        P1_Docs[Smart Document Integration\nPDF to Practice Problems to Tasks]
        P1_Registry[Registry Framework\nExtensible Everything]

        P1_Brain --> P1_Schedule --> P1_Draft
        P1_Memory --> P1_Schedule
        P1_Docs --> P1_Schedule
    end

    subgraph Phase2 [Phase 2 — NEXT]
        direction TB
        P2_DKT[Deep Knowledge Tracing\nLSTM Mastery Tracking]
        P2_RL[Reinforcement Learning\nOptimal Learning Path]
        P2_SARIMAX[Energy Forecasting\nSARIMAX Capacity Model]
        P2_PII[Privacy Gateway\nPII Anonymization]
        P2_Signals[Signals API\nTime + Focus + Mood]

        P2_DKT --> P2_RL --> P1_Schedule
        P2_SARIMAX --> P1_Schedule
        P2_Signals --> P2_DKT
        P2_Signals --> P2_RL
    end

    subgraph Phase3 [Phase 3 — SCALE]
        direction TB
        P3_Mobile[Mobile App\niOS + Android]
        P3_Integrations[Slack + Email + Calendar\nMCP Integrations]
        P3_Team[Team Scheduling\nMulti-User Coordination]
        P3_Local[Full Local-First\nFine-Tuned On-Device Models]
    end

    Phase1 --> Phase2 --> Phase3

    style Phase1 fill:#e8f5e9,stroke:#2e7d32
    style Phase2 fill:#fff3e0,stroke:#f57c00
    style Phase3 fill:#f3e5f5,stroke:#7b1fa2
```

---

## The Moat (One Slide Summary)

| What | Jarvis | ChatGPT / Claude | Motion / Reclaim |
|------|--------|-----------------|-----------------|
| Scheduling | **OR-Tools CP-SAT** (mathematically proven) | LLM generates text (hallucinates) | Basic auto-scheduler |
| Memory | **Changes the scheduler** (constraints from behavior) | Changes responses only | No memory |
| Psychology | **Anti-guilt** (failure = recalibrate, not shame) | None | "Overdue" notifications |
| Documents | **Extracts problems, enriches tasks** | Reads and summarizes | No document support |
| Extensibility | **Registry framework** (add capabilities in 1 function) | N/A | Closed system |
| Privacy | **Local-first path** (cloud now, local Phase 2) | Cloud only | Cloud only |

---

## How to Present These Diagrams

1. **Copy the Mermaid code** for each diagram
2. **Paste into [Mermaid Live Editor](https://mermaid.live)**
3. **Export as SVG or PNG** (top right corner)
4. **Drop into your slide deck** (Google Slides, Keynote, etc.)

Or use **Mermaid CLI** to batch-export:
```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i PITCH_ARCHITECTURE.md -o slides/ -e png
```
