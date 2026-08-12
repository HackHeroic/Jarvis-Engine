# ✅ Jarvis Preflight — What We Test, What Should Happen, Exact Commands

Run top to bottom. Every step says **what it proves** and **exactly what you should see**.

---

## 0. Start the servers

```bash
# Terminal 1 — backend (ALWAYS .venv/bin — system python crashes on OR-Tools)
cd ~/Jarvis-cursor/Jarvis-Engine
.venv/bin/uvicorn app.main:app --reload --port 8000
```

**What should happen:** startup logs end with either
- `PRIMARY → openai/google/gemma-...` — a local model is loaded in LM Studio (best), or
- `⚠️ No usable model loaded in LM Studio — all LLM calls will use Gemini cloud` — fallback mode (everything still works).

```bash
# Terminal 2 — frontend
cd ~/Jarvis-cursor/jarvis-frontend
npm run dev
```

**What should happen:** `Ready` on http://localhost:3000. ⚠️ **Never run `npm run build` while this is up** — it corrupts the dev server (symptom: pages 500 with `Cannot find module './NNN.js'`; fix: kill dev → `rm -rf .next` → `npm run dev`).

---

## 1. Test suite — proves the whole backend contract

```bash
cd ~/Jarvis-cursor/Jarvis-Engine
.venv/bin/python -m pytest tests/ -q
```

**Expect:** `480 passed, 1 xfailed` in ~7s. The single xfail is a *documented known bug* (single-task plans hit a pacing floor), not a failure. Suite is fully offline — a socket guard fails any test that tries to reach the network.

---

## 2. Health + API surface

```bash
curl -s localhost:8000/health
```
**Expect:** `{"status":"healthy","database":"connected"}` — Supabase reachable.

Open **http://localhost:8000/docs** in a browser.
**Expect:** the API explorer; `/api/v1/chat` and `/api/v1/chat/stream` are marked *deprecated*; `/api/v1/chat/v2/stream` is the live entry point.

---

## 3. Fast path — proves latency-aware routing (no LLM on trivial input)

```bash
curl -s -N -X POST localhost:8000/api/v1/chat/v2/stream \
  -H 'Content-Type: application/json' \
  -d '{"user_prompt":"hi","user_id":"demo"}'
```

**Expect:** SSE frames within ~1–2s, ending in an `event: complete` whose JSON has `"intent": "CHAT"` and a greeting like *"Good to see you, sir. All systems are nominal."* No decomposition, no solver — a regex fast path answered.

---

## 4. Behavioral constraint — proves "memories change the math"

```bash
curl -s -N -X POST localhost:8000/api/v1/chat/v2/stream \
  -H 'Content-Type: application/json' \
  -d '{"user_prompt":"I never want to study before 11am","user_id":"demo"}'
```

**Expect:** `"intent": "BEHAVIORAL_CONSTRAINT"`, message *"Noted and locked in, sir: …"*, and crucially `"saved_constraints": ["I never want to study before 11am"]` in the complete frame.

Verify it's queryable (this is what the Habits page reads):

```bash
curl -s "localhost:8000/api/v1/habits/constraints?user_id=demo"
```
**Expect:** JSON with your constraint row (`raw_text`, `constraint_type: "habit"`).

---

## 5. Full planning pipeline — the money path

```bash
curl -s -N -X POST localhost:8000/api/v1/chat/v2/stream \
  -H 'Content-Type: application/json' \
  -d '{"user_prompt":"prepare a 15-minute technical presentation on my project by Friday","user_id":"demo"}'
```

**What's being tested:** brain-dump extraction → intent PLAN_DAY → planning sub-graph (goal gate → habit translation ∥ memory-constraints → LLM decomposition → CP-SAT solve) → draft creation.

