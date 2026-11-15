# Memory That Holds: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status (2026-11-16): built and merged.** M1 to M6 are on `main` in both repos; `scripts/memory_check.py` passes all seven steps against the live backend. One fix beyond this plan: a correction in the user's words no longer leaves a second, raw-worded memory beside the cleaned one.

**Goal:** What the user tells Jarvis is found when it is asked about, corrected when the user corrects it, kept for as long as it stays true, and visible and editable by the user.

**Architecture:** The memory architecture stays as it is: memories in Postgres with pgvector (`MemoryRepository`), one service that stores, reinforces, supersedes, recalls and fades them (`MemoryService`, `app/services/memory/memory_service.py`), extraction after each turn (`app/core/observation.py`), stated rules linked to the memory of stating them (`StatedRules`, `app/services/memory/stated_rules.py`), and the context note the model reads (`app/services/memory/context.py`). This plan fixes four behaviours inside that design and adds a check that proves the loop end to end. No new store, no new framework.

**Tech Stack:** Python 3.12+, FastAPI, Supabase Postgres + pgvector, all-MiniLM-L6-v2 embedder (`MiniLMEmbedder`), pytest with `tests/fakes.py` (`FakeSupabase`, `BagOfWordsEmbedder`, `ScriptedLLM`), Next.js 14 + Vitest for the frontend task.

**Spec:** `docs/superpowers/specs/2026-11-14-capstone-lld-and-anticipation-design.md` (memory lifecycle, consent, draft-then-accept). Where this plan and the spec disagree, the spec wins.

**Companion plans:** `2026-11-16-task-appropriate-workspaces.md` (A to E), `2026-11-16-documents-that-drive-the-plan.md` (F to K). This plan is tasks M1 to M6.

## What was measured (2026-11-16, live backend, real model, user `demo`)

| # | What was done | What happened | Verdict |
|---|---|---|---|
| 1 | Said in one message: gym every weekday at 6 PM; Networks professor Dr. Rao gives surprise quizzes on Thursdays | Two memories stored (constraint 0.95, fact 0.9); a rule created for the gym | Works |
| 2 | Same message's reply | "Noted and locked in, sir: 'I go to the gym every weekday at 6 PM'." The professor fact was stored but never acknowledged | **Defect** (M4) |
| 3 | New chat: "When is my gym, and what should I know about Thursdays?" | Gym answered. "As for Thursdays, I hold nothing specific about them." | **Defect** (M1) |
| 4 | New chat: "What do you know about Dr. Rao?" | Correct answer | Works |
| 5 | New chat: "Actually I moved my gym to 7 AM on weekdays, not 6 PM anymore." | Memory superseded correctly. Rules now hold both "I go to the gym every weekday at 6 PM" and "I moved my gym to 7 AM on weekdays, not 6 PM anymore" | **Defect** (M2) |
| 6 | You page, seeded memories | "I think most clearly in the evenings" shows freshness 0.20 after 13 days; below 0.10 a memory leaves recall, so a stated preference is gone in about 16 days | **Defect** (M3) |

Causes, read from the code:
- **Recall miss (3):** `MemoryService.recall` embeds the whole message once and keeps memories with cosine similarity ≥ `MEMORY_RECALL_MIN_SIMILARITY` (0.3). A two-part question averages its meaning, and the Thursday fact falls under the bar. Facts are not in the always-shown profile (only constraints, preferences and goals are).
- **Stale rule (5):** `observation.py` creates a rule for a stated constraint and links it to the new memory (`stated_rules.link`). When the memory supersedes an older one, nothing removes the rule linked to the older memory. The rule's text is the user's raw sentence, not the cleaned memory.
- **Fast fading (6):** every memory starts at `stability` 1.0, which loses a factor of e per week (`Memory.strength_at`). Only repeating or confirming it raises stability (+1.0). Being used does nothing.

## Global Constraints

