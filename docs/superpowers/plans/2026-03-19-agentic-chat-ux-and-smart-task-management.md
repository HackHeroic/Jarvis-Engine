# Agentic Chat UX & Smart Task Management

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a polished, multi-turn agentic chat experience where users can ask Jarvis anything, get task proposals they can edit inline or via chat, add habits that auto-recalibrate the schedule, and accept with a clean confirmation flow — inspired by ChatGPT, Claude, and Gemini's conversational patterns.

**Architecture:** Frontend wires `conversation_id` from the just-implemented backend chat sessions. A new session sidebar lists past conversations. `suggested_action: "replan"` triggers a prominent one-click replan flow. A new backend clarification intent detects ambiguous requests and returns quick-reply options. Accept/reject UX gets visual polish with transitions.

**Tech Stack:** Next.js 14 (App Router), React 18, TypeScript, Tailwind CSS, FastAPI backend, Supabase, existing SSE streaming infrastructure.

---

## Scope Note

This plan covers the **conversational UX layer** — everything the user sees and touches when interacting with Jarvis. **Google Calendar integration** (OAuth, push/pull events) is a separate plan because it's an independent subsystem with its own auth flow and external API dependencies.

---

## Current State

### What Already Works
- SSE streaming chat with thinking/message tokens (`useJarvisChat.ts`)
- Task decomposition with `awaiting_task_confirmation` flag → `TaskPreview` component
- Draft schedule with accept/suggest changes → `ScheduleSection` component
- `/confirm-schedule`, `/accept-schedule`, `/modify-schedule` backend endpoints
- Habit storage with `suggested_action: "replan"` in ChatResponse
- Multi-goal fusion: new goals merge with pending tasks automatically
- Backend chat sessions: `conversation_id` + `message_id` returned in all responses (migration 009)
- Session CRUD endpoints: `GET/PUT/DELETE /api/v1/sessions`

### What's Missing (This Plan Builds)
1. **Frontend doesn't send or track `conversation_id`** — every message is stateless from the frontend's perspective
2. **No session sidebar** — can't see past conversations or start new ones
3. **`suggested_action: "replan"` renders as a text banner** — no one-click replan, no auto-trigger
4. **No clarification flow** — Jarvis always tries to process even when the request is ambiguous
5. **Accept flow has no visual feedback** — no confirmation, no transition animation
6. **No conversation persistence** — page refresh loses all context

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `jarvis-demo/components/SessionSidebar.tsx` | Conversation list, new/switch/archive sessions |
| `jarvis-demo/components/ClarificationCard.tsx` | Quick-reply buttons for clarification prompts |
| `jarvis-demo/components/ReplanBanner.tsx` | Prominent one-click replan with context |

### Modified Files
| File | Changes |
|------|---------|
| `jarvis-demo/lib/api.ts:48-59,184-256` | Add `conversation_id` to `ChatRequest`, pass through `chatStream()` |
| `jarvis-demo/lib/useJarvisChat.ts:1-462` | Track `conversationId` state, extract from responses, send in requests, handle clarification + replan |
| `jarvis-demo/lib/jarvis-types.ts:38-85` | Add `conversation_id`, `message_id`, clarification types |
| `jarvis-demo/lib/scheduleStore.ts:1-87` | Persist `conversationId` with messages, key messages by session |
| `jarvis-demo/components/JarvisResponse.tsx:406-614` | Render clarification cards, replan banner, accept transition |
| `jarvis-demo/components/JarvisChatPanel.tsx:1-325` | Session sidebar layout, new conversation button |
| `jarvis-demo/components/ScheduleSection.tsx:479-503` | Accept flow polish with confirmation + feedback |
| `jarvis-demo/app/chat/page.tsx` | Layout with sidebar |
| `Jarvis-Engine/app/services/analytical/control_policy.py:355-369` | Clarification detection in brain dump extraction |
| `Jarvis-Engine/app/schemas/context.py:211-266` | Add `clarification_options` field to ChatResponse |

---

## Task 1: Wire conversation_id in Frontend API Layer

**Files:**
- Modify: `jarvis-demo/lib/api.ts:48-59` (ChatRequest type)
- Modify: `jarvis-demo/lib/api.ts:184-256` (chatStream function)
- Modify: `jarvis-demo/lib/jarvis-types.ts:75-85` (JarvisMessage type)

This is the foundation — without this, multi-turn doesn't work.

- [ ] **Step 1: Add conversation_id to ChatRequest type**

