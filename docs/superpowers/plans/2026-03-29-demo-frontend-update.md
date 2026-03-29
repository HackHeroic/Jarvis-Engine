# Demo Frontend Update — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the jarvis-demo frontend to showcase Phase 1 backend features — prompt selectors, memory panel, PEARL insights, document feedback, visual polish — for the VC pitch on Wednesday April 1.

**Architecture:** Modify the existing Next.js 14 jarvis-demo app. Add 5 new components (PromptSelector, MemoryPanel, PearlInsightBanner, DocumentClassificationToast, RejectionReasonModal). Update existing components for intent badges, draft wiring, and light mode. Add GSAP for hero animations. All changes work in both demo (mock data) and live (real backend) modes.

**Tech Stack:** Next.js 14, React 18, TypeScript, Tailwind CSS, Motion (framer-motion), GSAP, Mermaid

**Spec:** `docs/superpowers/specs/2026-03-29-demo-frontend-update-design.md`

**Frontend project:** `/Users/madhav/Jarvis-cursor/jarvis-demo/`

**NOTE:** All file paths in this plan are relative to `/Users/madhav/Jarvis-cursor/jarvis-demo/` unless otherwise stated.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `lib/themeContext.tsx` | Default to light mode |
| Modify | `app/layout.tsx` | Light mode in inline script |
| Modify | `app/globals.css` | Consistent border-radius, shadows, design tokens |
| Modify | `lib/demoData.ts` | Move ChatResponse to jarvis-types.ts, add 4 scenario mocks + memories |
| Modify | `lib/jarvis-types.ts` | Add ChatResponse type, MemoryRecord, PearlInsight, DocumentClassification |
| Modify | `lib/api.ts` | Update simulateDemoStream routing for 4 scenarios, add draft API calls |
| Modify | `lib/useJarvisChat.ts` | Add memories, pearlInsights, documentClassification state |
| Create | `components/PromptSelector.tsx` | 4 scenario cards, full grid → compact strip |
| Create | `components/MemoryPanel.tsx` | "Jarvis Knows" sidebar with memory types |
| Create | `components/PearlInsightBanner.tsx` | Behavioral insight banner with confidence |
| Create | `components/DocumentClassificationToast.tsx` | Upload classification feedback |
| Create | `components/RejectionReasonModal.tsx` | Draft rejection with reason capture |
| Modify | `components/JarvisChatPanel.tsx` | Integrate PromptSelector, MemoryPanel toggle |
| Modify | `components/JarvisResponse.tsx` | Update intent badges, add PEARL banner |
| Modify | `components/ScheduleSection.tsx` | Constraint-applied badges, draft API wiring |
| Modify | `lib/architectureDiagrams.ts` | Replace with 3 diagrams from PITCH_ARCHITECTURE.md |
| Modify | `app/page.tsx` | GSAP hero animation |

---

### Task 1: Light Mode Default + Design Token Polish

**Files:**
- Modify: `lib/themeContext.tsx`
- Modify: `app/layout.tsx`
- Modify: `app/globals.css`

- [ ] **Step 1: Change theme default to light**

In `lib/themeContext.tsx`, change line 18:

```typescript
// OLD:
const [theme, setThemeState] = useState<Theme>("dark");
// NEW:
const [theme, setThemeState] = useState<Theme>("light");
```

- [ ] **Step 2: Update inline script in layout.tsx**

In `app/layout.tsx`, replace the inline script (line 20) to default to light:

```typescript
__html: `(function(){var t=localStorage.getItem('jarvis-theme');if(t==='dark'||t==='light'){document.documentElement.setAttribute('data-theme',t)}else{document.documentElement.setAttribute('data-theme','light')}})();`,
```

The change: removed the `matchMedia` dark check — now defaults to `'light'` when no stored preference.

- [ ] **Step 3: Polish design tokens in globals.css**

In `app/globals.css`, find the `:root` / light mode CSS variables and update:

```css
/* Ensure these values in the light mode (default) section: */
--background: #ffffff;
--card-bg: #ffffff;
--border: #e2e8f0;
```

Add a global utility class after the variables:

```css
/* Consistent design tokens */
.card {
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
```

- [ ] **Step 4: Verify light mode**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npm run dev`
Open http://localhost:3000 — should load in light mode by default.

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/themeContext.tsx app/layout.tsx app/globals.css
git commit -m "feat(ui): default to light mode, polish design tokens"
```

---

### Task 2: ChatResponse Type Expansion

**Files:**
- Modify: `lib/jarvis-types.ts`
- Modify: `lib/demoData.ts`

- [ ] **Step 1: Move ChatResponse to jarvis-types.ts and extend**

In `lib/jarvis-types.ts`, remove the import of `ChatResponse` from `demoData` (line 6) and define it directly. Add the new Phase 1 fields:

```typescript
// Add at the top of jarvis-types.ts (replace the import line):

export interface MemoryRecord {
  id?: string;
  memory_type: "fact" | "preference" | "behavioral_pattern" | "temporal_event" | "goal" | "feedback" | "constraint";
  content: string;
  confidence: number;
}

export interface PearlInsight {
  insight: string;
  confidence: number;
}

export interface DocumentClassification {
  document_type: string;
  confidence: number;
  topics_covered: string[];
  problem_count?: number;
  deadline_detected?: string;
}

export interface ChatResponse {
  intent: string;
  message: string;
  schedule?: {
    status: string;
    schedule: Record<string, {
      start_min: number;
      end_min: number;
      title?: string;
      tmt_score?: number;
      constraint_applied?: string;
    }>;
    goal_metadata?: Record<string, unknown>;
    horizon_start?: string;
  };
  execution_graph?: {
    goal_metadata?: { objective?: string; goal_id?: string };
    decomposition: Array<{
      task_id: string;
      title: string;
      duration_minutes: number;
      difficulty_weight: number;
      completion_criteria?: string;
      implementation_intention?: string;
    }>;
  };
  ingestion_result?: Record<string, unknown>;
  action_proposals?: Array<Record<string, unknown>>;
  search_result?: Record<string, unknown>;
  suggested_action?: string;
  thinking_process?: string;
  awaiting_task_confirmation?: boolean;
  schedule_status?: "draft" | "accepted";
  draft_id?: string;
  memories?: MemoryRecord[];
  pearl_insights?: PearlInsight[];
  document_classification?: DocumentClassification;
  generation_metrics?: {
    total_tokens: number;
    total_time_s: number;
    tok_per_sec: number;
    ttft_ms: number | null;
    model: string;
  };
  conversation_id?: string;
  message_id?: string;
  clarification_options?: string[];
}
```

- [ ] **Step 2: Update demoData.ts to import from jarvis-types**

In `lib/demoData.ts`, replace the `ChatResponse` interface definition (lines 6-43) with an import:

```typescript
import type { ChatResponse } from "./jarvis-types";
```

Remove the old `export interface ChatResponse { ... }` block entirely.

- [ ] **Step 3: Fix any other files importing ChatResponse from demoData**

Search for `from "./demoData"` or `from "../lib/demoData"` that import `ChatResponse` and update them to import from `"./jarvis-types"` or `"../lib/jarvis-types"` instead.

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && grep -r "ChatResponse" --include="*.ts" --include="*.tsx" -l`

Update each file found.

- [ ] **Step 4: Verify build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit`
Expected: No type errors.

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/jarvis-types.ts lib/demoData.ts
git commit -m "feat(types): expand ChatResponse with memories, PEARL insights, document classification"
```

---

### Task 3: Prompt Selector Cards

**Files:**
- Create: `components/PromptSelector.tsx`
- Modify: `components/JarvisChatPanel.tsx`

- [ ] **Step 1: Create PromptSelector component**

```typescript
// components/PromptSelector.tsx
"use client";

import { motion } from "motion/react";
import { useState } from "react";

interface PromptCard {
  id: string;
  icon: string;
  title: string;
  prompt: string;
  color: string;
  bgGradient: string;
  borderColor: string;
  isUpload?: boolean;
  dependsOn?: string;
}

const SCENARIO_CARDS: PromptCard[] = [
  {
    id: "learn",
    icon: "🎓",
    title: "Learn a Concept",
    prompt: "Teach me Dijkstra's algorithm — explain with a step-by-step example",
    color: "#10b981",
    bgGradient: "from-emerald-500/8 to-emerald-500/2",
    borderColor: "border-emerald-500/20",
  },
  {
    id: "plan",
    icon: "📋",
    title: "Plan a Complex Task",
    prompt: "I have a deep learning contest Friday — I need to study CNNs, backpropagation, and optimization algorithms. Also have a calculus exam Monday covering integration and limits. Plan my week.",
    color: "#3b82f6",
    bgGradient: "from-blue-500/8 to-blue-500/2",
    borderColor: "border-blue-500/20",
  },
  {
    id: "habit",
    icon: "⚡",
    title: "Add Contradicting Habit",
    prompt: "I don't work before 2 PM and need 1-hour breaks between study sessions",
    color: "#f59e0b",
    bgGradient: "from-amber-500/8 to-amber-500/2",
    borderColor: "border-amber-500/20",
    dependsOn: "plan",
  },
  {
    id: "upload",
    icon: "📄",
    title: "Upload & Link Material",
    prompt: "Here are practice problems for my deep learning contest",
    color: "#8b5cf6",
    bgGradient: "from-violet-500/8 to-violet-500/2",
    borderColor: "border-violet-500/20",
    isUpload: true,
    dependsOn: "plan",
  },
];

interface PromptSelectorProps {
  onSelectPrompt: (prompt: string) => void;
  onSelectFile?: () => void;
  usedCards: Set<string>;
  compact?: boolean;
}