- Every repository call filters by `user_id`. Another user's memory reads as not found.
- A memory the user forgot or dismissed never comes back: `MEMORY_SUPPRESS_SIMILARITY` (0.85) behaviour must keep passing its existing tests.
- Forgotten memories keep no text. No change here may log or return a forgotten memory's content.
- Services take collaborators in the constructor; only `app/container.py` wires them. No raw `.table(...)` calls outside repositories (`tests/test_layering.py`).
- Model calls go through `route_llm_call`; nothing in this plan adds a model call to recall (recall must stay one embedding batch and one or two repository reads).
- The context note must stay deterministic for the same inputs (ties broken by id): it is the cached prefix of the prompt.
- Settings are read through the service's settings record (`app/core/settings.py`), with constants in `app/core/config.py`.
- Tests: no network, no real model. Names follow `test_{thing}__{condition}__{expected}`. Run `.venv/bin/python -m pytest tests/ -q` before each commit (2203 passing at the start).
- Commit messages: plain description, no co-author or tool attribution lines.

## Review Focus

1. A question in three parts, or with no "and" (e.g. "gym timing? thursday quizzes?"): each part still finds its memory. (M1 tests)
2. A correction worded without the old value ("gym is at 7 AM now"): the old rule still goes. (M2 tests)
3. A correction of a memory that never had a rule (a plain fact): nothing is deleted from rules. (M2 tests)
4. A memory recalled in ten replies in one day is strengthened once, not ten times. (M3 tests)
5. A memory the user dismissed is never strengthened by being similar to a question. (M3 tests)

---

### Task M1: recall finds each thing that was asked about

**Files:**
- Modify: `app/services/memory/memory_service.py` (`recall`, new helper `_query_parts`)
- Modify: `app/core/config.py`, `app/core/settings.py` (new `MEMORY_RECALL_MAX_PARTS: int = 3`, `MEMORY_RECALL_WORD_MATCH_SIMILARITY: float = 0.2`)
- Test: `tests/test_memory_recall.py` (add to the existing recall tests; create the file if recall tests live elsewhere, after `grep -rn "def test_recall" tests/`)

**Interfaces:**
- Consumes: `MemoryRepository.nearest(user_id, embedding, *, statuses, limit, min_similarity) -> list[MemoryMatch]`; `TextEmbedder.embed`.
- Produces: `_query_parts(query: str, max_parts: int) -> list[str]`; `recall(...)` keeps its signature and `Recall` return type.

- [ ] **Step 1: Write the failing tests**

```python
def test_query_parts__two_questions_joined_by_and__whole_then_each_part():
    assert _query_parts("When is my gym, and what should I know about Thursdays?", 3) == [
        "When is my gym, and what should I know about Thursdays?",
        "When is my gym",
        "what should I know about Thursdays",
    ]

def test_query_parts__single_clause__whole_only():
    assert _query_parts("What do you know about Dr. Rao?", 3) == ["What do you know about Dr. Rao?"]

async def test_recall__two_part_question__memory_for_each_part_is_relevant(memory_service):
    await remember_fact(memory_service, "Computer Networks professor is Dr. Rao, who gives surprise quizzes on Thursdays")
    await remember_fact(memory_service, "Hostel mess closes at 9 PM")
    recall = await memory_service.recall(USER, "When is the mess open, and what should I know about Thursdays?")
    contents = [m.content for m in recall.relevant]
    assert any("Thursdays" in c for c in contents) and any("mess" in c for c in contents)

async def test_recall__rare_word_shared_with_a_memory_below_the_bar__still_relevant(memory_service): ...
    # query "anything about Thursdays?", memory mentions "Thursdays", similarity between
    # MEMORY_RECALL_WORD_MATCH_SIMILARITY and MEMORY_RECALL_MIN_SIMILARITY -> included

async def test_recall__same_memory_matched_by_two_parts__listed_once(memory_service): ...
async def test_recall__same_inputs__same_note_bytes(memory_service): ...
```

- [ ] **Step 2: Run them and see them fail** — `.venv/bin/python -m pytest tests/test_memory_recall.py -q` → fails: `_query_parts` not defined.

- [ ] **Step 3: Implement `_query_parts`** — the whole query first, then its clauses split on `?`, `;`, `,` and the word `and`, trimmed, clauses under three words dropped, at most `max_parts` clauses, no duplicates. A single-clause query returns only itself.

