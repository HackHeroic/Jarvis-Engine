---
name: ""
overview: ""
todos: []
isProject: false
---

# Plan: Multi-Level LLM Response Display in Jarvis Demo

## Context

The Jarvis Engine returns a rich `ChatResponse` with multiple layers — intent, thinking process, task decomposition, schedule, and ingestion results. Currently `ChatPanel.tsx` only renders `msg.content` (plain text) and collapses `thinking_process` in a small toggle. The user wants every pipeline layer surfaced progressively, with thinking shown prominently.

## Goal

Reveal each layer of the Jarvis response in the chat UI:

1. **Intent badge** — what pipeline was triggered
2. **Thinking process** — LLM's `<think>` chain (prominent, scrollable)
3. **Main message** — Jarvis's response text
4. **Execution graph** — task breakdown cards (title, duration, difficulty, completion criteria, implementation intention)
5. **Schedule summary** — compact list of scheduled times
6. **Ingestion result** — knowledge chunks stored

Sections animate in sequentially (staggered reveal) for the last message, simulating pipeline stages completing one by one.

## Files to Create

- `jarvis-demo/components/ResponseLayers.tsx` — new multi-section response renderer

## Files to Modify

- `jarvis-demo/components/ChatPanel.tsx` — swap inline render → `<ResponseLayers>`
- `jarvis-demo/lib/demoData.ts` — enrich `thinking_process` strings to reference each pipeline stage (Brain Dump → Intent → Decompose → Schedule → Synthesize)

---

## Implementation

### 1. `ResponseLayers.tsx`

**Props:**

```ts
interface Props {
  content: string;
  response?: ChatResponse;
  animate?: boolean; // true only for the latest message → staggered reveal
}
```

**Sections (in render order):**


| #   | Section          | Condition                   | Visual                                                                                                                         |
| --- | ---------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Intent badge     | always                      | Colored pill: `PLAN_DAY` → emerald, `KNOWLEDGE_INGESTION` → blue, `BEHAVIORAL_CONSTRAINT` → amber, other → slate               |
| 2   | Thinking process | `response.thinking_process` | Collapsible (starts expanded), muted text, monospace-style, scrollable max-h-48, label "⚙ Internal reasoning"                  |
| 3   | Main message     | always                      | Normal text, `whitespace-pre-wrap`                                                                                             |
| 4   | Execution graph  | `response.execution_graph`  | Collapsible (starts expanded), task cards with difficulty bar, completion_criteria in green, implementation_intention in amber |
| 5   | Schedule summary | `response.schedule`         | Collapsible (starts collapsed), compact table: task title + time range, link "→ View full schedule"                            |
| 6   | Ingestion result | `response.ingestion_result` | Collapsible (starts collapsed), show `stored_chunk_count` and `suggested_actions`                                              |


**Staggered animation logic:**

```ts
const SECTION_DELAYS = [0, 200, 500, 900, 1200, 1500]; // ms
const [visibleCount, setVisibleCount] = useState(animate ? 0 : 99);
useEffect(() => {
  if (!animate) return;
  SECTION_DELAYS.forEach((delay, i) =>
    setTimeout(() => setVisibleCount((n) => Math.max(n, i + 1)), delay)
  );
}, [animate]);
```

Each section checks `visibleCount >= sectionIndex` before rendering — creates a cascading reveal.

**Difficulty bar** (for execution graph tasks):

- A small colored bar (width = `difficulty_weight * 100%`) under the task title
- Color: green (≤0.4) → amber (0.4–0.7) → red (>0.7)

**Time formatting helper:**

```ts
function fmtTime(min: number, horizonStart: string): string {
  const base = new Date(horizonStart);
  base.setMinutes(base.getMinutes() + min);
  return base.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
```

### 2. `ChatPanel.tsx` changes

- Import `ResponseLayers`
- Replace:

```tsx
  <p className="whitespace-pre-wrap">{msg.content}</p>
  {msg.response?.thinking_process && <ThinkingProcess text={msg.response.thinking_process} />}
  

```

  With:

```tsx
  <ResponseLayers
    content={msg.content}
    response={msg.response}
    animate={i === messages.length - 1 && !loading}
  />
  

```

  Note: `animate` only on the last message and only after loading finishes.

### 3. `demoData.ts` changes

Enrich `DEMO_THINKING_PROCESS` to explicitly label pipeline stages:

```
[Brain Dump Extraction] Detected intent: PLAN_DAY. Goal: "study for math mid-sem".
No file attached. No calendar text.

[Habit Translation] Fetching behavioral constraints...
Found constraint: "no work before 11 AM" → minimal_work block 00:00–11:00.

[Socratic Chunker] Decomposing goal using Cognitive Load Theory...
Breaking into 5 × 25-min chunks. WOOP applied: obstacle + behavioral response per chunk.
Estimated cognitive load: 0.62 (moderate).

[OR-Tools CP-SAT] Running scheduler...
Hard block: 00:00–11:00 (behavioral constraint).
5 tasks placed. Status: OPTIMAL. TMT priority applied.

[Voice of Jarvis] Synthesizing response...
```

Similarly for `DEMO_THINKING_INGESTION` and `DEMO_THINKING_BEHAVIORAL`.

---

## Visual Structure (per assistant message)

```
┌─────────────────────────────────────────────────────┐
│ [PLAN_DAY]                                  (badge)  │
├─────────────────────────────────────────────────────┤
│ ▼ ⚙ Internal reasoning                              │
│   [Brain Dump Extraction] Detected intent...         │
│   [Habit Translation] Found constraint...            │
│   [Socratic Chunker] Breaking into 5 chunks...       │
│   [OR-Tools CP-SAT] Status: OPTIMAL...               │
│   [Voice of Jarvis] Synthesizing...                  │
├─────────────────────────────────────────────────────┤
│  Here's your schedule. I've broken down...           │
│  (main message)                                      │
├─────────────────────────────────────────────────────┤
│ ▼ 📋 Task breakdown (5 tasks)                       │
│   ● Quantifiers: universal & existential    25 min   │
│     ████░░░░░░  0.6 difficulty                       │
│     ✓ Explain ∀ and ∃ with 2 examples                │
│     ↳ If distracted → 2-min walk                    │
│   ...                                               │
├─────────────────────────────────────────────────────┤
│ ▶ 🗓 Schedule (5 tasks)          [collapsed]        │
└─────────────────────────────────────────────────────┘
```

---

## Verification

1. Run `cd jarvis-demo && npm run dev`
2. Open `http://localhost:3000/chat` in demo mode
3. Send "Plan my day to study for math mid-sem"
  - Sections should cascade in: badge → thinking → message → task cards → schedule
4. Collapse/expand thinking section → should toggle correctly
5. Switch to another intent (habit/ingestion) → verify only relevant sections appear
6. Send a second message → first message should be fully static, new message animates