export default function PromptSelector({
  onSelectPrompt,
  onSelectFile,
  usedCards,
  compact = false,
}: PromptSelectorProps) {
  const handleClick = (card: PromptCard) => {
    if (card.isUpload && onSelectFile) {
      onSelectFile();
    }
    onSelectPrompt(card.prompt);
  };

  if (compact) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex gap-2 px-4 py-2 overflow-x-auto"
      >
        <span className="text-xs text-[var(--muted)] whitespace-nowrap self-center">Try next:</span>
        {SCENARIO_CARDS.filter((c) => !usedCards.has(c.id)).map((card) => (
          <button
            key={card.id}
            onClick={() => handleClick(card)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs border border-[var(--border)] hover:border-[var(--accent)] transition-all whitespace-nowrap"
          >
            <span>{card.icon}</span>
            <span>{card.title}</span>
          </button>
        ))}
      </motion.div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center py-12 px-4">
      <p className="text-sm text-[var(--muted)] mb-6">Try a scenario:</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 max-w-2xl w-full">
        {SCENARIO_CARDS.map((card, index) => (
          <motion.button
            key={card.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.1, type: "spring", stiffness: 300, damping: 25 }}
            whileHover={{ y: -2, scale: 1.01 }}
            whileTap={{ scale: 0.98 }}
            onClick={() => handleClick(card)}
            className={`relative text-left p-4 rounded-xl border ${card.borderColor} bg-gradient-to-br ${card.bgGradient} hover:shadow-md transition-shadow`}
          >
            {usedCards.has(card.id) && (
              <span className="absolute top-2 right-2 text-emerald-500">✓</span>
            )}
            {card.dependsOn && !usedCards.has(card.dependsOn) && (
              <span className="absolute top-2 right-2 text-[10px] text-[var(--muted)]">
                After: {SCENARIO_CARDS.find((c) => c.id === card.dependsOn)?.title}
              </span>
            )}
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-lg">{card.icon}</span>
              <span className="text-sm font-semibold" style={{ color: card.color }}>
                {card.title}
              </span>
            </div>
            <p className="text-xs text-[var(--muted)] line-clamp-2">"{card.prompt}"</p>
          </motion.button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Wire PromptSelector into JarvisChatPanel**

In `components/JarvisChatPanel.tsx`, add the import and state:

```typescript
import PromptSelector from "./PromptSelector";

// Inside the component, add state:
const [usedCards, setUsedCards] = useState<Set<string>>(new Set());
```

Add the `handleSelectPrompt` function:

```typescript
const handleSelectPrompt = (prompt: string) => {
  // Find which card was used
  const card = ["learn", "plan", "habit", "upload"].find(
    (id) => SCENARIO_CARDS_MAP[id] === prompt
  );
  if (card) setUsedCards((prev) => new Set([...prev, card]));

  // In demo mode: send immediately. In live mode: fill input.
  if (isDemoMode) {
    handleSend(prompt);
  } else {
    setInput(prompt);
  }
};
```

Render PromptSelector in the message area:
- Full grid when `messages.length === 0`
- Compact strip when `messages.length > 0 && usedCards.size < 4`

- [ ] **Step 3: Verify prompt selector appears**

Run: `npm run dev`, open http://localhost:3000/chat
Expected: 4 scenario cards visible in empty chat. Click one → sends/fills prompt.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/PromptSelector.tsx components/JarvisChatPanel.tsx
git commit -m "feat(ui): add prompt selector cards with 4 demo scenarios"
```

---

### Task 4: Demo Mode Mock Data (4 Scenarios + Memories)

**Files:**
- Modify: `lib/demoData.ts`
- Modify: `lib/api.ts`

- [ ] **Step 1: Add 4 scenario responses to demoData.ts**

Add these constants to `lib/demoData.ts`:

```typescript
export const DEMO_RESPONSE_DIJKSTRA: ChatResponse = {
  intent: "CHAT",
  message: "## Dijkstra's Algorithm\n\nDijkstra's algorithm finds the shortest path from a source node to all other nodes in a weighted graph with non-negative weights.\n\n### How it works:\n\n1. **Initialize:** Set distance to source = 0, all others = ∞\n2. **Pick:** Select unvisited node with smallest distance\n3. **Update:** For each neighbor, check if going through current node is shorter\n4. **Repeat:** Until all nodes visited\n\n### Example:\n```\n  A --2-- B\n  |       |\n  4       1\n  |       |\n  C --3-- D\n```\n\nStarting from A: A=0, B=2, D=3 (via B), C=4\n\n### Time Complexity: O((V + E) log V) with a min-heap",
  thinking_process: "The user wants to learn Dijkstra's algorithm. I'll explain the concept step by step with a visual example and complexity analysis.",
  memories: [
    { memory_type: "fact", content: "User is interested in graph algorithms", confidence: 0.6 },
  ],
};

export const DEMO_RESPONSE_PLAN_WEEK: ChatResponse = {
  intent: "PLAN_DAY",
  message: "I've created a study plan for your deep learning contest (Friday) and calculus exam (Monday). The schedule respects cognitive load — harder topics are placed during peak focus hours, with breaks between sessions.",
  thinking_process: "User has two deadlines: DL contest Friday and calculus exam Monday. I need to decompose both goals into micro-tasks and schedule them across the week while respecting cognitive load theory.",
  schedule_status: "draft",
  draft_id: "demo-draft-001",
  execution_graph: {
    goal_metadata: { objective: "DL contest prep + Calculus exam prep", goal_id: "demo-goal-1" },
    decomposition: [
      { task_id: "dl_1", title: "Study CNNs — convolution layers and pooling", duration_minutes: 25, difficulty_weight: 0.6, completion_criteria: "Explain convolution operation and output dimensions" },
      { task_id: "dl_2", title: "Study backpropagation — chain rule and gradients", duration_minutes: 25, difficulty_weight: 0.8, completion_criteria: "Derive gradient for a 2-layer network" },
      { task_id: "dl_3", title: "Study optimization — SGD, Adam, learning rate", duration_minutes: 25, difficulty_weight: 0.5, completion_criteria: "Compare SGD vs Adam with momentum" },
      { task_id: "dl_4", title: "Practice: implement basic CNN forward pass", duration_minutes: 25, difficulty_weight: 0.7, completion_criteria: "Working convolution + pooling in Python" },
      { task_id: "dl_5", title: "DL mock contest — timed practice problems", duration_minutes: 25, difficulty_weight: 0.6, completion_criteria: "Complete 3 problems in 25 minutes" },
      { task_id: "calc_1", title: "Review integration techniques — substitution, parts", duration_minutes: 25, difficulty_weight: 0.5, completion_criteria: "Solve 5 integration problems" },
      { task_id: "calc_2", title: "Practice limits — L'Hôpital and squeeze theorem", duration_minutes: 25, difficulty_weight: 0.6, completion_criteria: "Evaluate 5 limit problems" },
      { task_id: "calc_3", title: "Calculus practice exam — timed", duration_minutes: 25, difficulty_weight: 0.7, completion_criteria: "Score 80%+ on practice exam" },
    ],
  },
  schedule: {
    status: "OPTIMAL",
    horizon_start: new Date().toISOString(),
    schedule: {
      dl_1: { start_min: 540, end_min: 565, title: "Study CNNs", tmt_score: 8 },
      dl_2: { start_min: 570, end_min: 595, title: "Study backpropagation", tmt_score: 9 },
      calc_1: { start_min: 630, end_min: 655, title: "Review integration", tmt_score: 7 },
      dl_3: { start_min: 660, end_min: 685, title: "Study optimization", tmt_score: 6 },
      calc_2: { start_min: 720, end_min: 745, title: "Practice limits", tmt_score: 7 },
      dl_4: { start_min: 810, end_min: 835, title: "Implement CNN", tmt_score: 8 },
      dl_5: { start_min: 840, end_min: 865, title: "DL mock contest", tmt_score: 7 },
      calc_3: { start_min: 900, end_min: 925, title: "Calculus practice exam", tmt_score: 8 },
    },
    goal_metadata: { objective: "DL contest + Calculus exam" },
  },
  memories: [
    { memory_type: "goal", content: "DL contest on Friday", confidence: 0.95 },
    { memory_type: "goal", content: "Calculus exam on Monday", confidence: 0.95 },
    { memory_type: "fact", content: "Studying CNNs, backpropagation, optimization", confidence: 0.8 },
  ],
};

export const DEMO_RESPONSE_CONTRADICT_HABIT: ChatResponse = {
  intent: "ADD_CONSTRAINT",
  message: "Got it — I've recalibrated your schedule. All tasks are now after 2 PM with 1-hour breaks between sessions. Your deep learning and calculus prep will fit in the afternoon focus window.\n\n⚡ **Schedule recalibrated** — 8 tasks shifted to respect your constraint.",
  thinking_process: "User added a constraint that contradicts the current schedule. Tasks were scheduled in the morning. I need to shift everything to after 2 PM and add 1-hour gaps between sessions.",
  schedule_status: "draft",
  draft_id: "demo-draft-002",
  schedule: {
    status: "OPTIMAL",
    horizon_start: new Date().toISOString(),
    schedule: {
      dl_1: { start_min: 840, end_min: 865, title: "Study CNNs", tmt_score: 8, constraint_applied: "No work before 2 PM" },
      dl_2: { start_min: 925, end_min: 950, title: "Study backpropagation", tmt_score: 9, constraint_applied: "No work before 2 PM" },
      calc_1: { start_min: 1010, end_min: 1035, title: "Review integration", tmt_score: 7, constraint_applied: "No work before 2 PM" },
      dl_3: { start_min: 1095, end_min: 1120, title: "Study optimization", tmt_score: 6 },
    },
    goal_metadata: { objective: "DL contest + Calculus exam (recalibrated)" },
  },
  memories: [
    { memory_type: "constraint", content: "No work before 2 PM", confidence: 0.95 },
    { memory_type: "constraint", content: "1-hour breaks between study sessions", confidence: 0.95 },
  ],
  pearl_insights: [
    {
      insight: "Your new habit matches what I've observed — you complete 92% of tasks scheduled after 2 PM, but only 35% before 10 AM. I've made 'no work before 2 PM' a permanent scheduling rule.",
      confidence: 0.92,
    },
  ],
};

export const DEMO_RESPONSE_UPLOAD_PDF: ChatResponse = {
  intent: "INGEST_DOCUMENT",
  message: "I've processed your practice problems PDF. Detected **10 practice problems** covering CNNs and backpropagation. I've linked them to your existing study tasks as completion criteria.\n\n📄 **3 tasks enriched** with practice problems from your upload.",
  thinking_process: "User uploaded a PDF. Document classifier identified it as practice_problems. Extracted 10 individual problems, matched by topic to existing tasks.",
  document_classification: {
    document_type: "practice_problems",
    confidence: 0.92,
    topics_covered: ["CNNs", "backpropagation", "convolution"],
    problem_count: 10,
  },
  memories: [
    { memory_type: "fact", content: "Uploaded practice problems for DL contest", confidence: 0.8 },
  ],
};

export const MOCK_MEMORIES: import("./jarvis-types").MemoryRecord[] = [
  { memory_type: "goal", content: "DL contest on Friday", confidence: 0.95 },
  { memory_type: "goal", content: "Calculus exam on Monday", confidence: 0.95 },
  { memory_type: "constraint", content: "No work before 2 PM", confidence: 0.95 },
  { memory_type: "constraint", content: "1-hour breaks between sessions", confidence: 0.95 },
  { memory_type: "behavioral_pattern", content: "Completes 92% of tasks after 2 PM", confidence: 0.92 },
  { memory_type: "behavioral_pattern", content: "Skips 65% of tasks before 10 AM", confidence: 0.87 },
  { memory_type: "fact", content: "Studying CNNs, backpropagation, optimization", confidence: 0.8 },
  { memory_type: "fact", content: "Uploaded practice problems for DL contest", confidence: 0.8 },
  { memory_type: "temporal_event", content: "DL contest: Friday March 31", confidence: 0.95 },
  { memory_type: "preference", content: "Prefers afternoon study sessions", confidence: 0.85 },
];
```

- [ ] **Step 2: Update simulateDemoStream routing in api.ts**

In `lib/api.ts`, find the `getMockChatResponse` function and update it to route 4 scenarios:

```typescript
function getMockChatResponse(prompt: string): ChatResponse {
  const lower = prompt.toLowerCase();

  if (lower.includes("dijkstra") || lower.includes("teach me") || lower.includes("explain"))
    return DEMO_RESPONSE_DIJKSTRA;

  if (lower.includes("contest") || lower.includes("exam") || lower.includes("plan my"))
    return DEMO_RESPONSE_PLAN_WEEK;

  if (lower.includes("don't work before") || lower.includes("breaks between") || lower.includes("habit"))
    return DEMO_RESPONSE_CONTRADICT_HABIT;

  if (lower.includes("practice problems") || lower.includes("here are"))
    return DEMO_RESPONSE_UPLOAD_PDF;

  return DEMO_RESPONSE_DIJKSTRA; // Fallback
}
```

Add the imports at the top of `api.ts`:

```typescript
import {
  DEMO_RESPONSE_DIJKSTRA,
  DEMO_RESPONSE_PLAN_WEEK,
  DEMO_RESPONSE_CONTRADICT_HABIT,
  DEMO_RESPONSE_UPLOAD_PDF,
} from "./demoData";
```

- [ ] **Step 3: Verify demo mode responses**

Run: `npm run dev`, switch to Demo mode, click each scenario card.
Expected: Each card triggers the correct mock response with streaming.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/demoData.ts lib/api.ts
git commit -m "feat(demo): add 4 scenario mock responses + memories + PEARL insights"
```

---

### Task 5: Memory Panel — "Jarvis Knows"

**Files:**
- Create: `components/MemoryPanel.tsx`
- Modify: `components/JarvisChatPanel.tsx`

- [ ] **Step 1: Create MemoryPanel component**

```typescript
// components/MemoryPanel.tsx
"use client";

import { motion, AnimatePresence } from "motion/react";
import type { MemoryRecord } from "@/lib/jarvis-types";

const TYPE_CONFIG: Record<string, { icon: string; label: string; color: string }> = {
  constraint: { icon: "🔒", label: "Scheduling Constraints", color: "#ef4444" },
  goal: { icon: "🎯", label: "Active Goals", color: "#3b82f6" },
  behavioral_pattern: { icon: "📊", label: "Observed Patterns", color: "#8b5cf6" },
  preference: { icon: "💡", label: "Preferences", color: "#f59e0b" },
  fact: { icon: "📌", label: "Facts", color: "#64748b" },
  temporal_event: { icon: "📅", label: "Upcoming Events", color: "#06b6d4" },
  feedback: { icon: "💬", label: "Feedback", color: "#10b981" },
};

interface MemoryPanelProps {
  memories: MemoryRecord[];
  isOpen: boolean;
  onToggle: () => void;
}

export default function MemoryPanel({ memories, isOpen, onToggle }: MemoryPanelProps) {
  const grouped = memories.reduce<Record<string, MemoryRecord[]>>((acc, mem) => {
    const type = mem.memory_type;
    if (!acc[type]) acc[type] = [];
    acc[type].push(mem);
    return acc;
  }, {});

  const sessionCount = memories.length;

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div
          initial={{ x: 300, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          exit={{ x: 300, opacity: 0 }}
          transition={{ type: "spring", stiffness: 300, damping: 30 }}
          className="w-72 border-l border-[var(--border)] bg-[var(--card-bg)] p-4 overflow-y-auto"
        >
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold flex items-center gap-2">
              🧠 Jarvis Knows
            </h3>
            <button onClick={onToggle} className="text-xs text-[var(--muted)] hover:text-[var(--foreground)]">
              ✕
            </button>
          </div>

          {Object.entries(TYPE_CONFIG).map(([type, config]) => {
            const items = grouped[type];
            if (!items || items.length === 0) return null;

            return (
              <div key={type} className="mb-4">
                <h4 className="text-xs font-medium mb-1.5 flex items-center gap-1.5" style={{ color: config.color }}>
                  <span>{config.icon}</span>
                  {config.label}
                </h4>
                <ul className="space-y-1">
                  {items.map((mem, i) => (
                    <motion.li
                      key={`${type}-${i}`}
                      initial={{ opacity: 0, x: 10 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: i * 0.05 }}
                      className="text-xs text-[var(--foreground)] pl-5 relative"
                    >
                      <span className="absolute left-0 top-0.5 w-1.5 h-1.5 rounded-full" style={{ backgroundColor: config.color, opacity: mem.confidence }} />
                      {mem.content}
                    </motion.li>
                  ))}
                </ul>
              </div>
            );
          })}

          {sessionCount > 0 && (
            <p className="text-[10px] text-[var(--muted)] mt-4 pt-3 border-t border-[var(--border)]">
              {sessionCount} memories this session
            </p>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
```

- [ ] **Step 2: Wire MemoryPanel into JarvisChatPanel**

In `components/JarvisChatPanel.tsx`, add:

```typescript
import MemoryPanel from "./MemoryPanel";

// State:
const [memoryPanelOpen, setMemoryPanelOpen] = useState(false);
const [memories, setMemories] = useState<MemoryRecord[]>([]);

// After each response, accumulate memories:
// (In the onComplete handler or wherever ChatResponse is processed)
if (response.memories) {
  setMemories((prev) => [...prev, ...response.memories]);
}
```

Add a memory panel toggle button in the chat header:

```tsx
<button
  onClick={() => setMemoryPanelOpen(!memoryPanelOpen)}
  className="text-xs px-2 py-1 rounded border border-[var(--border)] hover:bg-[var(--accent)]/10"
>
  🧠 {memories.length > 0 ? `(${memories.length})` : ""}
</button>
```

Render the panel alongside the chat:

```tsx
<div className="flex h-full">
  <div className="flex-1 flex flex-col">
    {/* existing chat content */}
  </div>
  <MemoryPanel
    memories={memories}
    isOpen={memoryPanelOpen}
    onToggle={() => setMemoryPanelOpen(false)}
  />
</div>
```

- [ ] **Step 3: Verify memory panel**

Run: `npm run dev`, demo mode, click "Plan a Complex Task" → Memory panel toggle appears → click → shows goals and facts.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/MemoryPanel.tsx components/JarvisChatPanel.tsx
git commit -m "feat(ui): add Memory Panel — 'Jarvis Knows' sidebar with memory types"
```

---

### Task 6: PEARL Insight Banner

**Files:**
- Create: `components/PearlInsightBanner.tsx`
- Modify: `components/JarvisResponse.tsx`

- [ ] **Step 1: Create PearlInsightBanner component**

```typescript
// components/PearlInsightBanner.tsx
"use client";

import { motion } from "motion/react";
import { useState } from "react";
import type { PearlInsight } from "@/lib/jarvis-types";

interface PearlInsightBannerProps {
  insight: PearlInsight;
}

export default function PearlInsightBanner({ insight }: PearlInsightBannerProps) {
  const [dismissed, setDismissed] = useState(false);

  if (dismissed) return null;

  const confidencePct = Math.round(insight.confidence * 100);
  const filledBars = Math.round(insight.confidence * 10);

  return (
    <motion.div
      initial={{ opacity: 0, y: -20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      transition={{ type: "spring", stiffness: 300, damping: 25 }}
      className="mx-4 my-2 p-4 rounded-xl border border-violet-500/20 bg-gradient-to-r from-violet-500/5 to-purple-500/5"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-sm">📊</span>
            <span className="text-xs font-semibold text-violet-500">Behavioral Insight</span>
          </div>
          <p className="text-sm text-[var(--foreground)]">{insight.insight}</p>
          <div className="flex items-center gap-2 mt-2">
            <span className="text-[10px] text-[var(--muted)]">Confidence:</span>
            <div className="flex gap-0.5">
              {Array.from({ length: 10 }, (_, i) => (
                <div
                  key={i}
                  className="w-1.5 h-3 rounded-sm"
                  style={{
                    backgroundColor: i < filledBars ? "#8b5cf6" : "var(--border)",
                  }}
                />
              ))}
            </div>
            <span className="text-[10px] text-[var(--muted)]">{confidencePct}%</span>
          </div>
        </div>
        <button
          onClick={() => setDismissed(true)}
          className="text-xs text-[var(--muted)] hover:text-[var(--foreground)]"
        >
          ✕
        </button>
      </div>
    </motion.div>
  );
}
```

- [ ] **Step 2: Wire into JarvisResponse**

In `components/JarvisResponse.tsx`, import and render PEARL insights:

```typescript
import PearlInsightBanner from "./PearlInsightBanner";

// Inside the response rendering, after the message content:
{response?.pearl_insights?.map((insight, i) => (
  <PearlInsightBanner key={i} insight={insight} />
))}
```

- [ ] **Step 3: Verify PEARL banner**

Run: `npm run dev`, demo mode, click "Add Contradicting Habit" → PEARL insight banner appears with confidence bar.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/PearlInsightBanner.tsx components/JarvisResponse.tsx
git commit -m "feat(ui): add PEARL insight banner with confidence indicator"
```

---

### Task 7: Document Classification Toast

**Files:**
- Create: `components/DocumentClassificationToast.tsx`
- Modify: `components/JarvisResponse.tsx`

- [ ] **Step 1: Create toast component**

```typescript
// components/DocumentClassificationToast.tsx
"use client";

import { motion } from "motion/react";
import { useEffect, useState } from "react";
import type { DocumentClassification } from "@/lib/jarvis-types";

interface DocumentClassificationToastProps {
  classification: DocumentClassification;
  autoDismissMs?: number;
}

const TYPE_LABELS: Record<string, string> = {
  practice_problems: "Practice Problems Detected",
  lecture_notes: "Lecture Notes Processed",
  syllabus: "Syllabus Analyzed",
  assignment: "Assignment Detected",
  reference: "Reference Material Stored",
};

export default function DocumentClassificationToast({
  classification,
  autoDismissMs = 8000,
}: DocumentClassificationToastProps) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const timer = setTimeout(() => setVisible(false), autoDismissMs);
    return () => clearTimeout(timer);
  }, [autoDismissMs]);

  if (!visible) return null;

  const label = TYPE_LABELS[classification.document_type] || "Document Processed";
  const confidencePct = Math.round(classification.confidence * 100);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20, x: 20 }}
      animate={{ opacity: 1, y: 0, x: 0 }}
      exit={{ opacity: 0, y: 20 }}
      className="fixed bottom-20 right-4 z-50 w-80 p-4 rounded-xl border border-[var(--border)] bg-[var(--card-bg)] shadow-lg"
    >
      <div className="flex items-start gap-3">
        <span className="text-2xl">📄</span>
        <div className="flex-1">
          <h4 className="text-sm font-semibold text-[var(--foreground)]">{label}</h4>
          {classification.problem_count && (
            <p className="text-xs text-[var(--muted)] mt-1">
              {classification.problem_count} problems found
            </p>
          )}
          {classification.topics_covered.length > 0 && (
            <p className="text-xs text-[var(--muted)]">
              Topics: {classification.topics_covered.join(", ")}
            </p>
          )}
          <div className="flex items-center gap-2 mt-2">
            <span className="text-[10px] text-[var(--muted)]">Confidence: {confidencePct}%</span>
          </div>
        </div>
        <button onClick={() => setVisible(false)} className="text-xs text-[var(--muted)]">✕</button>
      </div>
    </motion.div>
  );
}
```

- [ ] **Step 2: Wire into JarvisResponse**

In `components/JarvisResponse.tsx`, render the toast when document_classification is present:

```typescript
import DocumentClassificationToast from "./DocumentClassificationToast";

// Inside response rendering:
{response?.document_classification && (
  <DocumentClassificationToast classification={response.document_classification} />
)}
```

- [ ] **Step 3: Verify toast**

Run: `npm run dev`, demo mode, click "Upload & Link Material" → Toast appears bottom-right with classification details.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/DocumentClassificationToast.tsx components/JarvisResponse.tsx
git commit -m "feat(ui): add document classification toast for upload feedback"
```

---

### Task 8: Intent Badge Updates + Constraint-Applied Badges

**Files:**
- Modify: `components/JarvisResponse.tsx`
- Modify: `components/ScheduleSection.tsx`

- [ ] **Step 1: Update intent badges in JarvisResponse.tsx**

Find the intent badge rendering in `JarvisResponse.tsx` and update the color map:

```typescript
const INTENT_COLORS: Record<string, string> = {
  PLAN_DAY: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
  EDIT_TASK: "bg-cyan-500/10 text-cyan-600 border-cyan-500/20",
  REARRANGE: "bg-violet-500/10 text-violet-600 border-violet-500/20",
  ADD_CONSTRAINT: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  ACCEPT_DRAFT: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
  REJECT_DRAFT: "bg-red-500/10 text-red-600 border-red-500/20",
  INGEST_DOCUMENT: "bg-blue-500/10 text-blue-600 border-blue-500/20",
  CHECK_PROGRESS: "bg-teal-500/10 text-teal-600 border-teal-500/20",
  CHAT: "bg-slate-500/10 text-slate-600 border-slate-500/20",
  // Legacy mappings
  GREETING: "bg-slate-500/10 text-slate-600 border-slate-500/20",
  GENERAL_QA: "bg-slate-500/10 text-slate-600 border-slate-500/20",
  KNOWLEDGE_INGESTION: "bg-blue-500/10 text-blue-600 border-blue-500/20",
  BEHAVIORAL_CONSTRAINT: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  CALENDAR_SYNC: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  ACTION_ITEM: "bg-cyan-500/10 text-cyan-600 border-cyan-500/20",
};
```

- [ ] **Step 2: Add constraint-applied badges to ScheduleSection.tsx**

In `components/ScheduleSection.tsx`, find where each task slot is rendered and add the constraint badge:

```tsx
{/* Inside the task slot rendering, after the title */}
{task.constraint_applied && (
  <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 border border-amber-500/20">
    ⚡ {task.constraint_applied}
  </span>
)}
```

- [ ] **Step 3: Verify badges**

Run: `npm run dev`, demo mode, click "Plan Complex Task" → intent badge shows "PLAN_DAY" in emerald. Click "Add Contradicting Habit" → shifted tasks show "⚡ No work before 2 PM" badges.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/JarvisResponse.tsx components/ScheduleSection.tsx
git commit -m "feat(ui): update intent badges + add constraint-applied indicators on schedule"
```

---

### Task 9: Architecture Diagrams Update

**Files:**
- Modify: `lib/architectureDiagrams.ts`

- [ ] **Step 1: Replace diagrams with Phase 1 versions**

Read `docs/PITCH_ARCHITECTURE.md` (in Jarvis-Engine) and copy the Mermaid code for:
1. Diagram 1: Core Loop (stateDiagram)
2. Diagram 3: Memory-to-Constraint Bridge (stateDiagram)
3. Diagram 10: Platform Roadmap (flowchart)

Replace the contents of `lib/architectureDiagrams.ts` with these 3 diagrams using the existing format (array of `{ id, title, description, mermaid }` objects).

- [ ] **Step 2: Verify diagrams render**

Run: `npm run dev`, open http://localhost:3000/architecture → Should show 3 updated diagrams.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/architectureDiagrams.ts
git commit -m "feat(ui): update architecture diagrams — Core Loop, Memory Bridge, Roadmap"
```

---

### Task 10: GSAP Hero Animation (Cut if Tight)

**Files:**
- Modify: `app/page.tsx`

- [ ] **Step 1: Install GSAP**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo && npm install gsap
```

- [ ] **Step 2: Add hero text animation**

In `app/page.tsx`, add GSAP text reveal on the hero headline:

```typescript
"use client";
import { useEffect, useRef } from "react";
import gsap from "gsap";

// Inside the component:
const heroRef = useRef<HTMLHeadingElement>(null);

useEffect(() => {
  if (heroRef.current) {
    gsap.fromTo(
      heroRef.current.children,
      { opacity: 0, y: 30 },
      { opacity: 1, y: 0, stagger: 0.08, duration: 0.6, ease: "power3.out" }
    );
  }
}, []);
```

Wrap each word in a `<span>` for the stagger effect:

```tsx
<h1 ref={heroRef} className="text-5xl font-bold">
  {"Reclaim your focus with Jarvis".split(" ").map((word, i) => (
    <span key={i} className="inline-block mr-3">{word}</span>
  ))}
</h1>
```

- [ ] **Step 3: Verify animation**

Run: `npm run dev`, open http://localhost:3000 → Words stagger in on hero.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add app/page.tsx package.json package-lock.json
git commit -m "feat(ui): add GSAP hero text reveal animation"
```

---

## Complete Checklist

After completing all tasks, verify:

- [ ] Light mode is default (no dark flash on load)
- [ ] 4 prompt selector cards visible in chat
- [ ] Cards collapse to compact strip after first use
- [ ] Demo mode: each card triggers correct mock response
- [ ] Memory panel shows accumulated memories
- [ ] PEARL insight banner appears after Card 3
- [ ] Document classification toast appears after Card 4
- [ ] Intent badges show new names (PLAN_DAY, ADD_CONSTRAINT, etc.)
- [ ] Constraint-applied badges show on shifted tasks
- [ ] Architecture diagrams updated to Phase 1
- [ ] Hero animation works (if GSAP installed)
- [ ] Both demo and live modes work
- [ ] `npm run build` succeeds with no errors
