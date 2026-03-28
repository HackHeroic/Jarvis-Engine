# Demo Frontend Update — Design Spec

**Date:** 2026-03-29
**Author:** Madhav + Claude
**Status:** Draft — awaiting review
**Purpose:** Update jarvis-demo frontend to showcase Phase 1 backend features for VC pitch (Wednesday April 1, 2026)

---

## Executive Summary

The Phase 1 architecture reset added memory, PEARL behavioral intelligence, document intelligence, and a registry framework to the backend — but the frontend doesn't show any of it. This spec updates the jarvis-demo to:

1. **Pre-loaded prompt selectors** — 4 scenario cards so the demo flows smoothly without typing
2. **Visual enhancements** — Motion animations, GSAP hero effects, light mode default, polished design
3. **Memory visibility** — "Jarvis Knows" panel showing extracted memories + PEARL insights
4. **Document feedback** — Classification toast after PDF upload
5. **Draft UX cleanup** — Wire to Supabase DraftStore, rejection reason capture
6. **Updated intent badges** — Match the new registry (PLAN_DAY, EDIT_TASK, etc.)
7. **Demo + Live mode** — Both get prompt selectors; demo uses mock data, live hits real backend

---

## Prompt Selector Cards

### Design

4 scenario cards displayed when the chat is empty (no messages). Each card has:
- Icon (emoji)
- Title (color-coded by scenario type)
- Pre-written prompt text (visible to user)
- Gradient background matching the category

### Card Definitions

| Card | Icon | Title | Prompt | Color |
|------|------|-------|--------|-------|
| 1 | 🎓 | Learn a Concept | "Teach me Dijkstra's algorithm — explain with a step-by-step example" | Emerald |
| 2 | 📋 | Plan a Complex Task | "I have a deep learning contest Friday — I need to study CNNs, backpropagation, and optimization algorithms. Also have a calculus exam Monday covering integration and limits. Plan my week." | Blue |
| 3 | ⚡ | Add Contradicting Habit | "I don't work before 2 PM and need 1-hour breaks between study sessions" | Amber |
| 4 | 📄 | Upload & Link Material | Opens file picker, then auto-sends with: "Here are practice problems for my deep learning contest" | Purple |

### Behavior

**Demo Mode:** Click → instantly triggers pre-built mock response from `demoData.ts`. No typing needed. Simulated streaming with thinking + content tokens.

**Live Mode:** Click → fills the chat input with the prompt text. User presses Send (or Enter). Real backend streaming.

**Card 4 (Upload):** Click → opens file picker. After file selected, auto-fills the prompt and attaches the file. In demo mode, uses a built-in mock PDF response.

### Component

```
File: components/PromptSelector.tsx
Props: onSelectPrompt(prompt: string), onSelectFile(), mode: "demo" | "live"
Location: Rendered inside JarvisChatPanel when messages.length === 0
Animation: Cards fade in with stagger (Motion), hover lift + subtle scale
```

---

## Visual Enhancements

### Light Mode Default

Change `themeContext.tsx` to default to `"light"` instead of system preference:
```typescript
// Current: fallback to system preference
// New: default to "light", user can toggle
const defaultTheme = localStorage.getItem("jarvis-theme") || "light";
```

Also update the inline script in `layout.tsx` to match.

### Motion Animations (Already Installed)

The `motion` package (11.18.2) is already in `package.json`. Apply to:

| Element | Animation | Duration |
|---------|-----------|----------|
| Chat messages | `slideInFromBottom` with spring | 0.3s |
| Prompt selector cards | `fadeIn` with stagger (100ms each) | 0.4s |
| Schedule tasks appearing | `slideInFromLeft` with stagger | 0.3s |
| Phase indicators | `scale` pulse on active | 0.2s loop |
| Memory panel | `slideInFromRight` | 0.3s |
| PEARL insight banner | `slideInFromTop` + `fadeIn` | 0.5s |
| Document toast | `slideInFromBottomRight` | 0.4s |

### GSAP (New Dependency)

Install: `npm install gsap`

Use for:
- **Home hero:** Text reveal with character stagger ("Reclaim your focus with Jarvis")
- **Architecture page:** Diagram nodes animate in sequence
- **Page transitions:** Smooth scroll between sections

### Spline 3D (Home Hero — Optional)

Install: `npm install @splinetool/react-spline`

Try to find a community scene (brain/neural network/abstract nodes) from spline.design community. If nothing fits, fall back to a GSAP particle animation or animated gradient.

Embed in hero section of `app/page.tsx` — responsive, behind the headline text.

### Design Tokens Update

| Token | Current Light | New Light | Change |
|-------|--------------|-----------|--------|
| `--background` | #fafafa | #ffffff | Slightly brighter |
| `--card-bg` | #ffffff | #ffffff | No change |
| `--border` | #e5e7eb | #e2e8f0 | Slightly softer |
| `--accent` | #059669 | #059669 | Keep emerald |
| Border radius | Mixed (8px, 12px, 16px) | 12px everywhere | Consistent |
| Card shadow | None | `0 1px 3px rgba(0,0,0,0.05)` | Subtle depth |
| Font | System | Inter (Google Fonts) or system | Cleaner |

