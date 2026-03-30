# WS2: Frontend Chat UI Fixes Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all chat UI bugs: ThinkingProcess uses fun phase names, calendar approval handlers wired, model mode persisted, model_mode sent in confirmScheduleStream, math rendering verified.

**Architecture:** Small targeted fixes to existing components. No new components needed — just wiring gaps and display logic corrections.

**Tech Stack:** Next.js 14, TypeScript, React, Tailwind CSS, react-markdown, remark-math, rehype-katex

**Spec:** `docs/superpowers/specs/2026-03-30-jarvis-spec-compliance-fix-design.md` (Workstream 2)

**Frontend root:** `/Users/madhav/Jarvis-cursor/jarvis-frontend`

---

### Task 1: Fix ThinkingProcess to use fun phase names

**Files:**
- Modify: `components/app/ThinkingProcess.tsx`

- [ ] **Step 1: Update ThinkingProcess to import and use getPhaseDisplayName**

Replace the content of `components/app/ThinkingProcess.tsx`. The key change: instead of hardcoded "Thinking" and "Thought for Xs", derive the display name from the latest phase in `phaseHistory`.

```typescript
"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getPhaseDisplayName } from "@/lib/constants";
import type { PhaseEventData } from "@/lib/types";

interface ThinkingProcessProps {
  reasoning: string;
  isStreaming: boolean;
  durationMs?: number | null;
  phaseHistory?: PhaseEventData[];
}

export function ThinkingProcess({
  reasoning,
  isStreaming,
  durationMs,
  phaseHistory,
}: ThinkingProcessProps) {
  const [expanded, setExpanded] = useState(false);

  if (!reasoning && !isStreaming) return null;

  // Derive display duration from phaseHistory or prop
  let displayDuration: string | null = null;
  if (durationMs && durationMs > 0) {
    displayDuration =
      durationMs >= 1000
        ? `${(durationMs / 1000).toFixed(1)}s`
        : `${Math.round(durationMs)}ms`;
  } else if (phaseHistory && phaseHistory.length >= 2) {
    const first = phaseHistory[0];
    const last = phaseHistory[phaseHistory.length - 1];
    if (first.timestamp && last.timestamp) {
      const delta = last.timestamp - first.timestamp;
      displayDuration =
        delta >= 1000
          ? `${(delta / 1000).toFixed(1)}s`
          : `${Math.round(delta)}ms`;
    }
  }

  // Get the current phase display name from the latest phase event
  const currentPhase =
    phaseHistory && phaseHistory.length > 0
      ? phaseHistory[phaseHistory.length - 1].phase
      : "reasoning";
  const phaseName = getPhaseDisplayName(currentPhase);

  return (
    <div className="my-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <span className="select-none">{expanded ? "▾" : "▸"}</span>
        {isStreaming ? (
          <span className="inline-flex items-center gap-1">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-terra opacity-75" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-terra" />
            </span>
            {phaseName}
          </span>
        ) : (
          <span>
            Thought{displayDuration ? ` for ${displayDuration}` : ""}
          </span>
        )}
      </button>
      {expanded && reasoning && (
        <div className="mt-2 pl-4 border-l-2 border-muted text-xs text-muted-foreground prose prose-sm dark:prose-invert max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {reasoning}
          </ReactMarkdown>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify the component compiles**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit 2>&1 | head -20`
Expected: No errors related to ThinkingProcess

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/ThinkingProcess.tsx
git commit -m "feat: ThinkingProcess uses fun phase names from constants

Shows 'Brewing your plan...' while streaming instead of generic 'Thinking'.
Phase name derived from latest phaseHistory entry via getPhaseDisplayName()."
```

---

### Task 2: Wire calendar approval handlers in chat page

**Files:**
- Modify: `app/(app)/chat/page.tsx`

- [ ] **Step 1: Import calendar API functions and pass handlers**

In `app/(app)/chat/page.tsx`, add the import and wire the missing props:

At the top imports, add:
```typescript
import { approveCalendar, rejectCalendar } from "@/lib/api";
```

Find where `<JarvisResponse` is rendered (around line 255) and add the missing props:

```typescript
<JarvisResponse
  message={msg}
  onClarificationSelect={(text) => sendMessage(text)}
  onReplan={() => triggerReplan()}
  isReplanning={isReplanning}
  onConfirmTasks={(tasks) => confirmTasks(tasks)}
  onAcceptDraft={() => acceptDraft()}
  onRejectDraft={() => rejectDraft()}
  onChatModify={handleChatModify}
  onCalendarApproved={(id) => approveCalendar(id)}
  onCalendarRejected={(id) => rejectCalendar(id)}
