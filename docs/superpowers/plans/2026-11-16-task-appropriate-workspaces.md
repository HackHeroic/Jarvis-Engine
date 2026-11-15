# Task-Appropriate Workspaces Implementation Plan

**Goal:** When a task's workspace opens, everything in it is for that task: the right kind of help for the kind of work, drawn only from documents that are actually about it, and shown as something to use, not a block of text.

**Why:** Today every task gets the same recipe. "Course project: write the README" was given a quiz, labelled "From your syllabus", built from an unrelated marks transcript (the model itself wrote "the supplied materials contain no README instructions"). A writing task can also be handed LeetCode links. This is the gap between a planner and Jarvis: the workspace should show it understood the task.

**Companion plan:** `2026-11-16-documents-that-drive-the-plan.md` (packages F to K) covers where uploaded files live (this chat, a project, general) and turning an exam timetable plus a syllabus into a calibrated study plan. This plan only covers what a single task's workspace shows.

**Already done (2026-11-16):** quizzes and excerpts render as formatted markdown (`jarvis-frontend/components/app/Markdown.tsx`). Do not redo it; reuse that component.

**Architecture:** One backend module builds the workspace: `app/services/analytical/workspace_builder.py` (`build_task_workspace`). It fetches material chunks, runs a web search, generates practice assets, and returns `TaskWorkspace` (`app/schemas/workspace.py`). The frontend page is `jarvis-frontend/app/(app)/workspace/[taskId]/page.tsx`. The five packages below are independent unless a "needs" line says otherwise, so each can be one person's work.

## Global constraints

- Layering: services never touch Supabase tables directly; they take repository ports (`app/domain/ports.py`). `tests/test_layering.py` enforces it.
- Every model call goes through `route_llm_call`; every web search through `_search_web`. No direct SDK calls.
- Model output is parsed through a Pydantic model before any field is used. A parse failure means "no asset", never raw text shown to the user.
- No `print`; use `logger`. Log exception types, not exception text.
- Tests never call a real model or the network: script the model (`tests/fakes.py`, `ScriptedLLM`). Test names follow `test_{thing}__{condition}__{expected}`.
- The workspace is cached for 24 hours (`workspace_cache`). Any change to what is built must still pass through `get_or_build_task_workspace`; when the response shape changes, old cached rows must not break the page (new fields need defaults).
- Run before every commit: backend `.venv/bin/python -m pytest tests/ -q` (2203 passing today), frontend `npm test && npm run lint && npx tsc --noEmit`.
- Commit messages: plain description, no co-author or tool attribution lines.

---

## Package A: the workspace fits the kind of task (backend)

**Problem:** `generate_practice_assets` always asks for a quiz when there are chunks, and otherwise always searches for "2 LeetCode or Codeforces problems + 1 YouTube", whatever the task is.

**Files**
- Create: `app/services/analytical/task_kind.py`
- Modify: `app/services/analytical/workspace_builder.py` (`build_task_workspace`, `generate_practice_assets`, `perform_learning_style_search`)
- Modify: `app/schemas/workspace.py` (add `task_kind: str = "study"` to `TaskWorkspace`)
- Test: `tests/test_task_kind.py`, `tests/test_workspace_recipes.py`

**Design**
- `TaskKind` enum: `PRACTICE` (solve problems), `STUDY` (revise, review, flashcards, read), `WRITE` (report, README, essay, documentation), `BUILD` (implement, code a project or assignment), `ADMIN` (submit, email, register, pay).
- `classify_task(title: str, topic_keywords: list[str], goal_id: str | None) -> TaskKind`: deterministic, no model call. Match verbs and nouns in the title (table in the module, e.g. "write/draft/report/README" → `WRITE`; "practice/solve/mock interview/problems" → `PRACTICE`; "submit/send/register" → `ADMIN`; "implement/build/simulator" → `BUILD`). Default `STUDY`.
- A recipe per kind (Strategy pattern: one small class per kind behind a `WorkspaceRecipe` protocol with `async def assets(ctx) -> list[StudyAsset]`):

| Kind | From documents | Generated | Web |
|---|---|---|---|
| PRACTICE | excerpts if relevant | quiz or practice problems | problem links (LeetCode/Codeforces only when the topic is DSA/programming) + one tutorial |
| STUDY | excerpts | quiz | one resource matched to learning style |
| WRITE | excerpts if relevant | an outline with section headings and a short checklist (`generated_content`) | one guide or good example |
| BUILD | excerpts if relevant | a step checklist: smallest first step, then the rest (`generated_content`) | one reference or documentation link |
| ADMIN | none | a 3-5 item checklist of what to have ready (`generated_content`) | none |

