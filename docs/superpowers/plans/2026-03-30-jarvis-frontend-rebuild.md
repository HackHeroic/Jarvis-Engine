# Jarvis Frontend Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the jarvis-frontend to properly integrate with the Jarvis Engine backend (Phase 1A-1E), fix all regressions from Jarvis-Demo, and deliver a pitch-ready product by April 1.

**Architecture:** Port battle-tested code from Jarvis-Demo (`/Users/madhav/Jarvis-cursor/Jarvis-Demo/`) as the foundation, then extend with new features from the spec. The frontend is Next.js 14 + React 18, connecting to the Jarvis Engine backend at `localhost:8000` via SSE streaming.

**Tech Stack:** Next.js 14, React 18, TypeScript, Tailwind CSS 3.4, Motion (framer-motion), Lucide React, react-markdown, GSAP (for landing page)

**Spec:** `jarvis-frontend/docs/superpowers/specs/2026-03-30-jarvis-frontend-rebuild-design.md`

**Key principle:** Port from Jarvis-Demo wherever possible. Don't rewrite what already works.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Rewrite | `lib/api.ts` | Port from Demo — SSE streaming, REST endpoints, session/task/draft/document API |
| Rewrite | `lib/types.ts` | Port from Demo — TypeScript interfaces matching backend Pydantic schemas |
| Rewrite | `lib/hooks/useJarvisChat.ts` | Port from Demo — streaming state, draft workflow, conversation persistence |
| Extend | `lib/constants.ts` | Add PHASE_NAMES, INTENT_COLORS, IS_DEMO_MODE, USER_ID |
| Merge | `lib/store.ts` | Port Demo's scheduleStore.ts — full draft persistence, message capping |
| Refactor | `lib/providers.tsx` | Remove ModeContext, add ConversationContext + DraftContext |
| Fix | `app/layout.tsx` | Fix theme script to not conflict with ThemeProvider |
| Modify | `app/(app)/layout.tsx` | Add ConversationContext, DraftContext providers |
| Rewrite | `app/(app)/chat/page.tsx` | 3-column layout: SessionPanel + Chat + AIPanel |
| Rewrite | `app/(app)/dashboard/page.tsx` | Real data from backend APIs |
| Create | `app/(app)/workspace/[taskId]/page.tsx` | Task workspace with criteria + materials |
| Create | `app/(app)/documents/page.tsx` | Document management |
| Create | `app/(app)/habits/page.tsx` | Habits tracker + PEARL patterns |
| Create | `app/(app)/schedule/page.tsx` | Day view schedule |
| Create | `components/app/ChatSessionPanel.tsx` | Port from Demo SessionSidebar — conversation list |
| Create | `components/app/JarvisResponse.tsx` | Port from Demo — full response rendering |
| Create | `components/app/DraftReview.tsx` | Two-stage draft negotiation wrapper |
| Create | `components/app/TaskPreview.tsx` | Stage 1: editable task cards |
| Create | `components/app/SchedulePreview.tsx` | Stage 2: schedule timeline |
| Create | `components/app/MemoryPanel.tsx` | Type-agnostic memory display |
| Create | `components/app/PhaseProgress.tsx` | Fun streaming phase names |
| Create | `components/app/IntentBadge.tsx` | Dynamic color intent pills |
| Create | `components/app/MetricsBar.tsx` | TTFT, tok/sec, model display |
| Create | `components/app/ClarificationChips.tsx` | Quick-reply pills |
| Create | `components/app/InlineHabitStaging.tsx` | Save/ignore habit extraction |
| Create | `components/app/SM2QualityRating.tsx` | 0-5 quality rating |
| Create | `components/app/InfeasibleGuidance.tsx` | Anti-guilt recalibration |
| Create | `components/app/ReplanBanner.tsx` | Schedule outdated CTA |
| Modify | `components/app/AIChatPanel.tsx` | Functional with real streaming |
| Modify | `components/app/NavRail.tsx` | Fix theme toggle wiring |

---

## Phase 1: Fix Broken Features (MUST for pitch)

### Task 1: Port Types and Constants

**Files:**
- Rewrite: `jarvis-frontend/lib/types.ts`
- Extend: `jarvis-frontend/lib/constants.ts`
- Create: `jarvis-frontend/.env.local`

- [ ] **Step 1: Create .env.local with demo mode and user ID**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
```

Write to `jarvis-frontend/.env.local`:
```env
NEXT_PUBLIC_DEMO_MODE=false
NEXT_PUBLIC_USER_ID=demo
NEXT_PUBLIC_API_URL=http://localhost:8000
```

- [ ] **Step 2: Rewrite lib/types.ts**

Port from `Jarvis-Demo/lib/jarvis-types.ts` and align with the spec's TypeScript definitions. The file must include all interfaces: `ChatResponse`, `ChatRequest`, `ExecutionGraph`, `TaskChunk`, `GenerationMetrics`, `ActionItemProposal`, `MemoryRecord`, `TaskWorkspace`, `StudyAsset`, `Session`, `SessionMessage`, `ScheduleResponse`, `TaskSchedule`, `JarvisStreamPhase`, `JarvisStreamState`, `JarvisMessage`, `PhaseEventData`, `PreviewTask`.

Read the Demo's `jarvis-types.ts` first, then read the spec's TypeScript section (search for "TypeScript Type Definitions"). Merge them — use the spec's corrected versions where they differ (e.g., `ActionItemProposal` has `id`, `title`, `summary`, `suggested_actions`, `deadline_mentioned`, `deadline_date`, `created_at`; `search_result` is `Record<string, any>` not `string`; no `model_used` or `document_classification` top-level fields).

Key additions beyond Demo types:
```typescript
export const IS_DEMO_MODE = process.env.NEXT_PUBLIC_DEMO_MODE === 'true';
export const USER_ID = process.env.NEXT_PUBLIC_USER_ID || 'demo';
export const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
```

- [ ] **Step 3: Extend lib/constants.ts**

Read the current `jarvis-frontend/lib/constants.ts`. Keep existing `NAV_ITEMS`. Add:

```typescript
import { API_BASE, IS_DEMO_MODE, USER_ID } from './types';