/>
```

- [ ] **Step 2: Verify no TypeScript errors**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit 2>&1 | grep -i "calendar\|chat/page" | head -10`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add app/\(app\)/chat/page.tsx
git commit -m "feat: wire calendar approval/rejection handlers in chat page

Calendar approval buttons in PendingCalendarApproval component now functional.
Calls POST /api/v1/ingestion/pending-calendar/{id}/approve and /reject."
```

---

### Task 3: Persist model mode to localStorage

**Files:**
- Modify: `lib/hooks/useJarvisChat.ts`

- [ ] **Step 1: Update modelMode state initialization and add persistence effect**

In `lib/hooks/useJarvisChat.ts`, find the modelMode state (line 92):

Replace:
```typescript
const [modelMode, setModelMode] = useState<ModelMode>("auto");
```

With:
```typescript
const [modelMode, setModelMode] = useState<ModelMode>(() => {
  if (typeof window === "undefined") return "auto";
  return (localStorage.getItem("jarvis-model-mode") as ModelMode) || "auto";
});
```

Add a `useEffect` right after the state declaration:
```typescript
useEffect(() => {
  localStorage.setItem("jarvis-model-mode", modelMode);
}, [modelMode]);
```

- [ ] **Step 2: Add model_mode to confirmScheduleStream request**

Find the `confirmScheduleStream` call (around lines 402-415). In the request body object, add `model_mode`:

```typescript
await confirmScheduleStream(
  {
    user_id: USER_ID,
    model_mode: modelMode,  // ADD THIS LINE
    tasks: editedTasks.map((t) => ({
      task_id: t.task_id,
      title: t.title,
      duration_minutes: t.duration_minutes,
      difficulty_weight: t.difficulty_weight,
      completion_criteria: t.completion_criteria,
      implementation_intention: t.implementation_intention,
      dependencies: [],
    })),
    goal_metadata: goalMetadata,
  },
  { /* ...stream handlers... */ }
);
```

- [ ] **Step 3: Add model_mode to ConfirmScheduleRequest type**

In `lib/types.ts`, find the `ConfirmScheduleRequest` interface and add:

```typescript
export interface ConfirmScheduleRequest {
  user_id: string;
  model_mode?: "auto" | "4b" | "27b";  // ADD THIS LINE
  tasks: Array<{
    // ... existing fields
  }>;
  goal_metadata?: Record<string, unknown>;
}
```

- [ ] **Step 4: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/hooks/useJarvisChat.ts lib/types.ts
git commit -m "feat: persist model mode to localStorage, send in confirmScheduleStream

Model preference survives page reload. Schedule confirmation
respects user's model choice instead of defaulting to 'auto'."
```

---

### Task 4: Verify math rendering works end-to-end

**Files:**
- Verify: `components/app/JarvisResponse.tsx`

- [ ] **Step 1: Verify KaTeX imports are present**

Read `components/app/JarvisResponse.tsx` and confirm these imports exist:
```typescript
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
```

And that the ReactMarkdown usage includes both plugins:
```typescript
<ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
```

- [ ] **Step 2: Check ThinkingProcess also supports math**

The ThinkingProcess component we updated in Task 1 only uses `remarkGfm`. If thinking blocks may contain math, add remark-math:

```typescript
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

// In the ReactMarkdown rendering:
<ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
  {reasoning}
</ReactMarkdown>
```

- [ ] **Step 3: Verify the dev server builds**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -10`
Expected: Build succeeds

- [ ] **Step 4: Commit if changes were needed**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/ThinkingProcess.tsx components/app/JarvisResponse.tsx
git commit -m "fix: ensure KaTeX math rendering in both JarvisResponse and ThinkingProcess

Both components now support inline ($...$) and block ($$...$$) LaTeX."
```

---

### Task 5: Final integration check for WS2

- [ ] **Step 1: Run full TypeScript check**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit`
Expected: Clean

- [ ] **Step 2: Run build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`
Expected: Build succeeds

- [ ] **Step 3: Summary commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add -A
git commit -m "chore: WS2 frontend chat UI fixes complete

- ThinkingProcess shows fun phase names (Brewing your plan...)
- Calendar approval handlers wired
- Model mode persisted to localStorage
- model_mode sent in confirmScheduleStream
- Math rendering verified (KaTeX in both response and thinking)"
```