**Tests (minimum)**
- `test_classify_task__write_the_readme__write`, `__dp_practice_knapsack__practice`, `__submit_the_quiz_answers__admin`, `__page_replacement_simulator__build`, `__unknown_wording__study`.
- `test_workspace__write_task__no_quiz_and_no_problem_links`.
- `test_workspace__admin_task__no_web_search_is_made` (assert `_search_web` was not called).
- `test_workspace__practice_task_not_about_programming__no_leetcode_links`.

**Done when:** opening "Course project: write the README" shows an outline and a guide link, and no quiz or LeetCode link; "DP practice: knapsack and its variants" still shows problems.

---

## Package B: only material that is about the task (backend)

**Problem:** `fetch_chunks_for_task` falls back to a semantic search over all of the user's documents when the task has no linked document, and whatever comes back is treated as "your syllabus". Excerpts are titled "Material excerpt 1" with no source.

**Files**
- Modify: `app/services/analytical/workspace_builder.py` (`fetch_chunks_for_task`, the excerpt loop in `build_task_workspace`, the quiz prompt)
- Modify: `app/utils/chroma_client.py` only if a stricter cut-off helper is needed
- Test: `tests/test_workspace_materials.py`

**Design**
- Return material with its source: `fetch_chunks_for_task` returns `list[MaterialChunk]` (`text`, `source_id`, `file_name`) instead of `list[str]`.
- Linked documents first (`DocumentRepository.links_for_task`, already used by `DocumentService.linked_documents`). Within a linked document, rank its chunks against the task title and keep the best 5, instead of taking the first 5 of the file.
- The unlinked fallback stays, but with a stricter relevance bar than general search (start at cosine similarity ≥ 0.45 with the real embedder and tune on the demo data); if nothing passes, return no chunks. No material is a valid, honest result.
- Excerpt titles name the source: `"{file_name}: excerpt {n}"`. `metadata={"source_id": ...}` so the page can link to the document viewer.
- The quiz rationale names the document (`"From DBMS-Syllabus-CS304.pdf"`), never the fixed text "From your syllabus".
- The quiz prompt says: if the material is not about the task, return `{"assets": []}`. An empty list shows no quiz.

**Tests (minimum)**
- `test_materials__no_linked_document_and_nothing_relevant__no_chunks_and_no_quiz`.
- `test_materials__linked_document__excerpts_named_after_the_file_with_source_id`.
- `test_materials__another_users_document__never_returned` (data isolation).
- `test_quiz__model_says_material_is_unrelated__no_quiz_asset`.

**Done when:** with only a marks transcript uploaded, the README task's workspace shows no excerpt and no quiz; with the DBMS syllabus uploaded, "Revise DBMS normalisation" shows excerpts named after the syllabus.

---

## Package C: a quiz is data, not a text blob (backend) — needs nothing; Package D builds on it

**Problem:** the quiz is markdown inside a JSON string inside a model reply. When the JSON does not parse, the raw reply (up to 4,000 characters) is shown as the quiz.

**Files**
- Modify: `app/schemas/workspace.py` (add `QuizQuestion`, `Quiz`)
- Modify: `app/services/analytical/workspace_builder.py` (`generate_practice_assets`)
- Test: `tests/test_workspace_quiz.py`

**Design**
- `QuizQuestion(question: str, answer: str, hint: str = "")`, `Quiz(title: str, questions: list[QuizQuestion])` with 3 to 5 questions.
- Ask the model for exactly that JSON and parse with `Quiz.model_validate_json` (strip markdown fences first; the project already does this for planning output, reuse that helper).
- The asset keeps `asset_type="generated_quiz"`; `metadata={"quiz": quiz.model_dump()}`; `content_or_url` holds the same quiz as markdown, so an older frontend and cached rows still display.
- On a parse failure: log the exception type and return no quiz. Remove the branch that shows the raw reply.

**Tests (minimum)**
- `test_quiz__valid_reply__asset_carries_structured_questions_and_markdown`.
- `test_quiz__reply_in_markdown_fences__parsed`.
- `test_quiz__malformed_reply__no_asset_and_no_raw_text`.
- `test_quiz__two_questions_only__rejected`.

**Done when:** `GET /api/v1/tasks/{task_id}/workspace` returns a quiz asset whose `metadata.quiz.questions` is a list, and a malformed model reply produces no quiz.

---

## Package D: a workspace you can use (frontend) — best after C, works without it

**Problem:** the page is one flat list of cards. A quiz shows its answers next to the questions. Excerpts do not say where they came from.

