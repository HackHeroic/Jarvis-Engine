# WS3: Frontend Schedule/Calendar Rebuild Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the schedule page with a proper time grid, day/week/month view toggles, blocked window rendering, complete/skip action buttons, and demo data for testing.

**Architecture:** Replace the flat card list with a CSS-positioned time grid. Add view mode state for day/week/month. Wire task actions (complete, skip) to existing API functions. Add demo schedule data for DEMO_MODE.

**Tech Stack:** Next.js 14, TypeScript, React, Tailwind CSS, Lucide React icons

**Spec:** `docs/superpowers/specs/2026-03-30-jarvis-spec-compliance-fix-design.md` (Workstream 3)

**Frontend root:** `/Users/madhav/Jarvis-cursor/jarvis-frontend`

---

### Task 1: Add demo schedule data to demoData.ts

**Files:**
- Modify: `lib/demoData.ts`
- Modify: `lib/api.ts`

- [ ] **Step 1: Add demo schedule tasks to demoData.ts**

Add at the bottom of `lib/demoData.ts`:

```typescript
// ── Demo Schedule Data ───────────────────────────────────────────────

const today = new Date();
const todayStr = today.toISOString().split("T")[0];

export const DEMO_SCHEDULE_TASKS: Record<string, unknown>[] = [
  {
    task_id: "demo_blocked_sleep",
    title: "Sleep",
    start_time: `${todayStr}T00:00:00`,
    end_time: `${todayStr}T08:00:00`,
    duration_minutes: 480,
    status: "completed",
    goal_id: "blocked",
    constraint_applied: "sleep",
  },
  {
    task_id: "demo_1",
    title: "Review DSA: Binary Search patterns",
    start_time: `${todayStr}T09:00:00`,
    end_time: `${todayStr}T09:25:00`,
    duration_minutes: 25,
    status: "completed",
    goal_id: "dsa_prep",
    completed_at: `${todayStr}T09:23:00`,
  },
  {
    task_id: "demo_2",
    title: "Practice: LeetCode Two Sum variations",
    start_time: `${todayStr}T09:30:00`,
    end_time: `${todayStr}T09:55:00`,
    duration_minutes: 25,
    status: "completed",
    goal_id: "dsa_prep",
    completed_at: `${todayStr}T09:50:00`,
  },
  {
    task_id: "demo_3",
    title: "Study CNNs: convolution layers",
    start_time: `${todayStr}T10:00:00`,
    end_time: `${todayStr}T10:25:00`,
    duration_minutes: 25,
    status: "in_progress",
    goal_id: "deep_learning",
  },
  {
    task_id: "demo_blocked_lunch",
    title: "Lunch Break",
    start_time: `${todayStr}T12:00:00`,
    end_time: `${todayStr}T13:00:00`,
    duration_minutes: 60,
    status: "pending",
    goal_id: "blocked",
    constraint_applied: "lunch",
  },
  {
    task_id: "demo_4",
    title: "Write essay outline: Climate Policy",
    start_time: `${todayStr}T13:30:00`,
    end_time: `${todayStr}T13:55:00`,
    duration_minutes: 25,
    status: "pending",
    goal_id: "essay_writing",
  },
  {
    task_id: "demo_5",
    title: "Draft essay introduction paragraph",
    start_time: `${todayStr}T14:00:00`,
    end_time: `${todayStr}T14:25:00`,
    duration_minutes: 25,
    status: "pending",
    goal_id: "essay_writing",
  },
  {
    task_id: "demo_blocked_volleyball",
    title: "Volleyball Practice",
    start_time: `${todayStr}T16:00:00`,
    end_time: `${todayStr}T17:30:00`,
    duration_minutes: 90,
    status: "pending",
    goal_id: "blocked",
    constraint_applied: "volleyball",
  },
  {
    task_id: "demo_6",
    title: "Review lecture notes: Backpropagation",
    start_time: `${todayStr}T18:00:00`,
    end_time: `${todayStr}T18:25:00`,
    duration_minutes: 25,
    status: "pending",
    goal_id: "deep_learning",
  },
];
```