export { API_BASE, IS_DEMO_MODE, USER_ID };

export const PHASE_NAMES: Record<string, string> = {
  connecting:       "Brewing your plan...",
  brain_dump_extraction: "Digesting your brain dump...",
  extracting:       "Digesting your brain dump...",
  intent_classified: "Aha, figuring out what you need...",
  classifying:      "Aha, figuring out what you need...",
  decomposing:      "Breaking it into bite-sized pieces...",
  translating:      "Reading your habits...",
  scheduling:       "Crunching the numbers...",
  reasoning:        "Putting on my thinking cap...",
  responding:       "Crafting your response...",
  synthesizing:     "Adding the finishing touches...",
  complete:         "Voila!",
};

export function getPhaseDisplayName(phase: string): string {
  if (typeof window !== 'undefined') {
    const custom = localStorage.getItem('jarvis-phase-names');
    if (custom) {
      try {
        const parsed = JSON.parse(custom);
        if (parsed[phase]) return parsed[phase];
      } catch {}
    }
  }
  return PHASE_NAMES[phase] || phase.replace(/_/g, ' ');
}

export const INTENT_COLORS: Record<string, string> = {
  PLAN_DAY: 'sage',
  EDIT_TASK: 'dusk',
  REARRANGE: 'dusk',
  ADD_CONSTRAINT: 'terra',
  ACCEPT_DRAFT: 'sage',
  REJECT_DRAFT: 'gold',
  INGEST_DOCUMENT: 'gold',
  CHECK_PROGRESS: 'dusk',
  CHAT: 'ink',
  GREETING: 'terra',
  GENERAL_QA: 'ink',
  CALENDAR_SYNC: 'gold',
  KNOWLEDGE_INGESTION: 'gold',
  BEHAVIORAL_CONSTRAINT: 'terra',
  ACTION_ITEM: 'dusk',
};

const PALETTE_ROTATION = ['terra', 'sage', 'dusk', 'gold'];

function hashCode(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash |= 0;
  }
  return Math.abs(hash);
}

export function getIntentColor(intent: string): string {
  return INTENT_COLORS[intent] || PALETTE_ROTATION[hashCode(intent) % PALETTE_ROTATION.length];
}

export const DEMO_LATENCY = 800;
```

- [ ] **Step 4: Verify imports resolve**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit --pretty 2>&1 | head -30`

Fix any import errors. This is a smoke test — not all files need to compile yet.

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/types.ts lib/constants.ts .env.local
git commit -m "feat: port types from Demo, add phase names, env-based demo mode"
```

---

### Task 2: Port API Layer

**Files:**
- Rewrite: `jarvis-frontend/lib/api.ts`
- Merge: `jarvis-frontend/lib/store.ts`

- [ ] **Step 1: Rewrite lib/api.ts**

Read `Jarvis-Demo/lib/api.ts` (the full 511-line file). Port it to `jarvis-frontend/lib/api.ts` with these changes:

1. Import `API_BASE`, `IS_DEMO_MODE`, `USER_ID` from `./constants` instead of hardcoding
2. Replace `isDemoMode()` localStorage check with `IS_DEMO_MODE` constant
3. Keep all functions: `chatStream`, `confirmScheduleStream`, `acceptSchedule`, `updateTask`, `completeTask`, `skipTask`, `deleteTask`, `listSessions`, `loadConversation`, `archiveSession`, `renameSession`, `listDocuments`, `deleteDocument`
4. Keep the SSE parser logic exactly as-is from Demo (it's battle-tested)
5. Replace hardcoded `"demo"` user_id with `USER_ID` constant
6. Keep demo data lazy loading pattern (`import('./demoData')`)
7. Add `getDraftById` function: `GET ${API_BASE}/api/v1/drafts/${draftId}?user_id=${USER_ID}`
8. Add `acceptDraft` function: `POST ${API_BASE}/api/v1/drafts/${draftId}/accept` with body `{ user_id: USER_ID }`
9. Add `rejectDraft` function: `POST ${API_BASE}/api/v1/drafts/${draftId}/reject` with body `{ user_id: USER_ID, components }`
10. Add `editDraftTask` function: `PATCH ${API_BASE}/api/v1/drafts/${draftId}/tasks/${taskId}` with body
11. Add `listTasks` function: `GET ${API_BASE}/api/v1/tasks/?user_id=${USER_ID}`
12. Add `getWorkspace` function: `GET ${API_BASE}/api/v1/tasks/${taskId}/workspace?user_id=${USER_ID}`
13. Add `processDocument` function: `POST ${API_BASE}/api/v1/ingestion/process` with body `{ file_base64, media_type, user_id: USER_ID }`
14. Add `getDueHabits` function: `GET ${API_BASE}/api/v1/habits/tracker/due?user_id=${USER_ID}`
15. Add `completeHabit` function: `POST ${API_BASE}/api/v1/habits/tracker/${id}/complete` with body `{ quality }`

The SSE parser from Demo uses a line-by-line approach. Keep it exactly — it handles partial events, multi-line data fields, and buffer management correctly.

- [ ] **Step 2: Merge lib/store.ts with Demo's scheduleStore.ts**

Read `Jarvis-Demo/lib/scheduleStore.ts` (87 lines). Read current `jarvis-frontend/lib/store.ts`. Merge:

Keep the current store.ts structure but add/replace these functions from Demo:
- `saveChatMessages(messages)` — caps at 50, strips `thinking_process`, caps `phaseHistory` to 20
- `loadChatMessages()` — returns parsed messages or empty array
- `saveDraftSchedule(response)` — stores full ChatResponse for draft
- `loadDraftSchedule()` — returns parsed response or null
- `clearDraftSchedule()` — removes draft from localStorage
- `promoteDraftToFinal()` — clears draft key

Keep existing keys: `jarvis-conversation-id`, `jarvis-theme`, `jarvis-show-metrics`, `jarvis-model-mode`.

Remove: any `jarvis-demo-mode` references (demo mode is now env-based).

- [ ] **Step 3: Verify api.ts compiles**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit --pretty 2>&1 | head -30`