In `jarvis-demo/lib/api.ts`, update the `ChatRequest` interface (line 48):

```typescript
export interface ChatRequest {
  user_prompt: string;
  user_id: string;
  day_start_hour?: number;
  deadline_override?: string;
  file_base64?: string;
  media_type?: string;
  model_mode?: ModelMode;
  max_daily_deep_work_minutes?: number;
  min_daily_deep_work_minutes?: number;
  max_task_duration_minutes?: number;
  min_task_duration_minutes?: number;
  file_name?: string;
  confirm_before_schedule?: boolean;
  draft_schedule?: {
    schedule: Record<string, unknown>;
    execution_graph: Record<string, unknown>;
    horizon_start: string;
  };
  conversation_id?: string;  // NEW: session ID for multi-turn context
}
```

- [ ] **Step 2: Add conversation_id and message_id to JarvisMessage**

In `jarvis-demo/lib/jarvis-types.ts`, update the `JarvisMessage` interface (line 75):

```typescript
export interface JarvisMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  reasoningDurationMs?: number;
  phaseHistory?: PhaseEventData[];
  response?: ChatResponse;
  isStreaming?: boolean;
  fileName?: string;
  mediaType?: string;
  conversation_id?: string;   // NEW
  message_id?: string;        // NEW
}
```

- [ ] **Step 3: Add ChatResponse fields for conversation tracking**

In `jarvis-demo/lib/jarvis-types.ts`, add after the `JarvisMessage` interface:

```typescript
// Extend ChatResponse type awareness (backend already returns these)
// These are used by the hook to extract session info from complete events
export interface ChatResponseSessionFields {
  conversation_id?: string;
  message_id?: string;
  suggested_action?: string;
  clarification_options?: string[];
}
```

- [ ] **Step 4: Pass conversation_id through chatStream**

In `jarvis-demo/lib/api.ts`, the `chatStream()` function (line 184) already sends the full `ChatRequest` body. Since `conversation_id` is now part of the interface, it will be included automatically when the hook sets it. Verify the body construction at line 197:

```typescript
body: JSON.stringify(request),
```

This already sends all ChatRequest fields. No change needed in `chatStream()` itself.

- [ ] **Step 5: Verify by checking the request payload includes conversation_id**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`
Expected: No new type errors

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/api.ts lib/jarvis-types.ts
git commit -m "feat: add conversation_id to ChatRequest and JarvisMessage types"
```

---

## Task 2: Track conversation_id in useJarvisChat Hook

**Files:**
- Modify: `jarvis-demo/lib/useJarvisChat.ts:1-462`
- Modify: `jarvis-demo/lib/scheduleStore.ts:1-87`

The hook must: (1) store the current conversationId, (2) extract it from `complete` events, (3) send it in subsequent requests, (4) allow starting a new conversation.

- [ ] **Step 1: Add conversationId state to the hook**

In `jarvis-demo/lib/useJarvisChat.ts`, add after the existing state declarations (around line 40):

```typescript
const [conversationId, setConversationId] = useState<string | null>(() => {
  // Restore from localStorage if available
  if (typeof window !== "undefined") {
    return localStorage.getItem("jarvis-conversation-id");
  }
  return null;
});
```

- [ ] **Step 2: Extract conversation_id from complete events**

In the `onComplete` handler inside `sendMessage()` (around line 200), after `streamingMsg.current.response = response`:

```typescript
        // Track conversation session
        if (response.conversation_id) {
          setConversationId(response.conversation_id);
          localStorage.setItem("jarvis-conversation-id", response.conversation_id);
          // Tag the user message with conversation_id too
          setMessages((prev) => prev.map((m) =>
            m.id === userMsg.id
              ? { ...m, conversation_id: response.conversation_id }
              : m
          ));
          if (streamingMsg.current) {
            streamingMsg.current.conversation_id = response.conversation_id;
            streamingMsg.current.message_id = response.message_id;
          }
        }
```

- [ ] **Step 3: Send conversation_id in requests**

In `sendMessage()`, where the `chatStream` request is constructed (around line 100-120), add `conversation_id`:

```typescript
    const request: ChatRequest = {
      user_prompt: content,
      user_id: "demo",
      model_mode: options?.modelMode || "auto",
      confirm_before_schedule: true,
      conversation_id: conversationId || undefined,
      ...(options?.fileBase64 && {
        file_base64: options.fileBase64,
        media_type: options.mediaType,
      }),
      ...(options?.fileName && { file_name: options.fileName }),
    };
```

