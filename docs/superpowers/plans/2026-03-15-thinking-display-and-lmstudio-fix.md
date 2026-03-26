---
name: thinking display and LM Studio fix
overview: Fix thinking_process never showing in Live mode + UNKNOWN intent badge
todos: []
isProject: false
---

# Plan: Fix thinking_process + UNKNOWN intent (Live mode)

## Context

Two interrelated bugs cause thinking never to appear in Live mode:

1. `**voice_of_jarvis.py` fallback is broken** — when the LM Studio call throws (timeout, connection error, wrong model name), the `except` block only prints the error, then falls through to dead-code that only handles `spread_across_days`. For the normal PLAN_DAY path, the function hits an unreachable return and falls off the end → returns `None`. The caller (`_run_plan_day_flow`) does `message, thinking_process = await synthesize_jarvis_response(summary)` which throws `TypeError: cannot unpack NoneType`. This propagates as a 500 error. The frontend catches it, creates a message with NO `response` field → `ResponseLayers` shows `"UNKNOWN"` badge.
2. **Frontend shows `"UNKNOWN"` badge on error messages** — when the backend returns 500, ChatPanel creates `{ role: "assistant", content: "Error: ...", /* no response */ }`. `ResponseLayers` reads `response?.intent ?? "UNKNOWN"` → shows badge "UNKNOWN" even for pure error messages.
3. **Minor: `intent = "MULTI"`** — one control_policy fallback path sets an invalid intent string.

## Diagnosis: LM Studio + thinking

- `LOCAL_LLM_URL = "http://127.0.0.1:1234/v1"` ✓ correct
- `LOCAL_LLM_MODEL = "openai/mlx-community/qwen3.5-27b"` (default, not in .env) — LM Studio ignores the model field and uses whatever is loaded, so this is OK
- `SLM_ROUTER_MODEL = "openai/qwen3.5-4b"` (default) — same
- Voice of Jarvis prompt EXPLICITLY instructs the model: `"First, inside <think>...</think> tags, write 2-4 sentences..."`
- If the 4B model ignores this instruction, `_build_thinking_fallback` provides a deterministic fallback
- **The real issue is not LM Studio config** — it's that when LM Studio is slow/unreachable, `synthesize_jarvis_response` returns `None` causing a 500 before thinking can be set

---

## Files to Modify

- `Jarvis-Engine/app/services/analytical/voice_of_jarvis.py`
- `Jarvis-Engine/app/services/analytical/control_policy.py`
- `jarvis-demo/components/ResponseLayers.tsx`

---

## Fix 1: `voice_of_jarvis.py` — fix broken exception fallback

**Current broken code (lines 121–133):**

```python
    except Exception as e:
        print(f"[Voice of Jarvis] Synthesis failed: {e}")
    if execution_summary.get("spread_across_days"):   # ← outside except!
        thinking = _build_thinking_fallback(execution_summary)
        return (                                       # ← function can stop here
            "I've spread this across multiple days...",
            thinking,
        )
        if execution_summary.get("schedule_generated"):   # ← DEAD CODE
            return "Here's your schedule.", None          # never reached
        ...
```

**Fixed code:**

```python
    except Exception as e:
        print(f"[Voice of Jarvis] Synthesis failed: {e}")
        # Always return a valid tuple from the fallback path
        thinking = _build_thinking_fallback(execution_summary)
        if execution_summary.get("spread_across_days"):
            return "I've spread this across multiple days to fit your constraints. Here's your schedule.", thinking
        if execution_summary.get("schedule_generated"):
            return "Here's your schedule.", thinking
        if execution_summary.get("habits_saved"):
            return "I've noted your preferences. I'll apply them to your next plan.", thinking
        if execution_summary.get("knowledge_ingested"):
            return "I've processed and stored your materials.", thinking
        if execution_summary.get("calendar_extracted"):
            return "I've extracted your timetable for review.", thinking
        return "Done.", thinking
```

Remove lines 123–133 (the old outside-except dead code block).

---

## Fix 2: `control_policy.py` — fix `intent = "MULTI"`

**Line 907–909:**

```python
    else:
        intent = "MULTI"          # ← invalid, frontend has no style for it
        suggested = None
```

**Fix:**

```python
    else:
        intent = IntentType.GREETING.value  # graceful fallback for unrecognized multi-intent
        suggested = None
```

---

## Fix 3: `ResponseLayers.tsx` — don't show "UNKNOWN" badge on error messages

**Current:**

```ts
const intent = response?.intent ?? "UNKNOWN";
// always renders badge
```

**Fix:**

- If `!response`, skip the badge and all sections (the error message text is already displayed by ChatPanel)
- Show a subtle error indicator instead

```tsx
// If no response object (error message), render plain text only
if (!response) {
  return <p className="text-slate-100 whitespace-pre-wrap">{content}</p>;
}
const intent = response.intent ?? "GREETING";
```

This means error messages (where `msg.response` is undefined) get plain text display, no badge.

---

## Visual result after fix

**On LM Studio success (thinking tags present):** badge + `<think>` content → full pipeline stages shown
**On LM Studio success (no think tags, fallback):** badge + deterministic fallback thinking → "I broke down your goal into micro-tasks..."
**On LM Studio failure (connection refused/timeout):** catches exception → returns fallback tuple → frontend shows badge + fallback thinking text. No more 500 error.
**On error (rare — some other unhandled path):** plain error text, no confusing "UNKNOWN" badge

---

## Verification

1. Start LM Studio with Qwen3.5-4b loaded
2. Start backend: `cd Jarvis-Engine && uvicorn app.main:app --reload --port 8000`
3. Switch frontend to Live mode
4. Send "Plan my day to study for Advanced Discrete Maths"
5. Expected: thinking section shows (either LLM `<think>` content OR fallback text), intent shows as `PLAN_DAY`
6. Stop LM Studio (simulate failure), send a message
7. Expected: graceful message returned (not 500), fallback thinking present
8. Previously broken "UNKNOWN" badge on error — should no longer appear