---

## Memory Panel — "Jarvis Knows"

### Design

A collapsible panel on the right side of the chat interface. Shows what Jarvis has learned about the user, grouped by memory type.

### Layout

```
┌──────────────────────────────────────────┐
│  🧠 Jarvis Knows          [Collapse ▼]  │
│                                          │
│  ── Scheduling Constraints ──            │
│  • No work before 2 PM                   │
│  • 1-hour breaks between sessions        │
│                                          │
│  ── Active Goals ──                      │
│  • DL contest Friday                     │
│  • Calculus exam Monday                  │
│                                          │
│  ── Observed Patterns ──                 │
│  • Skips tasks before 10 AM (87%)        │
│  • Most productive 2-5 PM (92%)          │
│                                          │
│  ── Facts ──                             │
│  • CS student                            │
│  • Prefers 15-min study blocks           │
│                                          │
│  3 memories added this session           │
└──────────────────────────────────────────┘
```

### Component

```
File: components/MemoryPanel.tsx
Props: memories: MemoryRecord[], isOpen: boolean, onToggle: () => void
Data source:
  - Demo mode: mock memories from demoData.ts
  - Live mode: Populated from ChatResponse — backend includes memories in response metadata. No new endpoint needed for demo.
Animation: Slide in from right (Motion), memory items fade in with stagger
```

### Memory Types Display

| Type | Icon | Color |
|------|------|-------|
| constraint | 🔒 | Red |
| goal | 🎯 | Blue |
| behavioral_pattern | 📊 | Purple |
| preference | 💡 | Amber |
| fact | 📌 | Slate |
| temporal_event | 📅 | Cyan |
| feedback | 💬 | Green |

---

## PEARL Insight Banner

### Design

An animated banner that appears in the chat when Jarvis detects a behavioral pattern. Distinct from regular messages — it's a system observation, not a response.

### Layout

```
┌─────────────────────────────────────────────┐
│  📊 Behavioral Insight                       │
│                                              │
│  I've noticed you tend to skip tasks around  │
│  9 AM. I've adjusted your schedule to avoid  │
│  scheduling deep work at that time.          │
│                                              │
│  Confidence: ████████░░ 85%     [Dismiss]   │
└─────────────────────────────────────────────┘
```

### Component

```
File: components/PearlInsightBanner.tsx
Props: insight: string, confidence: number, onDismiss: () => void
Animation: slideInFromTop + fadeIn, subtle purple glow border
Colors: Purple gradient background (rgba(139,92,246,0.05))
Data source:
  - Demo mode: triggered after "Add Contradicting Habit" scenario
  - Live mode: from ChatResponse.pearl_insights (new field) or separate API
```

---

## Document Classification Toast

### Design

A toast notification that appears after PDF upload, showing what Jarvis detected.

### Layout

```
┌──────────────────────────────────────┐
│  📄 Practice Problems Detected       │
│                                      │
│  10 problems found                   │
│  Topics: CNNs, backpropagation       │
│  Linked to 3 existing tasks          │
│                                      │
│  Confidence: 92%        [View Tasks] │
└──────────────────────────────────────┘
```

### Component

```
File: components/DocumentClassificationToast.tsx
Props: classification: DocumentClassification, onViewTasks?: () => void
Animation: slideInFromBottomRight, auto-dismiss after 8 seconds
Position: Bottom-right corner, above the chat input
Data source:
  - Demo mode: triggered after "Upload & Link Material" scenario
  - Live mode: from ingestion response
```

---

## Draft UX Cleanup

### Changes to Existing Components

**ScheduleSection.tsx:**
- Wire "Accept" button to new Supabase DraftStore (`POST /api/v1/drafts/{id}/accept`)
- Wire "Reject" button → show rejection reason modal → `POST /api/v1/drafts/{id}/reject`
- Add "Chat to Modify" button → fills chat input with "Change the schedule: " prefix
- Draft badge styling: amber border + "Draft" label with pulse animation

**TaskPreview.tsx:**
- No major changes — already has edit/confirm/regenerate flow

### Rejection Reason Modal

```
┌────────────────────────────────────┐
│  Why are you rejecting this        │
│  schedule?                         │
│                                    │
│  ┌──────────────────────────────┐  │
│  │ Too many tasks before lunch  │  │
│  └──────────────────────────────┘  │
│                                    │
│  [Cancel]            [Reject]      │
└────────────────────────────────────┘
```

This reason gets stored as a memory (via the reject endpoint), so Jarvis learns from rejections.

---

## Intent Badge Updates

### Current → New Mapping