Fix any type errors.

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/api.ts lib/store.ts
git commit -m "feat: port API layer from Demo — SSE streaming, sessions, tasks, drafts"
```

---

### Task 3: Port Chat Hook and Fix Providers

**Files:**
- Rewrite: `jarvis-frontend/lib/hooks/useJarvisChat.ts`
- Refactor: `jarvis-frontend/lib/providers.tsx`

- [ ] **Step 1: Rewrite useJarvisChat.ts**

Read `Jarvis-Demo/lib/useJarvisChat.ts` (570 lines). Port to `jarvis-frontend/lib/hooks/useJarvisChat.ts` with these changes:

1. Import from `../api` and `../types` and `../store` and `../constants` (adjust paths)
2. Replace `isDemoMode()` checks with `IS_DEMO_MODE`
3. Replace hardcoded `"demo"` with `USER_ID`
4. Keep ALL hook functionality:
   - `messages`, `streamState`, `isStreaming`
   - `sendMessage(content, options)` with file upload
   - `confirmTasks(editedTasks)` for Stage 1 → Stage 2
   - `acceptDraft()` / `rejectDraft()`
   - `conversationId`, `startNewConversation()`, `loadConversation(id)`
   - `triggerReplan()`
   - `pendingTasks` (extracted from execution_graph)
   - `draftScheduleResponse` (full response for draft UI)
   - `modelMode`, `setModelMode()`
5. Keep the abort controller pattern
6. Keep phase history accumulation in `phaseHistoryRef`
7. Keep reasoning duration calculation (`reasoningStartTime` ref)
8. Add `confirm_before_schedule: true` as default in `sendMessage`

- [ ] **Step 2: Refactor providers.tsx**

Read current `jarvis-frontend/lib/providers.tsx`. Remove `ModeContext` and `ModeProvider` entirely.

Keep `ThemeContext` and `ThemeProvider` — but verify it matches the Demo's pattern:
- Reads from `localStorage.getItem('jarvis-theme')`
- Sets `document.documentElement.setAttribute('data-theme', theme)` on mount and on change
- Provides `{ theme, setTheme, toggleTheme }`

Add `ConversationContext`:
```typescript
interface ConversationContextType {
  conversationId: string | null;
  setConversationId: (id: string | null) => void;
  startNewConversation: () => void;
}

const ConversationContext = createContext<ConversationContextType>({
  conversationId: null,
  setConversationId: () => {},
  startNewConversation: () => {},
});

export function ConversationProvider({ children }: { children: React.ReactNode }) {
  const [conversationId, setConversationId] = useState<string | null>(null);

  useEffect(() => {
    const stored = localStorage.getItem('jarvis-conversation-id');
    if (stored) setConversationId(stored);
  }, []);

  useEffect(() => {
    if (conversationId) {
      localStorage.setItem('jarvis-conversation-id', conversationId);
    }
  }, [conversationId]);

  const startNewConversation = () => {
    setConversationId(null);
    localStorage.removeItem('jarvis-conversation-id');
  };

  return (
    <ConversationContext.Provider value={{ conversationId, setConversationId, startNewConversation }}>
      {children}
    </ConversationContext.Provider>
  );
}

export const useConversation = () => useContext(ConversationContext);
```

Remove `ModeProvider` from the exported `Providers` wrapper component. Keep `ThemeProvider`.

- [ ] **Step 3: Update app/(app)/layout.tsx**

Read current `jarvis-frontend/app/(app)/layout.tsx`. Wrap children with `ConversationProvider`:

```typescript
import { ConversationProvider } from '@/lib/providers';

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <ConversationProvider>
      <div className="flex h-screen">
        <NavRail />
        <main className="flex-1 overflow-auto">{children}</main>
        <AIChatPanel />
      </div>
    </ConversationProvider>
  );
}
```

- [ ] **Step 4: Fix theme script in app/layout.tsx**

Read `jarvis-frontend/app/layout.tsx`. The inline `<script>` should ONLY prevent flash:

```typescript
const themeScript = `
  (function() {
    try {
      var t = localStorage.getItem('jarvis-theme');
      if (t) document.documentElement.setAttribute('data-theme', t);
      else if (window.matchMedia('(prefers-color-scheme: dark)').matches)
        document.documentElement.setAttribute('data-theme', 'dark');
    } catch(e) {}
  })();
`;
```

Ensure the `<script dangerouslySetInnerHTML={{ __html: themeScript }} />` is in the `<head>` tag.

Remove any `ModeProvider` imports from the root layout if present.

- [ ] **Step 5: Verify compilation**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit --pretty 2>&1 | head -30`

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/hooks/useJarvisChat.ts lib/providers.tsx app/layout.tsx "app/(app)/layout.tsx"
git commit -m "feat: port useJarvisChat from Demo, fix providers, add ConversationContext"
```

---

### Task 4: Port Response Components

**Files:**
- Create: `jarvis-frontend/components/app/JarvisResponse.tsx`
- Create: `jarvis-frontend/components/app/PhaseProgress.tsx`
- Create: `jarvis-frontend/components/app/IntentBadge.tsx`
- Create: `jarvis-frontend/components/app/MetricsBar.tsx`
- Create: `jarvis-frontend/components/app/ClarificationChips.tsx`
- Modify: `jarvis-frontend/components/app/ThinkingProcess.tsx`
- Modify: `jarvis-frontend/components/app/PipelineTrace.tsx`

- [ ] **Step 1: Create PhaseProgress.tsx**

New component that displays streaming phases with fun names and timing.

```typescript
'use client';
import { getPhaseDisplayName } from '@/lib/constants';
import { PhaseEventData } from '@/lib/types';
import { Check } from 'lucide-react';