- [ ] **Step 4: Change `recall`** — embed all parts (one call to the embedder per part, run with `asyncio.gather`; these are embeddings, not model calls), call `nearest` for each part with `min_similarity=settings.recall_word_match_similarity`, merge by memory id keeping the highest similarity. Keep a match when its similarity is ≥ `recall_min_similarity`, **or** when it is ≥ `recall_word_match_similarity` and the memory shares a content word (4+ letters, not a stop word, case-folded, trailing "s" ignored) with the query. Ranking and budget stay as they are.

- [ ] **Step 5: Run the tests, then the whole suite** — both pass.

- [ ] **Step 6: Commit** — `git commit -m "Recall looks for each part of a question"`.

---

### Task M2: correcting a rule replaces it

**Files:**
- Modify: `app/core/observation.py` (where a stated constraint becomes a rule, near the `stated_rules.link` call)
- Modify: `app/services/memory/stated_rules.py` (new `replace_for_superseded`)
- Modify: `app/services/extraction/behavioral_store.py` only if the rule's text is set there
- Test: `tests/test_stated_rules.py`, `tests/behaviour/test_rule_correction.py`

**Interfaces:**
- Consumes: `RememberOutcome(action, memory)` from `MemoryService.remember`; the superseded memory's id is on the old row (`superseded_by` points at the new one), so `remember` must return it: add `superseded_id: str | None = None` to `RememberOutcome`.
- Produces: `StatedRules.replace_for_superseded(user_id: str, old_memory_id: str) -> list[str]` (ids of the rules deleted, with their check-in trackers, exactly as `forget_memory` deletes them, but without touching the memory).

- [ ] **Step 1: Write the failing tests**

```python
async def test_remember__supersedes__outcome_names_the_old_memory(memory_service): ...
    assert outcome.action == "superseded" and outcome.superseded_id == old.id

async def test_replace_for_superseded__old_memory_had_a_rule__rule_and_its_check_in_deleted(stated_rules, repos): ...
async def test_replace_for_superseded__old_memory_had_no_rule__nothing_deleted(stated_rules, repos): ...
async def test_replace_for_superseded__another_users_memory_id__nothing_deleted(stated_rules, repos): ...

# tests/behaviour: the real chat turn over the fake database and a scripted model
async def test_chat__user_moves_gym_from_6pm_to_7am__one_gym_rule_remains_and_it_is_the_new_one(client): ...
    rules = (await get_you(client))["rules"]
    assert [r["text"] for r in rules if "gym" in r["text"].lower()] == ["Goes to the gym at 7 AM on weekdays"]

async def test_chat__after_the_correction__a_new_plan_keeps_7am_blocked_and_6pm_free(client): ...  # real CP-SAT
```

- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Add `superseded_id` to `RememberOutcome` and set it in `MemoryService._supersede`.**
- [ ] **Step 4: Implement `StatedRules.replace_for_superseded`** by reusing `_delete_linked`.
- [ ] **Step 5: In `observation.py`**, when a stated constraint's outcome is `superseded`, call `replace_for_superseded(user_id, outcome.superseded_id)` before creating and linking the new rule. Store the rule with the memory's cleaned content as its text (the raw sentence stays in the conversation).
- [ ] **Step 6: Run the tests, then the whole suite.**
- [ ] **Step 7: Commit** — `git commit -m "A corrected rule replaces the one it corrects"`.

---

### Task M3: a memory lasts as long as it should

**Files:**
- Modify: `app/services/memory/memory_service.py` (`remember`, new `note_used`), `app/core/config.py`, `app/core/settings.py`
- Modify: `app/services/chat_turn/context.py` or the caller that builds the context note (call `note_used` after recall)
- Modify: `app/domain/ports.py` and both memory repositories only if `note_used` needs a new guarded update (prefer the existing `update`)
- Modify: `scripts/seed_demo_user.py` (nothing to change if it stores through `MemoryService.remember`; confirm its self-check still passes)
- Test: `tests/test_memory_strength.py`

**Interfaces:**
- Produces: `initial_stability(memory_type: MemoryType, provenance: MemorySource) -> float`; `MemoryService.note_used(user_id: str, memories: list[Memory], now: datetime) -> int` (how many were strengthened).

**Values (weeks to lose a factor of e):**