Also in `confirmTasks()` (around line 270), the `confirmScheduleStream` request should include `conversation_id` if the confirm endpoint supports it. For now, this is fine since confirm-schedule is a separate flow.

- [ ] **Step 4: Add startNewConversation function**

Add this function to the hook, after the existing `clearMessages` function:

```typescript
  const startNewConversation = useCallback(() => {
    setConversationId(null);
    localStorage.removeItem("jarvis-conversation-id");
    setMessages([]);
    clearChatMessages();
    setDraftScheduleResponse(null);
    clearDraftSchedule();
    setPendingTasks(null);
  }, []);
```

- [ ] **Step 5: Add loadConversation function**

```typescript
  const loadConversation = useCallback(async (sessionId: string) => {
    setConversationId(sessionId);
    localStorage.setItem("jarvis-conversation-id", sessionId);
    // Fetch messages from backend
    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const resp = await fetch(
        `${baseUrl}/api/v1/sessions/${sessionId}?user_id=demo`
      );
      if (resp.ok) {
        const data = await resp.json();
        const loaded: JarvisMessage[] = (data.messages || []).map(
          (m: { id: string; role: string; content: string; intent?: string; metadata?: Record<string, unknown> }, i: number) => ({
            id: m.id || `loaded-${i}`,
            role: m.role as "user" | "assistant",
            content: m.content,
            conversation_id: sessionId,
          })
        );
        setMessages(loaded);
        saveChatMessages(loaded);
      }
    } catch {
      // Graceful degradation — keep current messages
    }
  }, []);
```

- [ ] **Step 6: Export new functions and state from hook**

Update the hook's return statement to include:

```typescript
  return {
    messages,
    streamState,
    isStreaming,
    sendMessage,
    abort,
    pendingTasks,
    pendingGoalMetadata,
    confirmTasks,
    isConfirming,
    draftScheduleResponse,
    acceptDraft,
    rejectDraft,
    clearMessages,
    conversationId,           // NEW
    startNewConversation,     // NEW
    loadConversation,         // NEW
  };
```

- [ ] **Step 7: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 8: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/useJarvisChat.ts lib/scheduleStore.ts
git commit -m "feat: track conversation_id across messages for multi-turn context"
```

---

## Task 3: Session Sidebar Component

**Files:**
- Create: `jarvis-demo/components/SessionSidebar.tsx`
- Modify: `jarvis-demo/lib/api.ts` (add session API functions)
- Modify: `jarvis-demo/app/chat/page.tsx` (layout with sidebar)

- [ ] **Step 1: Add session API functions to api.ts**

In `jarvis-demo/lib/api.ts`, add after the existing API functions (bottom of file):

```typescript
// ── Session management ──────────────────────────────────────────────

export interface ChatSession {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  is_archived: boolean;
}

export async function listSessions(
  userId: string = "demo",
  limit: number = 30,
): Promise<ChatSession[]> {
  if (isDemoMode()) return [];
  const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  try {
    const resp = await fetch(
      `${baseUrl}/api/v1/sessions/?user_id=${userId}&limit=${limit}`,
    );
    if (!resp.ok) return [];
    const data = await resp.json();
    return data.sessions || [];
  } catch {
    return [];
  }
}

export async function archiveSession(
  sessionId: string,
  userId: string = "demo",
): Promise<void> {
  if (isDemoMode()) return;
  const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  await fetch(`${baseUrl}/api/v1/sessions/${sessionId}?user_id=${userId}`, {
    method: "DELETE",
  });
}