- [ ] **Step 2: Update listTasks to return demo data in DEMO_MODE**

In `lib/api.ts`, find the `listTasks` function (line 514) and replace:

```typescript
export async function listTasks(): Promise<Record<string, unknown>[]> {
  if (IS_DEMO_MODE) {
    const { DEMO_SCHEDULE_TASKS } = await import("./demoData");
    return DEMO_SCHEDULE_TASKS;
  }
  const res = await fetch(
    `${API_BASE}/api/v1/tasks/?user_id=${encodeURIComponent(USER_ID)}`,
  );
  if (res.status === 404 || res.status === 503) return [];
  if (!res.ok) throw new Error(`Failed to list tasks: ${res.status}`);
  const data = await res.json();
  return data.tasks ?? data;
}
```

- [ ] **Step 3: Verify imports work**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit 2>&1 | grep -i "demo\|schedule" | head -5`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add lib/demoData.ts lib/api.ts
git commit -m "feat: add demo schedule data with tasks and blocked windows

Demo mode now returns 6 tasks + 3 blocked windows (sleep, lunch, volleyball)
spread across the day with mixed statuses for testing the schedule UI."
```

---

### Task 2: Rebuild schedule page with time grid and view toggles

**Files:**
- Modify: `app/(app)/schedule/page.tsx`
- Modify: `lib/transforms.ts`

- [ ] **Step 1: Update transforms.ts to preserve constraint_applied**

In `lib/transforms.ts`, in the `apiTasksToScheduleTasks` function, add `constraint_applied` to the return object:

```typescript
return {
  task_id: (t.task_id as string) || (t.id as string) || "",
  title: (t.title as string) || "Untitled",
  start_time: startTime,
  end_time: endTime,
  duration_minutes: dur,
  status,
  completed_at: t.completed_at ? new Date(t.completed_at as string) : undefined,
  goal_id: (t.goal_id as string) || undefined,
  color: colorForGoal((t.goal_id as string) || undefined),
  deadline_hint: (t.deadline_hint as string) || undefined,
  constraint_applied: (t.constraint_applied as string) || undefined,
} satisfies ScheduleTask;
```

- [ ] **Step 2: Rebuild schedule page with time grid**

Replace `app/(app)/schedule/page.tsx` with a time-grid layout:

```tsx
"use client";

import { useEffect, useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { listTasks, completeTask, skipTask } from "@/lib/api";
import { apiTasksToScheduleTasks } from "@/lib/transforms";
import { SM2QualityRating } from "@/components/app/SM2QualityRating";
import type { ScheduleTask } from "@/lib/types";
import {
  CheckCircle2,
  SkipForward,
  Calendar as CalendarIcon,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

const DAY_START_HOUR = 8;
const DAY_END_HOUR = 23;
const PX_PER_MINUTE = 1.5;
const HOURS = Array.from(
  { length: DAY_END_HOUR - DAY_START_HOUR + 1 },
  (_, i) => DAY_START_HOUR + i,
);

type ViewMode = "day" | "week" | "month";

function formatHour(h: number): string {
  const period = h >= 12 ? "PM" : "AM";
  const display = h > 12 ? h - 12 : h === 0 ? 12 : h;
  return `${display} ${period}`;
}

function getMinuteOfDay(date: Date): number {
  return date.getHours() * 60 + date.getMinutes();
}

export default function SchedulePage() {
  const router = useRouter();
  const [tasks, setTasks] = useState<ScheduleTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewMode, setViewMode] = useState<ViewMode>("day");
  const [selectedDate, setSelectedDate] = useState(new Date());
  const [qualityTaskId, setQualityTaskId] = useState<string | null>(null);

  const fetchTasks = async () => {
    try {
      const raw = await listTasks();
      setTasks(apiTasksToScheduleTasks(raw));
    } catch {
      setTasks([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks();
  }, []);

  // NOW indicator position
  const now = new Date();
  const nowMinute = getMinuteOfDay(now);
  const nowTop = (nowMinute - DAY_START_HOUR * 60) * PX_PER_MINUTE;

  // Filter tasks for selected date
  const dayTasks = useMemo(() => {
    const dayStart = new Date(selectedDate);
    dayStart.setHours(0, 0, 0, 0);
    const dayEnd = new Date(selectedDate);
    dayEnd.setHours(23, 59, 59, 999);
    return tasks.filter(
      (t) => t.start_time >= dayStart && t.start_time <= dayEnd,
    );
  }, [tasks, selectedDate]);

  const totalMinutes = (DAY_END_HOUR - DAY_START_HOUR + 1) * 60;
  const gridHeight = totalMinutes * PX_PER_MINUTE;

  const handleComplete = async (taskId: string, quality: number) => {
    await completeTask(taskId, undefined, quality);
    setQualityTaskId(null);
    fetchTasks();
  };

  const handleSkip = async (taskId: string) => {
    await skipTask(taskId);
    fetchTasks();
  };

  const navigateDay = (delta: number) => {
    const next = new Date(selectedDate);
    next.setDate(next.getDate() + delta);
    setSelectedDate(next);
  };

  // ── Day View ──────────────────────────────────────────────────────
  const renderDayView = () => (
    <div className="flex" style={{ minHeight: gridHeight }}>
      {/* Hour labels */}
      <div className="w-16 flex-shrink-0">
        {HOURS.map((h) => (
          <div
            key={h}
            className="text-xs text-muted-foreground pr-2 text-right"
            style={{ height: 60 * PX_PER_MINUTE }}
          >
            {formatHour(h)}
          </div>
        ))}
      </div>

      {/* Time grid */}
      <div className="flex-1 relative border-l border-border">
        {/* Hour lines */}
        {HOURS.map((h) => (
          <div
            key={h}
            className="absolute w-full border-t border-border/30"
            style={{ top: (h - DAY_START_HOUR) * 60 * PX_PER_MINUTE }}
          />
        ))}

        {/* NOW indicator */}
        {nowTop > 0 && nowTop < gridHeight && (
          <div
            className="absolute w-full h-0.5 bg-red-500 z-20"
            style={{ top: nowTop }}
          >
            <div className="absolute -left-1.5 -top-1 w-3 h-3 rounded-full bg-red-500" />
          </div>
        )}

        {/* Task blocks */}
        {dayTasks.map((task) => {
          const startMin = getMinuteOfDay(task.start_time);
          const top = (startMin - DAY_START_HOUR * 60) * PX_PER_MINUTE;
          const height = Math.max(task.duration_minutes * PX_PER_MINUTE, 20);
          const isBlocked = !!task.constraint_applied;
          const isCompleted = task.status === "completed";
          const isSkipped = task.status === "skipped";

          return (
            <div
              key={task.task_id}
              className={`absolute left-1 right-1 rounded-md px-2 py-1 text-xs overflow-hidden transition-all z-10 ${
                isBlocked
                  ? "bg-muted/40 border border-dashed border-muted-foreground/30 cursor-default"
                  : isCompleted
                    ? "bg-sage/20 border border-sage/40 cursor-pointer opacity-70"
                    : isSkipped
                      ? "bg-gold/10 border border-gold/30 cursor-default opacity-50 line-through"
                      : "border cursor-pointer hover:shadow-md"
              }`}
              style={{
                top,
                height,
                borderLeftColor: isBlocked ? undefined : task.color,
                borderLeftWidth: isBlocked ? undefined : 3,
                backgroundColor: isBlocked
                  ? undefined
                  : isCompleted
                    ? undefined
                    : `${task.color}10`,
              }}
              onClick={() => {
                if (!isBlocked) router.push(`/workspace/${task.task_id}`);
              }}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="font-medium truncate">{task.title}</span>
                {!isBlocked && !isCompleted && !isSkipped && (
                  <div className="flex gap-1 flex-shrink-0">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setQualityTaskId(task.task_id);
                      }}
                      className="p-0.5 rounded hover:bg-sage/20"
                      title="Complete"
                    >
                      <CheckCircle2 size={14} className="text-sage" />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleSkip(task.task_id);
                      }}
                      className="p-0.5 rounded hover:bg-gold/20"
                      title="Skip"
                    >
                      <SkipForward size={14} className="text-gold" />
                    </button>
                  </div>
                )}
              </div>
              {height > 30 && (
                <div className="text-muted-foreground mt-0.5">
                  {task.start_time.toLocaleTimeString([], {
                    hour: "numeric",
                    minute: "2-digit",
                  })}{" "}
                  – {task.duration_minutes}min
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );

  // ── Week View ─────────────────────────────────────────────────────
  const renderWeekView = () => {
    const weekStart = new Date(selectedDate);
    weekStart.setDate(weekStart.getDate() - weekStart.getDay());
    const days = Array.from({ length: 7 }, (_, i) => {
      const d = new Date(weekStart);
      d.setDate(d.getDate() + i);
      return d;
    });

    return (
      <div className="grid grid-cols-7 gap-1">
        {days.map((day) => {
          const dayStart = new Date(day);
          dayStart.setHours(0, 0, 0, 0);
          const dayEnd = new Date(day);
          dayEnd.setHours(23, 59, 59, 999);
          const dayTasks = tasks.filter(
            (t) => t.start_time >= dayStart && t.start_time <= dayEnd,
          );
          const isToday = day.toDateString() === now.toDateString();

          return (
            <div
              key={day.toISOString()}
              className={`border rounded-lg p-2 min-h-[120px] cursor-pointer hover:bg-surface-subtle ${
                isToday ? "border-terra" : "border-border"
              }`}
              onClick={() => {
                setSelectedDate(day);
                setViewMode("day");
              }}
            >
              <div className="text-xs font-medium mb-1">
                {day.toLocaleDateString([], { weekday: "short", day: "numeric" })}
              </div>
              {dayTasks
                .filter((t) => !t.constraint_applied)
                .slice(0, 4)
                .map((t) => (
                  <div
                    key={t.task_id}
                    className="text-[10px] truncate py-0.5 px-1 rounded mb-0.5"
                    style={{ backgroundColor: `${t.color}20`, borderLeft: `2px solid ${t.color}` }}
                  >
                    {t.title}
                  </div>
                ))}
              {dayTasks.filter((t) => !t.constraint_applied).length > 4 && (
                <div className="text-[10px] text-muted-foreground">
                  +{dayTasks.filter((t) => !t.constraint_applied).length - 4} more
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  // ── Month View ────────────────────────────────────────────────────
  const renderMonthView = () => {
    const year = selectedDate.getFullYear();
    const month = selectedDate.getMonth();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const startPad = firstDay.getDay();
    const totalDays = lastDay.getDate();
    const cells: (Date | null)[] = [
      ...Array(startPad).fill(null),
      ...Array.from({ length: totalDays }, (_, i) => new Date(year, month, i + 1)),
    ];

    return (
      <div>
        <div className="grid grid-cols-7 gap-1 mb-1">
          {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => (
            <div key={d} className="text-xs text-muted-foreground text-center font-medium">
              {d}
            </div>
          ))}
        </div>
        <div className="grid grid-cols-7 gap-1">
          {cells.map((day, i) => {
            if (!day) return <div key={i} />;
            const dayStart = new Date(day);
            dayStart.setHours(0, 0, 0, 0);
            const dayEnd = new Date(day);
            dayEnd.setHours(23, 59, 59, 999);
            const dayTasks = tasks.filter(
              (t) =>
                t.start_time >= dayStart &&
                t.start_time <= dayEnd &&
                !t.constraint_applied,
            );
            const isToday = day.toDateString() === now.toDateString();

            return (
              <div
                key={day.toISOString()}
                className={`border rounded p-1 min-h-[60px] cursor-pointer hover:bg-surface-subtle text-center ${
                  isToday ? "border-terra bg-terra/5" : "border-border"
                }`}
                onClick={() => {
                  setSelectedDate(day);
                  setViewMode("day");
                }}
              >
                <div className="text-xs">{day.getDate()}</div>
                {dayTasks.length > 0 && (
                  <div className="flex justify-center gap-0.5 mt-1 flex-wrap">
                    {dayTasks.slice(0, 3).map((t) => (
                      <div
                        key={t.task_id}
                        className="w-1.5 h-1.5 rounded-full"
                        style={{ backgroundColor: t.color }}
                      />
                    ))}
                  </div>
                )}
                {dayTasks.length > 0 && (
                  <div className="text-[10px] text-muted-foreground mt-0.5">
                    {dayTasks.length}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full p-4 max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <CalendarIcon size={20} className="text-terra" />
          <h1 className="text-lg font-semibold">Schedule</h1>
        </div>

        <div className="flex items-center gap-2">
          {/* Date navigation */}
          <button onClick={() => navigateDay(-1)} className="p-1 rounded hover:bg-surface-subtle">
            <ChevronLeft size={16} />
          </button>
          <span className="text-sm font-medium min-w-[140px] text-center">
            {selectedDate.toLocaleDateString([], {
              weekday: "short",
              month: "short",
              day: "numeric",
            })}
          </span>
          <button onClick={() => navigateDay(1)} className="p-1 rounded hover:bg-surface-subtle">
            <ChevronRight size={16} />
          </button>

          {/* View mode toggle */}
          <div className="flex rounded-lg border border-border overflow-hidden ml-2">
            {(["day", "week", "month"] as ViewMode[]).map((mode) => (
              <button
                key={mode}
                onClick={() => setViewMode(mode)}
                className={`px-3 py-1 text-xs font-medium capitalize transition-colors ${
                  viewMode === mode
                    ? "bg-terra text-white"
                    : "hover:bg-surface-subtle"
                }`}
              >
                {mode}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Content */}
      {loading ? (
        <div className="flex items-center justify-center py-20 text-muted-foreground">
          Loading schedule...
        </div>
      ) : tasks.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <CalendarIcon size={40} className="mb-2 opacity-30" />
          <p>No tasks scheduled yet.</p>
          <p className="text-xs mt-1">Start by chatting with Jarvis to build your schedule.</p>
        </div>
      ) : (
        <div className="overflow-auto flex-1">
          {viewMode === "day" && renderDayView()}
          {viewMode === "week" && renderWeekView()}
          {viewMode === "month" && renderMonthView()}
        </div>
      )}

      {/* SM-2 Quality Rating Dialog */}
      {qualityTaskId && (
        <SM2QualityRating
          onRate={(quality) => handleComplete(qualityTaskId, quality)}
          onCancel={() => setQualityTaskId(null)}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add app/\(app\)/schedule/page.tsx lib/transforms.ts
git commit -m "feat: rebuild schedule page with time grid, day/week/month views

- Day view: CSS-positioned task blocks on hourly grid with NOW indicator
- Week view: 7-column grid with condensed task cards, click to drill into day
- Month view: Calendar grid with task count dots, click to drill into day
- Blocked windows render as dashed gray blocks (sleep, lunch, volleyball)
- Complete/skip action buttons on each pending task with SM-2 quality dialog
- Color-coded by goal using colorForGoal()"
```

---

### Task 3: Final integration check for WS3

- [ ] **Step 1: Run TypeScript check**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx tsc --noEmit`
Expected: Clean

- [ ] **Step 2: Run build**

Run: `cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build 2>&1 | tail -5`
Expected: Build succeeds

- [ ] **Step 3: Summary commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add -A
git commit -m "chore: WS3 frontend schedule/calendar rebuild complete

- Time grid layout with hour labels and CSS positioning
- Day/Week/Month view toggles with date navigation
- Blocked windows (sleep, lunch, volleyball) render as hatched blocks
- Complete/skip buttons with SM-2 quality rating
- Demo data with 6 tasks + 3 blocked windows for testing
- Click task → workspace navigation"
```