| | said | inferred |
|---|---|---|
| constraint | 16 | 4 |
| goal | 12 | 4 |
| preference | 12 | 3 |
| fact | 8 | 2 |

Constants: `MEMORY_USE_STABILITY_STEP = 0.5`, `MEMORY_USE_INTERVAL_HOURS = 20`.

- [ ] **Step 1: Write the failing tests**

```python
def test_initial_stability__said_preference__12_weeks(): ...
def test_initial_stability__inferred_fact__2_weeks(): ...

async def test_remember__said_preference__after_13_days_strength_at_least_0_8(memory_service, clock): ...
async def test_remember__inferred_fact_never_seen_again__below_the_floor_within_six_weeks(memory_service, clock): ...
async def test_note_used__relevant_memory__strength_reset_and_stability_up_by_half(memory_service, clock): ...
async def test_note_used__same_memory_twice_within_20_hours__strengthened_once(memory_service, clock): ...
async def test_note_used__dismissed_or_forgotten_memory__untouched(memory_service, clock): ...
async def test_note_used__store_fails__recall_still_returns(memory_service): ...  # best effort, logged
```

- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement `initial_stability`** and use it in `remember` when a new memory is created (a repeat or a confirmation still adds 1.0, capped at the entity's 20).
- [ ] **Step 4: Implement `note_used`** — for each memory in `recall.relevant` (not the profile, which is shown on every turn): if it is active and its `strength_as_of` is more than `MEMORY_USE_INTERVAL_HOURS` before `now`, set strength to 1.0 as of `now` and add `MEMORY_USE_STABILITY_STEP` to stability (capped). Best effort: a failure is logged by type and never fails the turn.
- [ ] **Step 5: Call `note_used`** after the context note is built, off the reply's critical path (the same background pattern `ConversationMemoryService.compact_in_background` uses).
- [ ] **Step 6: Run the tests, the seed tests (`tests/test_seed_demo_user.py`), then the whole suite.**
- [ ] **Step 7: Commit** — `git commit -m "Stated memories last for months; using a memory keeps it"`.

---

### Task M4: the user sees what was remembered, and can undo it (frontend)

**Files (in `jarvis-frontend`):**
- Modify: the chat message component that handles stream frames (find with `grep -rn "memory_extracted" app components lib`)
- Create: `components/app/RememberedLine.tsx`, `lib/remembered.ts`
- Modify: `lib/api.ts` only if a forget call is not already exported (`grep -n "memories" lib/api.ts`)
- Test: `lib/__tests__/remembered.test.ts`

**Interfaces:**
- Consumes: the `memory_extracted` frame (`memory_id`, `type`, `content`, `provenance`, `action`: created, reinforced or superseded), already sent by the backend; `DELETE /api/v1/memories/{memory_id}`.
- Produces: `rememberedLines(frames) -> { memoryId, text, verb }[]` where verb is "Remembered" (created), "Updated" (superseded); reinforced memories are not listed.

- [ ] **Step 1: Write the failing tests** for `rememberedLines`: two created frames give two lines in order; a reinforced frame gives none; a superseded frame reads "Updated"; a frame with the same `memory_id` twice is listed once.
- [ ] **Step 2: Run and see them fail** — `npm test`.
- [ ] **Step 3: Implement `rememberedLines` and `RememberedLine`** — under the reply: "Remembered: Computer Networks professor is Dr. Rao, who gives surprise quizzes on Thursdays" with an "Undo" button that forgets it and then shows "Forgotten". One line per memory; this is how a message carrying two facts acknowledges both.
- [ ] **Step 4: `npm test && npm run lint && npx tsc --noEmit`.**
- [ ] **Step 5: Commit** — `git commit -m "Chat shows what was remembered, with undo"`.

---

### Task M5: the You page lets the user correct a memory (frontend + small backend check)

**Files:**
- Modify: `jarvis-frontend/app/(app)/you/page.tsx`, `jarvis-frontend/lib/api.ts`, `jarvis-frontend/lib/types.ts`
- Test: `jarvis-frontend/lib/__tests__/api-memories.test.ts`
- Backend: `MemoryService.edit` exists; confirm its route with `grep -n "edit\|patch" app/api/v1/endpoints/memories.py`. If there is no route, add `PATCH /api/v1/memories/{memory_id}` (typed request with `content: str`, `max_length=2000`; typed response) with tests in `tests/test_api_contract.py` and `tests/test_routes.py`.

- [ ] **Step 1: Write the failing test** for `editMemory(memoryId, content)`: sends `PATCH` with the user id and the content; a 404 raises the API error type the other calls raise.
- [ ] **Step 2: Implement `editMemory`**, and on the You page an "Edit" action beside Confirm and Forget: the text becomes an input, Save calls `editMemory`, the row refreshes.
- [ ] **Step 3: Group the memories list** under "What you told me" (said) and "What I noticed" (inferred); show "last used 3 days ago" when the API returns it (skip the label when it does not).
- [ ] **Step 4: Frontend checks pass; backend suite passes if a route was added.**
- [ ] **Step 5: Commit** — `git commit -m "You page: edit a memory; said and noticed are grouped"`.

---

### Task M6: one check that proves the loop (do this first; it is the safety net for M1 to M3)

**Files:**
- Create: `tests/behaviour/test_memory_loop.py`
- Create: `scripts/memory_check.py` (live, like `scripts/live_e2e.py`; not part of the offline suite)

**Interfaces:**
- Consumes: the chat stream route, `GET /api/v1/you`, `DELETE /api/v1/memories/{id}`.

- [ ] **Step 1: Write the behaviour test** (real chat turn, fake database, scripted model). One test per row, each asserting on stored state and on the context note the model was given (`ScriptedLLM` records its prompts), not on the wording of a reply:

```python
async def test_loop__said_in_one_chat__in_the_context_note_of_a_new_chat(client, llm): ...
async def test_loop__two_part_question__both_memories_in_the_note(client, llm): ...          # red until M1
async def test_loop__correction__one_rule_and_old_memory_not_in_the_note(client, llm): ...    # red until M2
async def test_loop__thirty_days_later__stated_preference_still_in_the_note(client, llm, clock): ...  # red until M3
async def test_loop__forgotten__not_in_the_note_and_not_stored_again_when_repeated(client, llm): ...
async def test_loop__another_user__sees_none_of_it(client, llm): ...
```

Mark the three that are red today with `pytest.mark.xfail(strict=True, reason="M1")` (and M2, M3); the task that fixes each one removes its marker.

- [ ] **Step 2: Write `scripts/memory_check.py`** — against a running backend and a throwaway user id (`memcheck_<8 hex>`, never `demo`): say two facts, ask a two-part question in a new conversation, correct one fact, ask again, forget one, ask again. Print PASS or FAIL per step from stored state (`/you`) and from the `loading_context` frame's memory count, and delete the throwaway user's memories and rules at the end. Exit 1 on any failure.
- [ ] **Step 3: Run** — `.venv/bin/python -m pytest tests/behaviour/test_memory_loop.py -q` → 3 passed, 3 xfailed.
- [ ] **Step 4: Commit** — `git commit -m "Memory loop: behaviour tests and a live check"`.

---

## Beyond (ideas to record, not to build in this plan)

- **Memories from documents:** a syllabus or timetable yields facts worth remembering (who teaches what, exam dates). Offer them as suggestions the user accepts, never store them silently. Belongs with package H of the documents plan.
- **People and courses as things:** "Dr. Rao", "Computer Networks" as entities that memories attach to, so "what do I know about my Networks course" gathers everything.
- **"Why did you say that?"** on a reply: the memories that were in its context note.
- **A weekly review:** "Three things I learned about you this week: keep, fix or forget."

## Order and owners

| Task | Needs | Size | Suggested owner |
|---|---|---|---|
| M6 loop check | none | half a day | backend teammate 1 |
| M1 recall | M6 | half a day | backend teammate 1 |
| M2 corrected rule | M6 | half a day | backend teammate 2 |
| M3 lasting memories | M6 | half a day | backend teammate 2 |
| M4 remembered line | none | half a day | frontend teammate |
| M5 You page edit | none | half a day | frontend teammate |

M1, M2 and M3 each touch a different part of `memory_service.py` or its callers; merge them one at a time and run the whole suite after each.