**Expect (30–90s):** a stream of `event: phase` / `event: step` frames (extracting → decomposing → scheduling), then `event: complete` with:
- `"intent": "PLAN_DAY"`
- `"draft_id": "<uuid>"` — **non-null** (this was fabricated before the rebuild; now it's real)
- `"schedule"` containing 5–8 tasks whose **titles reference your goal** (e.g. "Outline Demo Narrative…"), each ≤25 min
- **no task before 11am** (your constraint became a solver block)
- `"conversation_id": "<uuid>"` → **copy this — you need it for step 6.**

---

## 6. Accept → persistence — proves the negotiation loop + honest writes

```bash
# PASTE the conversation_id from step 5 — a made-up one lands on a fresh
# thread with no active negotiation and "accept" becomes ordinary chat!
curl -s -N -X POST localhost:8000/api/v1/chat/v2/stream \
  -H 'Content-Type: application/json' \
  -d '{"user_prompt":"accept","user_id":"demo","conversation_id":"PASTE-HERE"}'
```

**Expect:** `"intent": "ACCEPT_DRAFT"`, message *"Locked in, sir — N tasks are on your schedule."* The accept only says this after re-querying the DB by the write's plan_id — if persistence had failed, it would say so and keep the draft retryable.

Also try **`reject`** or **"looks good but move the practice run later"** on a fresh draft: reject discards it; edit-phrasing keeps negotiation open and asks what to change. And the safety net: *"I need to confirm my exam registration"* while a draft is open must **NOT** accept (hardened matcher — 100+ adversarial probes).

---

## 7. Task lifecycle — completion triggers replan

Get a task id (or click ✓ in the Schedule page):

```bash
curl -s "localhost:8000/api/v1/tasks?user_id=demo" | head -c 400   # if listing exists; else read task_id from step 5's schedule payload
curl -s -X POST "localhost:8000/api/v1/tasks/<TASK_ID>/complete" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","actual_duration_minutes": 20}'
```

**Expect:** `"status": "completed"`, `"replan_triggered": true` — remaining schedule re-solves in the background, and PEARL pattern-mining runs on the completion.
(Note: `user_id` goes **in the body** — as a query param you'll get a 422.)

---

## 8. Restart survival — the checkpointer wow

1. In Terminal 1: Ctrl-C the backend, start it again.
2. Re-send the step-6 curl (same `conversation_id`).

**Expect:** a reply that still *knows the conversation* (references the plan / says there's nothing awaiting review). Negotiation + history live in `data/checkpoints.sqlite` under user-scoped threads — the process is disposable, the state isn't.

---

## 9. Resilience — dependencies are allowed to die

```bash
# DB-down: chat must degrade, never 500
SUPABASE_URL=https://dead.invalid .venv/bin/uvicorn app.main:app --port 8001 &
sleep 6
curl -s -N -X POST localhost:8001/api/v1/chat/v2/stream \
  -H 'Content-Type: application/json' -d '{"user_prompt":"hi","user_id":"demo"}' | tail -2
kill %1
```

**Expect:** a normal SSE reply (memory-less), **not** an error — stores degrade to empty context when the DB is unreachable. Similarly: LM Studio down → automatic Gemini fallback (with a PII regex gate before anything leaves the machine); solver infeasible → anti-guilt message, never a stack trace.

---

## 10. Frontend walkthrough (browser, http://localhost:3000)

| Page / action | What should happen |
|---|---|
| `/chat` → type the step-3/4/5 prompts | Phase trace streams with the **cycling verb** on the active line (completed lines freeze); tokens render progressively; constraint turns show the green **"Constraint saved"** card; plans show the draft review |
| type `accept` in the same chat | "Locked in" + tasks appear on `/schedule` |
| `/schedule` day view | tasks laid out by time; overlapping tasks render **side by side**; ✓ completes (with strikethrough), skip is blame-free |
| `/habits` | **Active constraints** section on top listing your saved rules; SM-2 trackers below |
| `/documents` | upload a PDF → Docling ingestion → classified + linked to matching tasks |
| `/dashboard`, `/` | render clean (200) |

---

## If something breaks tomorrow

- **Chat 500s / DB errors** → check `curl localhost:8000/health`; if DB down, chat still works memory-less — demo the resilience instead.
- **"accept" acts like chat** → you're on the wrong `conversation_id` (or negotiation already concluded). Re-plan and accept in the same UI conversation — the frontend handles the threading automatically.
- **Slow first LLM call** → warm up before the interview (one plan run).
- **Frontend 500 `Cannot find module './NNN.js'`** → `rm -rf .next && npm run dev`.