export async function renameSession(
  sessionId: string,
  title: string,
  userId: string = "demo",
): Promise<void> {
  if (isDemoMode()) return;
  const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  await fetch(`${baseUrl}/api/v1/sessions/${sessionId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, title }),
  });
}
```

- [ ] **Step 2: Create SessionSidebar component**

Create `jarvis-demo/components/SessionSidebar.tsx`:

```tsx
"use client";

import { useEffect, useState, useCallback } from "react";
import { ChatSession, listSessions, archiveSession } from "@/lib/api";

interface SessionSidebarProps {
  currentSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onNewConversation: () => void;
}

export default function SessionSidebar({
  currentSessionId,
  onSelectSession,
  onNewConversation,
}: SessionSidebarProps) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [isOpen, setIsOpen] = useState(false);

  const refresh = useCallback(async () => {
    const data = await listSessions();
    setSessions(data);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh, currentSessionId]);

  const handleArchive = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    await archiveSession(id);
    refresh();
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffDays = Math.floor(diffMs / 86400000);
    if (diffDays === 0) return "Today";
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) return `${diffDays}d ago`;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  };

  return (
    <>
      {/* Mobile toggle */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="lg:hidden fixed top-16 left-2 z-40 p-2 rounded-lg bg-[var(--card-bg)] border border-[var(--border)] shadow-lg"
        aria-label="Toggle sessions"
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d={isOpen ? "M6 18L18 6M6 6l12 12" : "M4 6h16M4 12h16M4 18h16"} />
        </svg>
      </button>

      {/* Sidebar */}
      <aside
        className={`
          fixed lg:relative z-30 top-0 left-0 h-full w-64
          bg-[var(--card-bg)] border-r border-[var(--border)]
          flex flex-col transition-transform duration-200
          ${isOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"}
        `}
      >
        {/* Header */}
        <div className="p-3 border-b border-[var(--border)] flex items-center gap-2">
          <button
            onClick={() => { onNewConversation(); setIsOpen(false); }}
            className="flex-1 py-2 px-3 rounded-lg border border-[var(--border)]
                       hover:bg-emerald-500/10 hover:border-emerald-500/30
                       text-sm font-medium transition-colors text-left"
          >
            + New chat
          </button>
        </div>

        {/* Session list */}
        <div className="flex-1 overflow-y-auto py-2">
          {sessions.length === 0 && (
            <p className="text-xs text-[var(--muted)] px-4 py-6 text-center">
              No conversations yet
            </p>
          )}
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => { onSelectSession(s.id); setIsOpen(false); }}
              className={`
                w-full text-left px-3 py-2.5 text-sm group flex items-center gap-2
                hover:bg-[var(--background)] transition-colors relative
                ${s.id === currentSessionId ? "bg-emerald-500/10 border-r-2 border-emerald-500" : ""}
              `}
            >
              <div className="flex-1 min-w-0">
                <div className="truncate font-medium">
                  {s.title || "Untitled"}
                </div>
                <div className="text-xs text-[var(--muted)]">
                  {formatDate(s.updated_at)}
                </div>
              </div>
              <button
                onClick={(e) => handleArchive(e, s.id)}
                className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-red-500/10
                           text-[var(--muted)] hover:text-red-400 transition-all"
                aria-label="Archive"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </button>
            </button>
          ))}
        </div>
      </aside>

      {/* Mobile overlay */}
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-20 lg:hidden"
          onClick={() => setIsOpen(false)}
        />
      )}
    </>
  );
}
```

- [ ] **Step 3: Update chat page layout to include sidebar**

Read `jarvis-demo/app/chat/page.tsx` and update to include the sidebar:

```tsx
"use client";

import JarvisChatPanel from "@/components/JarvisChatPanel";
import SessionSidebar from "@/components/SessionSidebar";
import { useJarvisChat } from "@/lib/useJarvisChat";

// ... existing state for file handling ...

export default function ChatPage() {
  // ... existing file handling state ...

  const {
    conversationId,
    startNewConversation,
    loadConversation,
    // ... other hook values passed to JarvisChatPanel
  } = useJarvisChat();

  return (
    <div className="flex h-[calc(100vh-64px)]">
      <SessionSidebar
        currentSessionId={conversationId}
        onSelectSession={loadConversation}
        onNewConversation={startNewConversation}
      />
      <main className="flex-1 min-w-0">
        <JarvisChatPanel
          fileBase64={fileBase64}
          mediaType={mediaType}
          fileName={fileName}
          onClearFile={clearFile}
        />
      </main>
    </div>
  );
}
```

NOTE: The exact integration depends on how `useJarvisChat` is currently consumed in the chat page. The hook may need to be lifted to this page and its values passed down as props, OR the sidebar can call the hook's functions directly if the hook is used via context. Check the current architecture and adapt.

- [ ] **Step 4: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/SessionSidebar.tsx lib/api.ts app/chat/page.tsx
git commit -m "feat: add session sidebar for conversation management"
```

---

## Task 4: Auto-Replan on Habit Changes

**Files:**
- Create: `jarvis-demo/components/ReplanBanner.tsx`
- Modify: `jarvis-demo/lib/useJarvisChat.ts` (auto-replan logic)
- Modify: `jarvis-demo/components/JarvisResponse.tsx:573-577` (render ReplanBanner)

When user adds a habit ("I hate mornings"), the backend returns `suggested_action: "replan"`. Currently the frontend shows this as a text banner. We need a prominent one-click replan button.

- [ ] **Step 1: Create ReplanBanner component**

Create `jarvis-demo/components/ReplanBanner.tsx`:

```tsx
"use client";

import { useState } from "react";

interface ReplanBannerProps {
  onReplan: () => void;
  isReplanning?: boolean;
}

export default function ReplanBanner({ onReplan, isReplanning }: ReplanBannerProps) {
  return (
    <div className="mt-3 p-3 rounded-xl border border-amber-500/30 bg-amber-500/5
                    flex items-center justify-between gap-3">
      <div className="flex items-center gap-2 min-w-0">
        <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse shrink-0" />
        <p className="text-sm text-amber-200">
          Your preferences changed. Replan to apply them to your schedule.
        </p>
      </div>
      <button
        onClick={onReplan}
        disabled={isReplanning}
        className="shrink-0 px-4 py-1.5 rounded-lg text-sm font-medium
                   bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/30
                   text-amber-200 transition-colors disabled:opacity-50"
      >
        {isReplanning ? (
          <span className="flex items-center gap-1.5">
            <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" strokeDasharray="31.4" strokeDashoffset="10" />
            </svg>
            Replanning...
          </span>
        ) : (
          "Replan Schedule"
        )}
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Add replan handler to useJarvisChat**

In `jarvis-demo/lib/useJarvisChat.ts`, add a `triggerReplan` function:

```typescript
  const [isReplanning, setIsReplanning] = useState(false);

  const triggerReplan = useCallback(async () => {
    // Find the last PLAN_DAY message to get the original goal
    const lastPlanMsg = [...messages].reverse().find(
      (m) => m.role === "assistant" && m.response?.intent === "PLAN_DAY"
    );
    const lastUserGoal = [...messages].reverse().find(
      (m) => m.role === "user"
    );

    const replanPrompt = lastPlanMsg?.response?.execution_graph?.goal_metadata?.objective
      ? `Replan: ${lastPlanMsg.response.execution_graph.goal_metadata.objective}`
      : lastUserGoal?.content || "Replan my schedule with updated preferences";

    setIsReplanning(true);
    try {
      await sendMessage(replanPrompt);
    } finally {
      setIsReplanning(false);
    }
  }, [messages, sendMessage]);
```

Export `triggerReplan` and `isReplanning` from the hook return.

- [ ] **Step 3: Render ReplanBanner in JarvisResponse**

In `jarvis-demo/components/JarvisResponse.tsx`, replace the current `suggested_action` banner (around line 573-577). The current code is approximately:

```tsx
{response?.suggested_action && (
  <div className="...">Suggested: {response.suggested_action}</div>
)}
```

Replace with:

```tsx
{response?.suggested_action === "replan" && !isStreaming && (
  <ReplanBanner
    onReplan={onReplan}
    isReplanning={isReplanning}
  />
)}
```

Add `onReplan` and `isReplanning` to the component props:

```typescript
interface JarvisResponseProps {
  // ... existing props
  onReplan?: () => void;
  isReplanning?: boolean;
}
```

- [ ] **Step 4: Wire props through JarvisChatPanel**

In `JarvisChatPanel.tsx` where `JarvisResponse` is rendered (around line 190), pass the replan handler:

```tsx
<JarvisResponse
  // ... existing props
  onReplan={triggerReplan}
  isReplanning={isReplanning}
/>
```

- [ ] **Step 5: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/ReplanBanner.tsx lib/useJarvisChat.ts components/JarvisResponse.tsx components/JarvisChatPanel.tsx
git commit -m "feat: add one-click replan banner when habits change"
```

---

## Task 5: Agentic Clarification Flow (Backend)

**Files:**
- Modify: `Jarvis-Engine/app/schemas/context.py:211-266` (add clarification fields)
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py:355-369` (clarification detection)

When Jarvis receives an ambiguous request like "do the thing" or "schedule it" with no prior context, it should ask for clarification instead of guessing.

- [ ] **Step 1: Add clarification fields to ChatResponse**

In `Jarvis-Engine/app/schemas/context.py`, add after the `message_id` field:

```python
    clarification_options: Optional[List[str]] = Field(
        default=None,
        description="Quick-reply options when Jarvis needs clarification. Frontend renders as buttons.",
    )
```

- [ ] **Step 2: Add clarification detection to brain dump extraction**

In `Jarvis-Engine/app/services/analytical/control_policy.py`, modify `_run_brain_dump_extraction` to detect when the extraction is ambiguous AND there's no conversation history to resolve it.

After the existing extraction logic (around line 369), add a new helper:

```python
def _needs_clarification(
    extraction: Optional[BrainDumpExtraction],
    user_prompt: str,
    conversation_history: list[dict] | None,
) -> Optional[ChatResponse]:
    """Return a clarification ChatResponse if the request is too ambiguous.

    Only triggers when:
    - Extraction is empty or None
    - No conversation history to resolve context
    - Prompt is very short (< 15 chars) or uses pronouns without antecedent
    """
    if conversation_history:
        return None  # Multi-turn context available, let pipeline handle it

    if extraction and not _is_extraction_empty(extraction):
        return None  # Extraction succeeded, no clarification needed

    prompt_lower = user_prompt.strip().lower()

    # Short ambiguous prompts
    ambiguous_patterns = [
        "do it", "schedule it", "plan it", "ok", "yes", "sure",
        "go ahead", "the thing", "do the thing", "that",
    ]
    is_ambiguous = (
        len(prompt_lower) < 15
        and any(p in prompt_lower for p in ambiguous_patterns)
    )

    if not is_ambiguous:
        return None

    return ChatResponse(
        intent="CLARIFICATION",
        message="I'd like to help! Could you give me a bit more detail?",
        clarification_options=[
            "Plan my day",
            "Help me study for an exam",
            "I want to build a habit",
            "Break down a project into tasks",
        ],
    )
```

- [ ] **Step 3: Call clarification check in execute_agentic_flow**

In `execute_agentic_flow`, after the brain dump extraction call (around line 1073) and before the fallback check:

```python
    extraction = await _run_brain_dump_extraction(effective_prompt, conversation_history=conversation_history)
    _extraction_duration = int((time_mod.monotonic() - _phase_start) * 1000)

    # Check if clarification is needed before proceeding
    clarification = _needs_clarification(extraction, effective_prompt, conversation_history)
    if clarification:
        if progress_callback:
            await progress_callback("intent_classified", {"intent": "CLARIFICATION"})
        return clarification

    if extraction is None or _is_extraction_empty(extraction):
        return await _fallback_single_intent(
            # ... existing fallback code
        )
```

- [ ] **Step 4: Verify syntax**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "import ast; ast.parse(open('app/services/analytical/control_policy.py').read()); print('OK')"`

- [ ] **Step 5: Write test**

Create a test in `Jarvis-Engine/tests/test_clarification.py`:

```python
"""Tests for clarification detection."""
from app.services.analytical.control_policy import _needs_clarification


def test_short_ambiguous_prompt_no_history():
    result = _needs_clarification(None, "do it", None)
    assert result is not None
    assert result.intent == "CLARIFICATION"
    assert result.clarification_options is not None
    assert len(result.clarification_options) > 0


def test_short_ambiguous_prompt_with_history():
    """With conversation history, don't ask for clarification."""
    history = [{"role": "user", "content": "plan my day"}]
    result = _needs_clarification(None, "do it", history)
    assert result is None


def test_long_prompt_no_clarification():
    result = _needs_clarification(None, "I want to study for my math exam tomorrow", None)
    assert result is None


def test_valid_extraction_no_clarification():
    from app.schemas.context import BrainDumpExtraction
    extraction = BrainDumpExtraction(planning_goal="study math")
    result = _needs_clarification(extraction, "do it", None)
    assert result is None
```

- [ ] **Step 6: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_clarification.py -v`

- [ ] **Step 7: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/schemas/context.py app/services/analytical/control_policy.py tests/test_clarification.py
git commit -m "feat: add clarification detection for ambiguous prompts"
```

---

## Task 6: Agentic Clarification Flow (Frontend)

**Files:**
- Create: `jarvis-demo/components/ClarificationCard.tsx`
- Modify: `jarvis-demo/components/JarvisResponse.tsx` (render clarification options)

- [ ] **Step 1: Create ClarificationCard component**

Create `jarvis-demo/components/ClarificationCard.tsx`:

```tsx
"use client";

interface ClarificationCardProps {
  options: string[];
  onSelect: (option: string) => void;
  disabled?: boolean;
}

export default function ClarificationCard({
  options,
  onSelect,
  disabled,
}: ClarificationCardProps) {
  return (
    <div className="mt-3 space-y-2">
      <p className="text-xs text-[var(--muted)] font-medium uppercase tracking-wider">
        Quick replies
      </p>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => (
          <button
            key={option}
            onClick={() => onSelect(option)}
            disabled={disabled}
            className="px-4 py-2 rounded-xl text-sm
                       border border-[var(--border)] bg-[var(--card-bg)]
                       hover:border-emerald-500/40 hover:bg-emerald-500/5
                       active:scale-[0.98] transition-all
                       disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {option}
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Render ClarificationCard in JarvisResponse**

In `jarvis-demo/components/JarvisResponse.tsx`, add after the main message section (around line 516), before the error display:

```tsx
        {/* Clarification quick-replies */}
        {response?.clarification_options &&
          response.clarification_options.length > 0 &&
          !isStreaming && (
            <ClarificationCard
              options={response.clarification_options}
              onSelect={onClarificationSelect}
              disabled={isStreaming}
            />
          )}
```

Add `onClarificationSelect` to the component props:

```typescript
interface JarvisResponseProps {
  // ... existing props
  onClarificationSelect?: (option: string) => void;
}
```

- [ ] **Step 3: Wire clarification handler through JarvisChatPanel**

In `JarvisChatPanel.tsx`, where `JarvisResponse` is rendered, add:

```tsx
<JarvisResponse
  // ... existing props
  onClarificationSelect={(option) => sendMessage(option)}
/>
```

- [ ] **Step 4: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/ClarificationCard.tsx components/JarvisResponse.tsx components/JarvisChatPanel.tsx
git commit -m "feat: add clarification quick-reply cards for ambiguous prompts"
```

---

## Task 7: Accept Flow Polish

**Files:**
- Modify: `jarvis-demo/components/ScheduleSection.tsx:479-503` (accept button UX)
- Modify: `jarvis-demo/lib/useJarvisChat.ts` (accept feedback state)

The accept button should have visual feedback — a confirmation state and a success transition.

- [ ] **Step 1: Add accept states to useJarvisChat**

In `useJarvisChat.ts`, add state for accept feedback:

```typescript
const [acceptState, setAcceptState] = useState<"idle" | "accepting" | "accepted">("idle");
```

Update the existing `acceptDraft` function to manage this state:

```typescript
  const acceptDraft = useCallback(async () => {
    if (!draftScheduleResponse) return;
    setAcceptState("accepting");
    try {
      const tasks = draftScheduleResponse.execution_graph?.decomposition || [];
      await acceptSchedule(tasks, "demo");
      promoteDraftToFinal(draftScheduleResponse);
      setAcceptState("accepted");
      // Reset after showing success
      setTimeout(() => {
        setDraftScheduleResponse(null);
        setAcceptState("idle");
      }, 2000);
    } catch {
      setAcceptState("idle");
    }
  }, [draftScheduleResponse]);
```

Export `acceptState` from the hook.

- [ ] **Step 2: Update ScheduleSection draft footer**

In `jarvis-demo/components/ScheduleSection.tsx`, update the draft footer section (around line 479-493). Add `acceptState` as a prop:

```typescript
interface ScheduleSectionProps {
  // ... existing props
  acceptState?: "idle" | "accepting" | "accepted";
}
```

Replace the accept button rendering:

```tsx
        {/* Draft action bar */}
        {isDraft && (
          <div className="sticky bottom-0 bg-[var(--card-bg)] border-t border-amber-500/20
                          p-3 flex items-center justify-between gap-3">
            {acceptState === "accepted" ? (
              <div className="flex items-center gap-2 text-emerald-400 text-sm font-medium w-full justify-center py-1">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                Schedule accepted!
              </div>
            ) : (
              <>
                <button
                  onClick={() => setShowSuggestInput(!showSuggestInput)}
                  className="px-4 py-2 rounded-lg text-sm border border-[var(--border)]
                             hover:bg-[var(--background)] transition-colors"
                >
                  Suggest Changes
                </button>
                <button
                  onClick={onAccept}
                  disabled={acceptState === "accepting"}
                  className="px-6 py-2 rounded-lg text-sm font-medium
                             bg-emerald-500 hover:bg-emerald-600 text-white
                             transition-all active:scale-[0.97]
                             disabled:opacity-60 disabled:cursor-wait"
                >
                  {acceptState === "accepting" ? (
                    <span className="flex items-center gap-2">
                      <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                        <circle cx="12" cy="12" r="10" stroke="currentColor"
                                strokeWidth="2" strokeDasharray="31.4" strokeDashoffset="10" />
                      </svg>
                      Accepting...
                    </span>
                  ) : (
                    "Accept Schedule"
                  )}
                </button>
              </>
            )}
          </div>
        )}
```

- [ ] **Step 3: Pass acceptState through JarvisResponse**

In `JarvisResponse.tsx`, where `ScheduleSection` is rendered for drafts (around line 540-562), pass `acceptState`:

```tsx
<ScheduleSection
  // ... existing props
  acceptState={acceptState}
/>
```

Add `acceptState` to JarvisResponse props and wire through from JarvisChatPanel.

- [ ] **Step 4: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add components/ScheduleSection.tsx components/JarvisResponse.tsx components/JarvisChatPanel.tsx lib/useJarvisChat.ts
git commit -m "feat: polish accept flow with loading state and success feedback"
```

---

## Task 8: Draft Schedule Context Passing (Enable Chat-Based Editing)

**Files:**
- Modify: `jarvis-demo/lib/useJarvisChat.ts` (auto-pass draft context)

When a draft schedule is active and the user sends a follow-up message like "make task 2 longer" or "remove the reading task", the frontend should automatically include the draft schedule as context so the backend's `/modify-schedule` logic can apply the change.

- [ ] **Step 1: Auto-include draft_schedule in requests when draft is active**

In `useJarvisChat.ts`, in the `sendMessage()` function where the request is constructed, add draft context:

```typescript
    // Auto-include draft schedule context for chat-based editing
    const draftContext = draftScheduleResponse;
    if (draftContext?.schedule && draftContext?.execution_graph) {
      request.draft_schedule = {
        schedule: draftContext.schedule,
        execution_graph: draftContext.execution_graph,
        horizon_start: draftContext.schedule?.horizon_start || new Date().toISOString(),
      };
    }
```

This is the key mechanism that enables the user to say "make the first task shorter" in chat and have it work. The backend's `execute_agentic_flow` already checks for `draft_schedule` and routes to the modification flow (control_policy.py line 1013-1021).

- [ ] **Step 2: Update draft state when modification response arrives**

In the `onComplete` handler, after existing draft handling:

```typescript
        // Update draft if modification returned a new schedule
        if (response.schedule_status === "draft" && response.schedule) {
          const draftResponse = {
            ...response,
            execution_graph: response.execution_graph,
            schedule: response.schedule,
          };
          setDraftScheduleResponse(draftResponse);
          saveDraftSchedule(draftResponse);
        }
```

This should already be handled by existing logic (around line 231-234 in the current hook), but verify it works for modification responses too.

- [ ] **Step 3: Verify**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-demo && npx tsc --noEmit 2>&1 | head -20`

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-demo
git add lib/useJarvisChat.ts
git commit -m "feat: auto-pass draft schedule context for chat-based task editing"
```

---

## Summary

### What This Delivers

| Before | After |
|--------|-------|
| Every message is stateless | Multi-turn conversations with `conversation_id` |
| No conversation history | Session sidebar to list/switch/archive conversations |
| "suggested_action: replan" renders as text | One-click Replan button with loading state |
| Ambiguous prompts always processed | Clarification cards with quick-reply options |
| Accept button has no feedback | Loading → success transition with checkmark |
| Chat-based editing requires manual context | Draft schedule auto-included for modification |

### User Flow After Implementation

```
1. Open Jarvis → see session sidebar + chat
2. Type "Plan my study day" →
   - If ambiguous: Jarvis shows quick-reply options
   - If clear: get task proposals in TaskPreview
3. Edit tasks (title, duration, difficulty) → click "Schedule"
4. Review draft schedule → accept or suggest changes via chat
5. Later: "I hate mornings" →
   - Habit saved → prominent "Replan Schedule" button
   - Click replan → schedule auto-recalibrates
6. Accept → success animation → schedule finalized
```

### Separate Plan (Not Covered Here)

**Google Calendar Integration** — OAuth flow, push accepted schedules to Google Calendar, pull existing events as constraints. This is an independent subsystem requiring its own plan.

### Performance Impact

- Session sidebar: 1 API call on mount (~50ms)
- conversation_id passing: 0 overhead (just a string in request body)
- Clarification check: negligible (string comparison, no LLM call)
- Replan trigger: same cost as normal chat request