**Files**
- Modify: `app/(app)/workspace/[taskId]/page.tsx`
- Create: `components/app/QuizCard.tsx`, `lib/workspace.ts` (pure helpers, tested)
- Modify: `lib/types.ts` (`Quiz`, `QuizQuestion`, `task_kind`)
- Test: `lib/__tests__/workspace.test.ts`

**Design**
- `lib/workspace.ts`: `groupAssets(assets)` → `{ fromDocuments, practice, links }`; `quizOf(asset)` → the structured quiz from `metadata.quiz`, or `null`.
- Sections in this order, each hidden when empty: "From your documents" (linked documents, then excerpts), "Practice" (quiz, generated outline or checklist), "Read or watch" (links).
- `QuizCard`: one question at a time or a list, each with a "Show answer" button; answers hidden until asked. A count of how many were revealed is enough; no scoring.
- When `quizOf` returns `null`, fall back to `<Markdown>` of `content_or_url` (the current behaviour).
- An excerpt with `metadata.source_id` gets an "Open document" link to `/documents/{source_id}`.
- Empty state when there are no assets at all: one line saying nothing was found for this task and a link to upload a document.

**Tests (minimum)**
- `groupAssets` puts each asset type in the right group and keeps order.
- `quizOf` returns `null` for a missing or malformed `metadata.quiz`.

**Done when:** the DBMS workspace shows three labelled sections, and quiz answers appear only after "Show answer".

---

## Package E: the task's own details and a clean builder (backend + small frontend)

**Problem 1:** "Completion criteria" shows the task title again. The page guesses criteria from `primary_objective` or `localStorage`; the task's real `completion_criteria` (e.g. "Normalise one schema to 3NF from memory") is never sent. The subtitle also repeats the title.

**Problem 2:** `workspace_builder.py` reads `user_tasks` and `task_materials` straight from Supabase and uses `print`, against the project's layering rules.

**Files**
- Modify: `app/schemas/workspace.py` (`completion_criteria: str = ""`, `duration_minutes: int | None = None`, `deadline_hint: str | None = None`)
- Modify: `app/services/analytical/workspace_builder.py` (take `TaskRepository` and `DocumentRepository`; remove `_get_supabase` reads of `user_tasks` and `task_materials`; `print` → `logger`)
- Modify: `app/container.py` (pass the repositories where the builder is wired)
- Modify: `jarvis-frontend/app/(app)/workspace/[taskId]/page.tsx` (show `completion_criteria`; drop the subtitle when it equals the title)
- Test: `tests/test_workspace_task_details.py`; `tests/test_layering.py` must still pass

**Design**
- Find where a task's `completion_criteria` is stored first (`TaskChunk.completion_criteria` in `app/schemas/decomposition.py`; check `user_tasks` columns in `docs/capstone/08-database-schema.md`). If it is not persisted on the task row, add a nullable column with a migration and write it in `accept_draft` (follow `supabase/migrations/20261115000007_user_tasks_not_before.sql` as the pattern for adding a column and restating the function).
- The task is read once through `TaskRepository` (scoped by `user_id`) and used for the title, keywords, criteria, WOOP card and goal, instead of two separate table reads.

**Tests (minimum)**
- `test_workspace__task_with_criteria__criteria_returned`.
- `test_workspace__another_users_task_id__not_found_not_their_title`.
- `test_workspace__repository_fails__typed_error_not_500`.

**Done when:** the workspace of "Revise DBMS normalisation" shows "Normalise one schema to 3NF from memory" under Completion criteria, and `grep -n "supabase.table\|print(" app/services/analytical/workspace_builder.py` returns nothing for task or material reads.

---

## Review focus

Things no single package's tests cover; whoever merges last checks them by hand:

1. A cached workspace built before these changes still opens (old rows lack `task_kind`, `metadata.quiz`, `completion_criteria`).
2. A task whose title matches two kinds ("Write and submit the lab report") gets a sensible recipe (`WRITE` wins over `ADMIN`).
3. The Anthropic key is missing or expired: the workspace still opens with documents and a clear notice, no error page.
4. A document is deleted while its task's workspace is cached: the excerpt's "Open document" link must not lead to a broken page (`invalidate_workspaces` is already called on delete; confirm it covers the new excerpt links).
5. A very long quiz answer or a table in an excerpt does not overflow the card on a phone-width screen.

## Suggested split

| Person | Package | Size |
|---|---|---|
| 1 | A (task kinds and recipes) | largest, about a day |
| 2 | B (relevant material only) then C (structured quiz) | about a day |
| 3 | D (frontend) then the frontend part of E | about a day |
| Madhav | backend part of E, merge order B → C → A → E → D, final check of the review focus | half a day |

Each package is its own branch and pull request against `main`.