interface PhaseProgressProps {
  phases: PhaseEventData[];
  currentPhase?: string;
  isStreaming: boolean;
}

export function PhaseProgress({ phases, currentPhase, isStreaming }: PhaseProgressProps) {
  if (!phases.length && !currentPhase) return null;

  return (
    <div className="space-y-1 text-sm mb-3">
      {phases.map((p, i) => (
        <div key={i} className="flex items-center gap-2 text-[var(--color-text-muted)]">
          <Check className="w-3.5 h-3.5 text-[var(--color-sage)]" />
          <span>{getPhaseDisplayName(p.phase)}</span>
          {p.duration_ms && (
            <span className="text-xs opacity-60">{p.duration_ms}ms</span>
          )}
        </div>
      ))}
      {isStreaming && currentPhase && (
        <div className="flex items-center gap-2 text-[var(--color-terra)]">
          <span className="w-3.5 h-3.5 rounded-full bg-[var(--color-terra)] animate-pulse" />
          <span>{getPhaseDisplayName(currentPhase)}</span>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Create IntentBadge.tsx**

```typescript
'use client';
import { getIntentColor } from '@/lib/constants';

interface IntentBadgeProps {
  intent: string;
}

export function IntentBadge({ intent }: IntentBadgeProps) {
  const color = getIntentColor(intent);
  const label = intent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());

  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-pill text-xs font-medium`}
      style={{
        backgroundColor: `var(--color-${color}, var(--color-ink))`,
        color: 'white',
        opacity: 0.9,
      }}
    >
      {label}
    </span>
  );
}
```

- [ ] **Step 3: Create MetricsBar.tsx**

```typescript
'use client';
import { GenerationMetrics } from '@/lib/types';
import { useState, useEffect } from 'react';

interface MetricsBarProps {
  metrics: GenerationMetrics;
}

export function MetricsBar({ metrics }: MetricsBarProps) {
  const [show, setShow] = useState(true);

  useEffect(() => {
    const stored = localStorage.getItem('jarvis-show-metrics');
    if (stored === 'false') setShow(false);
  }, []);

  if (!show) return null;

  return (
    <div className="flex items-center gap-3 text-xs text-[var(--color-text-muted)] font-mono mt-2 pt-2 border-t border-[var(--color-border)]">
      {metrics.ttft_ms != null && (
        <span>TTFT: {(metrics.ttft_ms / 1000).toFixed(1)}s</span>
      )}
      {metrics.tok_per_sec > 0 && (
        <span>{metrics.tok_per_sec.toFixed(1)} tok/s</span>
      )}
      {metrics.model && <span>{metrics.model}</span>}
      {metrics.total_time_s > 0 && (
        <span>Total: {metrics.total_time_s.toFixed(1)}s</span>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Create ClarificationChips.tsx**

```typescript
'use client';

interface ClarificationChipsProps {
  options: string[];
  onSelect: (option: string) => void;
}

export function ClarificationChips({ options, onSelect }: ClarificationChipsProps) {
  return (
    <div className="flex flex-wrap gap-2 mt-3">
      {options.map((option, i) => (
        <button
          key={i}
          onClick={() => onSelect(option)}
          className="px-3 py-1.5 rounded-pill text-sm border border-[var(--color-terra)] text-[var(--color-terra)] hover:bg-[var(--color-terra)] hover:text-white transition-colors"
        >
          {option}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Update ThinkingProcess.tsx**

Read current `jarvis-frontend/components/app/ThinkingProcess.tsx`. Verify it matches this pattern (port from Demo if needed):

- Collapsible section with chevron toggle
- Shows reasoning duration if available: "Thought for X.Xs"
- Renders thinking content as preformatted text
- Default: collapsed

- [ ] **Step 6: Create inline sub-components**

Create these small components that JarvisResponse will import:

**`components/app/InlineHabitStaging.tsx`** — When habits are detected in user's message, show:
```
New habit detected: "No work before 11 AM"
[Save as Constraint] [Ignore]
```
Props: `{ habit: string; onSave: () => void; onIgnore: () => void; }`

**`components/app/InfeasibleGuidance.tsx`** — When `schedule_status === 'INFEASIBLE'`, show anti-guilt guidance:
```
"Your goals don't fit the available time. Here's what I'd suggest:"
• Reduce scope — drop lowest-priority tasks
• Extend deadline — give yourself more runway
• Increase daily cap — schedule more hours
```
Props: `{ message: string; }`

**`components/app/ReplanBanner.tsx`** — When `suggested_action === 'replan'`:
```
Schedule may be outdated. [Replan →]
```
Props: `{ onReplan: () => void; }`

**`components/app/ActionProposalCards.tsx`** — Renders `action_proposals` array as cards with CTAs:
Each card: title, summary, suggested_actions as buttons.
Props: `{ proposals: ActionItemProposal[]; onAction: (proposal: ActionItemProposal, action: string) => void; }`

**`components/app/PendingCalendarApproval.tsx`** — When `CALENDAR_SYNC` intent returns pending entries:
Shows extracted timetable + [Approve] [Reject] buttons.
Props: `{ entries: any[]; onApprove: (id: string) => void; onReject: (id: string) => void; }`

- [ ] **Step 7: Port JarvisResponse.tsx from Demo**

Read `Jarvis-Demo/components/JarvisResponse.tsx` (659 lines). Port to `jarvis-frontend/components/app/JarvisResponse.tsx` with these changes:

1. Import from `@/lib/types`, `@/lib/constants`, `@/components/app/PhaseProgress`, etc.
2. Use the new sub-components: `PhaseProgress`, `IntentBadge`, `MetricsBar`, `ClarificationChips`, `ThinkingProcess`, `InlineHabitStaging`, `InfeasibleGuidance`, `ReplanBanner`, `ActionProposalCards`, `PendingCalendarApproval`
3. Keep the response section rendering pattern from Demo (phase indicator, intent badge, thinking, message, task decomposition, schedule section, clarification options, metrics)
4. Add new sections after the Demo sections: inline habit staging (if habits detected), action proposals (if present), replan banner (if `suggested_action`), infeasible guidance (if INFEASIBLE), pending calendar (if calendar entries)
5. Use `react-markdown` with `remark-gfm` for message rendering (already a dependency)
6. Replace Demo's emerald color references with terra/sage/dusk palette
7. Add streaming cursor `▌` during message streaming

The JarvisResponse component receives a single `JarvisMessage` and renders all sections conditionally. It is the primary response display for the chat page.

- [ ] **Step 8: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/JarvisResponse.tsx components/app/PhaseProgress.tsx components/app/IntentBadge.tsx components/app/MetricsBar.tsx components/app/ClarificationChips.tsx components/app/ThinkingProcess.tsx components/app/InlineHabitStaging.tsx components/app/InfeasibleGuidance.tsx components/app/ReplanBanner.tsx components/app/ActionProposalCards.tsx components/app/PendingCalendarApproval.tsx
git commit -m "feat: port response components — phases, intent, metrics, clarifications, thinking, habits, actions, replan"
```

---

### Task 5: Port Chat Session Panel and Rebuild Chat Page

**Files:**
- Create: `jarvis-frontend/components/app/ChatSessionPanel.tsx`
- Rewrite: `jarvis-frontend/app/(app)/chat/page.tsx`

- [ ] **Step 1: Create ChatSessionPanel.tsx**

Read `Jarvis-Demo/components/SessionSidebar.tsx` (234 lines). Port to `jarvis-frontend/components/app/ChatSessionPanel.tsx` with these changes:

1. Import from `@/lib/api` (listSessions, archiveSession, loadConversation)
2. Import from `@/lib/providers` (useConversation)
3. Replace emerald color scheme with terra palette
4. Replace Demo's layout classes with the spec's 260px collapsible panel
5. Keep: "New Chat" button, session list with relative timestamps, archive on hover, mobile drawer
6. On session click: call `loadConversation(sessionId)` from api.ts, set `conversationId` via context
7. On "New Chat": call `startNewConversation()` from context, clear messages in parent

The panel is collapsible via a hamburger icon. It renders as a left column on the chat page only.

- [ ] **Step 2: Rewrite chat/page.tsx**

Read current `jarvis-frontend/app/(app)/chat/page.tsx` (275 lines). Rewrite as 3-column layout:

```
┌──────────────┬─────────────────────────────┐
│ SessionPanel │      Chat Messages          │
│   (~260px)   │      (flex-1)               │
│  collapsible │                             │
└──────────────┴─────────────────────────────┘
```

(The AI Panel is already added by the app layout — it's the third column.)

Key implementation:
1. Use `useJarvisChat()` hook for all state
2. Render `ChatSessionPanel` on the left
3. Top bar: "Chat" title + "New Chat" button + `ModelModeSelector`
4. Messages area: map `messages` → `JarvisResponse` for assistant, styled bubbles for user
5. Empty state: `PromptSelector` with 4 starter prompts (reuse existing component)
6. Input area: textarea with paperclip (file upload), send button, enter-to-send
7. File upload: hidden `<input type="file" accept=".pdf,.png,.jpg,.jpeg">`, convert to base64 on select
8. When `JarvisResponse` renders clarification chips → `onSelect` sends that text as next message
9. When draft exists → render `DraftReview` component (Task 7)
10. Auto-scroll to bottom on new messages

- [ ] **Step 3: Verify chat page renders**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npm run dev`

Open `http://localhost:3000/chat`. Verify:
- Session panel shows on left (may be empty if no sessions yet)
- Chat input area renders at bottom
- Typing a message and pressing enter sends to backend via SSE
- If backend is running: phases display with fun names, response streams in
- If backend is not running: error toast appears

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/ChatSessionPanel.tsx "app/(app)/chat/page.tsx"
git commit -m "feat: rebuild chat page with session panel, SSE streaming, response rendering"
```

---

### Task 6: Fix NavRail Theme Toggle

**Files:**
- Modify: `jarvis-frontend/components/app/NavRail.tsx`

- [ ] **Step 1: Fix theme toggle in NavRail**

Read current `jarvis-frontend/components/app/NavRail.tsx`. The theme toggle is in the user menu popup. Ensure it:

1. Imports `useTheme` from the correct location (either `@/lib/providers` or `@/lib/hooks/useTheme`)
2. Calls `toggleTheme()` on click
3. Shows Sun icon for dark mode, Moon icon for light mode
4. Remove any `ModeToggle` or demo/live toggle from the menu

Also verify the `NavRail` routes:
- `/dashboard` — Home icon
- `/chat` — MessageSquare icon
- `/schedule` — Calendar icon
- `/workspace` — BookOpen icon (this won't work without a taskId, so link to `/dashboard` for now)
- `/documents` — FileText icon
- `/habits` — Target icon

Remove or disable nav items for pages that don't exist yet (Architecture, Analytics).

- [ ] **Step 2: Verify theme toggle works**

Run dev server. Click user avatar → Toggle theme. Verify:
- Page switches between light/dark
- Colors change correctly
- Persists on page reload

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/NavRail.tsx
git commit -m "fix: theme toggle wiring, remove demo/live toggle, clean nav items"
```

---

## Phase 2: Redesign App UX (MUST for pitch)

### Task 7: Draft Review Components

**Files:**
- Create: `jarvis-frontend/components/app/DraftReview.tsx`
- Create: `jarvis-frontend/components/app/TaskPreview.tsx`
- Create: `jarvis-frontend/components/app/SchedulePreview.tsx`
- Create: `jarvis-frontend/components/app/SM2QualityRating.tsx`

- [ ] **Step 1: Create TaskPreview.tsx (Stage 1)**

Renders editable task cards from `ExecutionGraph.decomposition`. Each card shows:
- Title (editable input)
- Duration (editable number input, ≤25)
- Difficulty bar (visual 0-1 range)
- Completion criteria (text)
- WOOP implementation intention if present ("If: ... → Then: ...")
- [Remove] button per task

Props:
```typescript
interface TaskPreviewProps {
  tasks: TaskChunk[];
  cognitiveLoad?: { intrinsic_load: number; germane_load: number };
  onConfirm: (editedTasks: TaskChunk[]) => void;
  onChatModify: () => void;
}
```

- [ ] **Step 2: Create SchedulePreview.tsx (Stage 2)**

Renders the schedule timeline from `ScheduleResponse`. Shows:
- Draft ID at top
- Time blocks computed as `new Date(horizonStart).getTime() + startMin * 60000`
- Color-coded: task blocks (terra), breaks (transparent), blocked windows (gray/hatched)
- Per-task: title, duration, time range
- Actions: [Accept All] [Reject] [Rearrange] (rearrange is stretch — just accept/reject for pitch)

Props:
```typescript
interface SchedulePreviewProps {
  schedule: ScheduleResponse;
  draftId: string;
  onAccept: () => void;
  onReject: (reason?: string) => void;
}
```

- [ ] **Step 3: Create DraftReview.tsx (wrapper)**

Wrapper that shows Stage 1 or Stage 2 based on state:
- If `awaitingTaskConfirmation` is true and `executionGraph` exists → render `TaskPreview`
- If `scheduleStatus === 'draft'` and `schedule` exists → render `SchedulePreview`
- On TaskPreview confirm → calls `confirmTasks(editedTasks)` from useJarvisChat
- On SchedulePreview accept → calls `acceptDraft()` from useJarvisChat
- On reject → prompts for reason, calls `rejectDraft()`

- [ ] **Step 4: Create SM2QualityRating.tsx**

Simple 0-5 button strip for rating task completion:

```typescript
const QUALITY_LABELS = ['Blackout', 'Wrong', 'Hard', 'Struggled', 'Good', 'Perfect'];

interface SM2QualityRatingProps {
  onRate: (quality: number) => void;
}

export function SM2QualityRating({ onRate }: SM2QualityRatingProps) {
  return (
    <div className="flex gap-1.5">
      {QUALITY_LABELS.map((label, i) => (
        <button
          key={i}
          onClick={() => onRate(i)}
          className="px-2 py-1 text-xs rounded-button border border-[var(--color-border)] hover:bg-[var(--color-terra)] hover:text-white hover:border-[var(--color-terra)] transition-colors"
          title={label}
        >
          {i}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Integrate DraftReview into chat page**

In `app/(app)/chat/page.tsx`, when the latest assistant message has `response.awaiting_task_confirmation` or `response.schedule_status === 'draft'`, render `DraftReview` below the message.

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/DraftReview.tsx components/app/TaskPreview.tsx components/app/SchedulePreview.tsx components/app/SM2QualityRating.tsx "app/(app)/chat/page.tsx"
git commit -m "feat: two-stage draft review — task preview + schedule preview"
```

---

### Task 8: Dashboard Page

**Files:**
- Rewrite: `jarvis-frontend/app/(app)/dashboard/page.tsx`
- Modify: `jarvis-frontend/components/app/DailyGreeting.tsx`
- Modify: `jarvis-frontend/components/app/StatsStrip.tsx`
- Modify: `jarvis-frontend/components/app/ScheduleTimeline.tsx`

- [ ] **Step 1: Rewrite dashboard/page.tsx**

Read the current dashboard page. Rewrite to fetch real data:

1. On mount: `listTasks()` from api.ts to get user's tasks
2. Compute stats from task data: completed count, total focus minutes, streak
3. Check for active draft: look in localStorage for `jarvis-active-draft`
4. Display PEARL insights: cached from latest chat response in localStorage

Layout (from spec):
- DailyGreeting header
- StatsStrip (4 cards: Tasks, Focus, Streak, Patterns Learned)
- Active draft banner (if draft exists) → links to `/chat`
- PEARL insight card ("Jarvis noticed: ...")
- ScheduleTimeline (today's tasks with NOW indicator)
- Task cards: click → navigates to `/workspace/{taskId}`

If `listTasks()` fails (backend not running): show demo data as fallback.

- [ ] **Step 2: Update DailyGreeting.tsx**

Read current component. Update to accept real task count and focus minutes as props instead of hardcoded demo data.

- [ ] **Step 3: Update StatsStrip.tsx**

Add 4th card: "Patterns Learned" with count + tooltip explaining what it means.
Accept all values as props.

- [ ] **Step 4: Update ScheduleTimeline.tsx**

Accept tasks from API response. Compute wall times from `horizon_start + start_min`. Show NOW indicator based on current time.

On task click: `router.push(\`/workspace/${task.task_id}\`)`.

- [ ] **Step 5: Verify dashboard renders with backend**

Start backend: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && uvicorn app.main:app --reload --port 8000`
Open `http://localhost:3000/dashboard`.

Verify: tasks load, stats compute, timeline renders.

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add "app/(app)/dashboard/page.tsx" components/app/DailyGreeting.tsx components/app/StatsStrip.tsx components/app/ScheduleTimeline.tsx
git commit -m "feat: dashboard with real data — stats, timeline, PEARL insights, draft banner"
```

---

### Task 9: Memory Panel

**Files:**
- Create: `jarvis-frontend/components/app/MemoryPanel.tsx`

- [ ] **Step 1: Create MemoryPanel.tsx**

Type-agnostic memory display that groups by `memory_type`. Slides in from right (over AI Panel) when triggered by brain icon.

Implementation:
1. Accept `memories: MemoryRecord[]` as prop (from chat responses)
2. Group by `memory_type` dynamically: `Object.groupBy(memories, m => m.memory_type)`
3. Sort groups by importance weight (hardcoded map, fallback to 0.5 for unknown types)
4. Per memory: content, confidence bar, source badge, delete button
5. For `behavioral_pattern` type: show "Jarvis noticed: {content}" format + [Confirm] [Dismiss] buttons
6. Superseded memories (has `superseded_by`): struck-through with "Updated →" text
7. Empty state: "No memories yet. Chat with Jarvis to build up context."

Color per type (from spec's `MEMORY_TYPE_COLORS` — add to constants.ts):
```typescript
const MEMORY_TYPE_COLORS: Record<string, string> = {
  constraint: 'terra',
  behavioral_pattern: 'sage',
  preference: 'dusk',
  temporal_event: 'gold',
  goal: 'sage',
  fact: 'ink',
  feedback: 'ink',
};
```

Use dynamic border-left color per group.

- [ ] **Step 2: Add brain icon trigger to chat page header**

In `app/(app)/chat/page.tsx`, add a Brain (lucide) icon button in the top bar that toggles `showMemoryPanel` state. When true, render `<MemoryPanel />` as an absolute/fixed overlay.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/MemoryPanel.tsx "app/(app)/chat/page.tsx"
git commit -m "feat: memory panel — type-agnostic grouped display with confidence + deletion"
```

---

### Task 10: Functional AI Chat Panel

**Files:**
- Rewrite: `jarvis-frontend/components/app/AIChatPanel.tsx`

- [ ] **Step 1: Rewrite AIChatPanel.tsx**

Read current `jarvis-frontend/components/app/AIChatPanel.tsx` (107 lines). Rewrite to be functional:

1. Use `useJarvisChat()` hook with `modelMode: '4b'` for fast responses
2. Share `conversationId` via `useConversation()` context
3. Show last 3-5 messages (condensed — no thinking process, no phase trace)
4. Real PEARL insight at top (from latest chat response's `pearl_insights` or cached)
5. Input area: text input + send button
6. "Continue in Chat →" button on responses → `router.push('/chat')`
7. Auto-detect PLAN_DAY intent → show "This needs the full workspace — open in Chat?" prompt
8. Collapsed state: floating "J" button at right edge, tooltip "Open Jarvis (Cmd+J)"
9. Toggle: `Cmd+J` keyboard shortcut (already implemented in app layout)

- [ ] **Step 2: Verify panel works**

Run dev server. Open any page. Press Cmd+J to toggle panel. Type a message. Verify response streams in with 4B model mode.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/AIChatPanel.tsx
git commit -m "feat: functional AI panel with 4B mode, shared context, continue-in-chat"
```

---

### Task 11: Workspace Page

**Files:**
- Create: `jarvis-frontend/app/(app)/workspace/[taskId]/page.tsx`

- [ ] **Step 1: Create workspace page**

New page at `app/(app)/workspace/[taskId]/page.tsx`.

On mount: call `getWorkspace(taskId)` from api.ts.

Layout (from spec):
1. Header: task title + back button + progress bar (criteria completion %)
2. Completion Criteria: checkboxes from workspace data. Mark complete → track locally, on all done → show SM2QualityRating → call `completeTask(taskId, quality)`
3. Study Materials: render `surfaced_assets` as cards grouped by type:
   - `pdf_chunk` → excerpt card with content
   - `youtube_link` → embedded link with play icon
   - `article_link` / `blog_link` → link card
   - `generated_quiz` → expandable quiz content
4. WOOP section: if task has `implementation_intention` → "IF: {obstacle} → THEN: {response}"
5. Mini-chat at bottom: input → sends message with `[Context: Working on task "${taskTitle}"]` prefix

Loading state: skeleton loaders. Error state: "Couldn't load workspace. Is the backend running?"

- [ ] **Step 2: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add "app/(app)/workspace/[taskId]/page.tsx"
git commit -m "feat: workspace page — criteria, materials, WOOP, mini-chat"
```

---

### Task 12: Supporting Pages (Documents, Habits, Schedule)

**Files:**
- Create: `jarvis-frontend/app/(app)/documents/page.tsx`
- Create: `jarvis-frontend/app/(app)/habits/page.tsx`
- Create: `jarvis-frontend/app/(app)/schedule/page.tsx`

- [ ] **Step 1: Create documents/page.tsx**

Simple document management page:
1. On mount: `listDocuments()` from api.ts
2. Upload zone: drag-drop area, accepts PDF/PNG/JPEG
3. On upload: convert to base64, call `processDocument(base64, mediaType)`
4. Document list: filename, type badge (classification), topics, delete button
5. Delete: confirmation dialog → `deleteDocument(id)`

- [ ] **Step 2: Create habits/page.tsx**

Habits tracker page:
1. On mount: `getDueHabits()` from api.ts
2. Due habits list: name, last done, days since, next interval
3. Complete button → shows SM2QualityRating → calls `completeHabit(id, quality)`
4. Empty state: "No habits due. They'll appear here when Jarvis detects patterns."

- [ ] **Step 3: Create schedule/page.tsx**

Simple day view:
1. On mount: `listTasks()` from api.ts
2. Show today's tasks in time blocks (same as dashboard timeline but full-page)
3. Click task → workspace

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add "app/(app)/documents/page.tsx" "app/(app)/habits/page.tsx" "app/(app)/schedule/page.tsx"
git commit -m "feat: documents, habits, and schedule pages"
```

---

## Phase 3: Landing Page Enhancement (STRETCH — if time permits)

### Task 13: GSAP ScrollTrigger "How It Works" Section

**Files:**
- Create: `jarvis-frontend/components/landing/HowItWorks.tsx`
- Modify: `jarvis-frontend/app/page.tsx`

- [ ] **Step 1: Create HowItWorks.tsx**

GSAP ScrollTrigger pinned section. 5-step pipeline that animates left-to-right as user scrolls:

1. Brain Dump (terra) → 2. Understand (dusk) → 3. Break Down (sage) → 4. Schedule (gold) → 5. Workspace (terra)

Each step fades in with stagger, connection lines animate between steps. Uses `@gsap/react` (already installed).

Implementation pattern:
```typescript
'use client';
import { useRef, useEffect } from 'react';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

gsap.registerPlugin(ScrollTrigger);

const STEPS = [
  { label: 'Brain Dump', desc: '"Plan my DL contest"', color: 'terra' },
  { label: 'Understand', desc: 'Intent Classification', color: 'dusk' },
  { label: 'Break Down', desc: 'Socratic Chunker', color: 'sage' },
  { label: 'Schedule', desc: 'OR-Tools Solver', color: 'gold' },
  { label: 'Workspace', desc: 'RAG + Practice', color: 'terra' },
];
```

Pin the section while scrolling, reveal each step sequentially.

- [ ] **Step 2: Add to landing page**

In `app/page.tsx`, add `<HowItWorks />` between Hero and FeatureBento sections.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/landing/HowItWorks.tsx app/page.tsx
git commit -m "feat: GSAP scroll-driven How It Works pipeline section"
```

---

### Task 14: Scroll-Reactive Logo and Interactive Demo

**Files:**
- Modify: `jarvis-frontend/components/landing/LandingNav.tsx`
- Create: `jarvis-frontend/components/landing/InteractiveDemo.tsx`
- Create: `jarvis-frontend/components/landing/TechCredibility.tsx`

- [ ] **Step 1: Add scroll-reactive logo to LandingNav**

In `LandingNav.tsx`, use a scroll listener:
- Scroll down → "J" morphs to "Jarvis" (opacity + width transition)
- Scroll up → "Jarvis" collapses back to "J"

Use CSS transitions (simpler than GSAP for this):
```typescript
const [scrollDir, setScrollDir] = useState<'up' | 'down'>('up');
// Track scroll direction via lastScrollY ref
```

- [ ] **Step 2: Create InteractiveDemo.tsx**

Embedded mock chat on the landing page:
- 3 preset prompts as clickable cards
- On click: simulated response plays using demoData.ts
- Shows phase progress + streaming text + task decomposition
- CTA: "Try it for real →" links to `/dashboard`

- [ ] **Step 3: Create TechCredibility.tsx**

Simple strip below features:
```
Local-first on Apple Silicon | OR-Tools deterministic scheduler |
SM-2 spaced repetition | Privacy-preserving | 143 tests passing
```

- [ ] **Step 4: Integrate into landing page**

Add `InteractiveDemo` after FeatureBento, `TechCredibility` before Pricing.

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/landing/LandingNav.tsx components/landing/InteractiveDemo.tsx components/landing/TechCredibility.tsx app/page.tsx
git commit -m "feat: scroll-reactive logo, interactive demo, tech credibility strip"
```

---

## Post-Plan Checklist

After all tasks are complete, verify:

- [ ] Chat page: SSE streaming works end-to-end with real backend
- [ ] Chat page: phases display with fun names and timing
- [ ] Chat page: thinking process renders in collapsible section
- [ ] Chat page: TTFT and tok/sec display (toggleable)
- [ ] Chat page: intent badge shows correct color
- [ ] Chat page: session panel lists conversations, click loads history
- [ ] Chat page: file upload works (PDF)
- [ ] Chat page: draft review flows (task preview → schedule preview → accept)
- [ ] Chat page: clarification chips render and work
- [ ] Chat page: memory panel shows memories grouped by type
- [ ] Dashboard: loads real tasks from backend
- [ ] Dashboard: PEARL insight card displays
- [ ] Dashboard: active draft banner links to chat
- [ ] Theme toggle: switches light/dark, persists on reload
- [ ] AI Panel: functional with 4B mode, shared conversation context
- [ ] Workspace: loads task materials, completion criteria, WOOP
- [ ] Documents page: upload and list documents
- [ ] Habits page: shows due habits with SM-2 completion
- [ ] Landing page: existing brain orb + features render correctly
- [ ] (Stretch) Landing page: GSAP How It Works section animates on scroll
- [ ] (Stretch) Landing page: interactive demo works with canned responses