| Old Badge | New Badge | Color |
|-----------|-----------|-------|
| GREETING | CHAT | Slate |
| GENERAL_QA | CHAT | Slate |
| PLAN_DAY | PLAN_DAY | Emerald |
| KNOWLEDGE_INGESTION | INGEST_DOCUMENT | Blue |
| BEHAVIORAL_CONSTRAINT | ADD_CONSTRAINT | Amber |
| CALENDAR_SYNC | ADD_CONSTRAINT | Amber |
| ACTION_ITEM | EDIT_TASK | Cyan |
| — (new) | ACCEPT_DRAFT | Green |
| — (new) | REJECT_DRAFT | Red |
| — (new) | REARRANGE | Purple |
| — (new) | CHECK_PROGRESS | Teal |

Update `JarvisResponse.tsx` intent badge rendering to use new intent names and colors.

---

## Demo Mode Mock Data Updates

### New Mock Data in demoData.ts

Add mock responses for each scenario card:

1. **Learn a Concept** — Dijkstra's algorithm explanation with markdown, code blocks, step-by-step walkthrough. Intent: CHAT.

2. **Plan Complex Task** — Full decomposition with 8-10 tasks across DL contest + calculus exam. Intent: PLAN_DAY. Includes execution_graph, schedule, and draft_id.

3. **Add Contradicting Habit** — Response acknowledging the constraint, showing schedule recalibration. Intent: ADD_CONSTRAINT. Schedule shifts tasks to afternoon.

4. **Upload & Link Material** — Classification result (practice_problems, 10 problems, topics: CNNs, backpropagation). Intent: INGEST_DOCUMENT. Shows linked tasks.

### Mock Memory Data

```typescript
const MOCK_MEMORIES = [
  { type: "goal", content: "DL contest on Friday" },
  { type: "goal", content: "Calculus exam on Monday" },
  { type: "constraint", content: "No work before 2 PM" },
  { type: "constraint", content: "1-hour breaks between sessions" },
  { type: "behavioral_pattern", content: "Skips tasks before 10 AM (87%)" },
  { type: "behavioral_pattern", content: "Most productive 2-5 PM (92%)" },
  { type: "fact", content: "CS student" },
  { type: "preference", content: "Prefers 15-min study blocks" },
];
```

### Mock PEARL Insight

```typescript
const MOCK_PEARL_INSIGHT = {
  insight: "I've noticed you tend to skip tasks around 9 AM. I've adjusted your schedule to avoid scheduling deep work at that time.",
  confidence: 0.85,
};
```

---

## Updated Architecture Diagrams

Update `lib/architectureDiagrams.ts` to reflect the Phase 1 architecture:
- Replace old 9-layer stack with current architecture
- Show BaseRegistry, Memory System, Document Intelligence, PEARL
- Use diagrams from `docs/PITCH_ARCHITECTURE.md`

---

## File Changes Summary

| Action | File | What Changes |
|--------|------|-------------|
| Create | `components/PromptSelector.tsx` | Scenario card grid |
| Create | `components/MemoryPanel.tsx` | "Jarvis Knows" sidebar |
| Create | `components/PearlInsightBanner.tsx` | Behavioral insight banner |
| Create | `components/DocumentClassificationToast.tsx` | Upload classification feedback |
| Create | `components/RejectionReasonModal.tsx` | Draft rejection with reason |
| Modify | `components/JarvisChatPanel.tsx` | Add PromptSelector, MemoryPanel toggle |
| Modify | `components/JarvisResponse.tsx` | Update intent badges, add PEARL banner |
| Modify | `components/ScheduleSection.tsx` | Wire draft accept/reject to new API |
| Modify | `lib/demoData.ts` | Add 4 scenario mock responses + mock memories |
| Modify | `lib/api.ts` | Add draft accept/reject API calls |
| Modify | `lib/themeContext.tsx` | Default to light mode |
| Modify | `app/layout.tsx` | Light mode default in inline script |
| Modify | `app/page.tsx` | GSAP hero animation, optional Spline 3D |
| Modify | `app/globals.css` | Consistent border-radius, shadows, typography |
| Modify | `lib/architectureDiagrams.ts` | Update to Phase 1 architecture |
| Install | `gsap` | GSAP animation library |
| Install | `@splinetool/react-spline` | Spline 3D embed (optional) |

---

## Implementation Order

1. **Light mode default + design token polish** (globals.css, themeContext, layout)
2. **Prompt selector cards** (PromptSelector.tsx, wire into JarvisChatPanel)
3. **Demo mode mock data** (demoData.ts updates for all 4 scenarios)
4. **Intent badge updates** (JarvisResponse.tsx)
5. **Memory panel** (MemoryPanel.tsx, wire into chat layout)
6. **PEARL insight banner** (PearlInsightBanner.tsx, wire into response flow)
7. **Document classification toast** (DocumentClassificationToast.tsx)
8. **Draft UX cleanup** (ScheduleSection.tsx, RejectionReasonModal.tsx, api.ts)
9. **GSAP hero animation** (app/page.tsx)
10. **Spline 3D** (optional, if community scene found)
11. **Architecture diagrams update** (architectureDiagrams.ts)
12. **Motion animations** across all new + existing components
