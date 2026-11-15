# Documents That Drive the Plan: Implementation Plan

**Goal:** Two things that today are missing.

1. **Files have a home.** A file uploaded in a chat belongs to that chat; a file can also belong to a project (a subject, an exam season) or to the general library. Jarvis looks in the nearest place first.
2. **Uploading is enough.** "My end sems start on Dec 15" plus the exam timetable and the syllabus is all the user gives. Jarvis works out the subjects, the dates and the units, proposes a calibrated study plan for the weeks left, schedules it around the user's rules and strong hours, fills each task's workspace from the syllabus, and keeps the plan honest as days are missed.

**Why:** This is the Jarvis promise: it reads what you hand it and does the thinking. Today an uploaded syllabus is stored and searched, and its tasks get linked to it, but nothing is planned from it (`handle_syllabus` in `app/services/documents/registry.py` only logs: "Task creation deferred"). A timetable becomes class slots awaiting approval, but exam dates in it become nothing.

**Companion plan:** `2026-11-16-task-appropriate-workspaces.md` (packages A to E) makes each task's workspace fit the task. This plan (packages F to K) feeds it: the tasks created here carry the topics and source documents those workspaces use.

## What exists today (build on it, do not replace it)

| Piece | Where | What it does now |
|---|---|---|
| Upload in chat or on the Documents page | `POST /api/v1/chat/stream` (`file_base64`), `POST /api/v1/ingestion/process` | Reads the file with Docling |
| Semantic router | `app/services/extraction/orchestrator.py` (`process_ingestion`) | Classifies into `CALENDAR_SYNC`, `KNOWLEDGE_INGESTION`, `BEHAVIORAL_CONSTRAINT`, `ACTION_ITEM` |
| Timetable → class slots | `calendar_extractor.py`, `pending_calendar_updates`, approve/reject routes | Slots wait for the user's approval |
| Document record, file, chunks | `DocumentService`, `DocumentRepository`, `DocumentFileStore`, ChromaDB | One flat list per user |
| Document ↔ task links | `DocumentLinker`, `DocumentLinkListener` | Links by meaning, both directions |
| Deadlines learned from documents | `GoalDeadlineUpdate`, `GoalRepository` | Stored, shown as context |
| Planning | `app/modules/planning_graph.py`, `DraftService`, `run_schedule`, `not_before` | Goal text → tasks → CP-SAT schedule → draft → accept |
| Proactive suggestions | `app/services/anticipation/` (`MorningBriefTrigger`, `PatternRuleTrigger`) | Suggest, with consent, on the dashboard |
| Replanning | `PlanningGraphReplanner` | Re-slots set-aside work |

## Global constraints

- **Draft, then accept.** Jarvis never writes study tasks into the schedule on its own. Everything it derives from a document is a draft or a suggestion the user accepts, adjusts or dismisses. This is the product's consent rule, not a nicety.
- **Never frame this as a calendar or planner feature** in names, copy or docs. The user uploads; Jarvis understands and prepares.
- Layering, `route_llm_call`, Pydantic parsing of all model output, `user_id` on every query, no `print`, scripted models in tests: as in `.claude/rules/` and the companion plan.
- Model output about dates is never trusted alone: an exam date must appear in the document text (check the extracted date string against the source text) or be confirmed by the user.
- `PRIMARY`-role model calls run one at a time (never in `asyncio.gather`).
- New tables and columns go in a new idempotent migration with row-level security on; apply with `supabase db push --include-all`.
- Commit messages: plain description, no co-author or tool attribution lines.

---

## Package F: files belong somewhere (backend)

**Files**
- Create: `supabase/migrations/2026111600000X_document_scope_and_projects.sql`
- Modify: `app/domain/entities.py` (`IngestedDocument`, new `Project`), `app/domain/ports.py` (`ProjectRepository`; `DocumentRepository.list_for_user` gains filters), both repository families in `app/infrastructure/persistence/`, `app/services/documents/service.py`, `app/api/v1/endpoints/documents.py`, new `app/api/v1/endpoints/projects.py`, `app/schemas/documents.py`
- Modify: `app/utils/chroma_client.py` and `knowledge_ingester.py` (chunks carry `conversation_id` and `project_id` in their metadata)
- Test: `tests/test_repository_contracts.py` (both families), `tests/test_document_scope.py`, `tests/test_api_contract.py`, `tests/test_routes.py`, `tests/test_data_isolation.py`

**Design**
- `projects` table: `id`, `user_id`, `name`, `kind` (`course`, `exam_season`, `other`), `starts_on`, `ends_on`, `created_at`.
- `documents` gains `conversation_id` (nullable) and `project_id` (nullable). Scope is derived, not stored: a project id means project; otherwise a conversation id means that chat; neither means general.
- Upload paths: a file sent in chat is stamped with that conversation's id; the Documents page uploads to general, or to a project when one is chosen.
- `DocumentService.list_documents(user_id, *, conversation_id=None, project_id=None, scope=None)`.
- `PATCH /api/v1/documents/{source_id}` with `{project_id | null}`: move a document into a project or back to general ("keep this for all my chats").
- `GET/POST /api/v1/projects`, `GET /api/v1/projects/{id}` (with its documents and goals).
- **Nearest first retrieval:** `query_knowledge(user_id, query, n, *, conversation_id=None, project_id=None)` searches the chat's files, then the project's, then general, and stops when it has `n` passages over the relevance bar. Chat context building (`app/services/chat_turn/context.py`) passes the conversation id.
- Deleting a conversation does not delete its documents; they become general (state this in the response).

**Tests (minimum)**
- `test_documents__uploaded_in_chat__listed_for_that_conversation_only_when_filtered`.
- `test_retrieval__same_topic_in_chat_file_and_general_file__chat_file_first`.
- `test_documents__move_to_project__listed_under_project_and_chunks_restamped`.
- `test_projects__another_users_project_id__not_found`.

**Done when:** a syllabus uploaded in one chat is quoted in that chat, is not offered as "this chat's file" in another, and can be moved into a project.

---

## Package G: files have a home on screen (frontend) — needs F

**Files**
- Modify: `app/(app)/chat/` (conversation view), `app/(app)/documents/page.tsx`, `lib/api.ts`, `lib/documents.ts`, `lib/types.ts`
- Create: `components/app/ChatFiles.tsx`, `components/app/ProjectPicker.tsx`, `app/(app)/projects/[projectId]/page.tsx`
- Test: `lib/__tests__/documents.test.ts`, `lib/__tests__/api-documents.test.ts`

**Design**
- In a chat: an uploaded file shows as a chip on the message that carried it (name, type, open in the viewer). A "Files in this chat" panel lists them all.
- Documents page: tabs "All", "General", one per project, "From chats". Each row shows where it lives and a "Move to…" menu.
- Project page: its name and dates, its documents, its goals with progress, and a "Talk about this" button that opens a chat already scoped to the project.
- The existing viewer (`/documents/[sourceId]`) is reused everywhere.

**Done when:** from a chat you can see and open the files uploaded in it, and the Documents page shows where every file lives.

---

## Package H: Jarvis reads an exam timetable and a syllabus as what they are (backend)

**Files**
- Create: `app/schemas/exam_prep.py`, `app/services/exam_prep/extractors.py`
- Modify: `app/services/extraction/orchestrator.py` (route to the extractors), `app/services/documents/registry.py` (`handle_syllabus` stops being a stub), `app/core/model_router.py` (task names `exam_schedule_extraction`, `syllabus_outline_extraction`, both `PRIMARY`)
- Modify: `app/domain/entities.py` and the documents migration (`documents.kind`, `documents.extraction` JSONB)
- Test: `tests/test_exam_prep_extractors.py`, with two small real PDFs under `tests/fixtures/` (a timetable and a syllabus) read by Docling

**Design**
- `ExamEntry(subject: str, code: str | None, exam_date: date, start_time: time | None, venue: str | None, source_text: str)`; `ExamSchedule(title: str, exams: list[ExamEntry])`.
- `SyllabusUnit(title: str, topics: list[str], weight: float | None)`; `SyllabusOutline(subject: str, code: str | None, units: list[SyllabusUnit], assessments: list[str])`.
- The router gains a document kind next to its intent: `exam_timetable`, `class_timetable`, `syllabus`, `past_paper`, `notes`, `other`. A class timetable keeps today's path (slots awaiting approval). An exam timetable goes to `extract_exam_schedule`; a syllabus to `extract_syllabus_outline` as well as the knowledge store.
- Guard: drop any `ExamEntry` whose date cannot be found in the document text; if none survive, store nothing and say so.
- The result is saved on the document record (`kind`, `extraction`), so it is extracted once.

**Tests (minimum)**
- `test_exam_schedule__real_timetable_pdf__subjects_and_dates`.
- `test_exam_schedule__date_not_in_document__entry_dropped`.
- `test_syllabus_outline__real_syllabus_pdf__units_and_topics`.
- `test_router__class_timetable__still_goes_to_pending_calendar`.

**Done when:** `GET /api/v1/documents/{id}` for an uploaded exam timetable returns its exams as data.

---

## Package I: the study plan builds itself (backend) — needs H; F optional

**Files**
- Create: `app/services/exam_prep/matcher.py`, `app/services/exam_prep/study_plan.py`, `app/services/exam_prep/service.py`, `app/services/anticipation/triggers/exam_prep.py`
- Modify: `app/container.py`, `app/services/anticipation/` (register the trigger and its action), `app/domain/events.py` (`DocumentUnderstood`)
- Test: `tests/test_exam_prep_matcher.py`, `tests/test_study_plan.py`, `tests/behaviour/test_exam_prep_turn.py`

**Design**
- **Match:** pair each `ExamEntry` with a `SyllabusOutline` by course code, then by subject-name similarity (the embedder already used by `DocumentLinker`). Unmatched exams are kept (a plan with no syllabus is still possible, in coarser tasks); unmatched syllabi wait.
- **Budget (pure function, no model call, fully tested):** `build_study_budget(exams, outlines, today, daily_capacity_minutes, focus_profile) -> list[SubjectBudget]`. Per subject: days left until its exam, units weighted by `weight` (equal when absent), and three phases:
  - *Learn or relearn* each unit (largest share),
  - *Practise* each unit (questions, past papers when uploaded),
  - *Revise* on a spaced rhythm (1, 3, 7 days after learning) and a final light revision the evening before; nothing heavy the night before an exam.
  Subjects whose exams come first get their work first; when the weeks cannot hold everything, the budget shrinks practice before revision and reports what was cut, instead of producing an impossible plan.
- **Tasks:** each budget line becomes a `TaskChunk` with the unit's topics as `topic_keywords`, `deadline_hint` = the day before the exam, `not_before` where a phase must wait, and dependencies learn → practise → revise. Titles are concrete ("DBMS Unit 3: normalise two schemas to 3NF"). One model call per subject may word the titles and completion criteria from the syllabus text; the counts, durations and dates come from the budget, never from the model.
- **Schedule:** through the existing path (`run_schedule`, the user's rules, focus profile, class slots). Each exam itself is a hard block. The result is one **draft**.
- **Offer, do not impose:** `ExamPrepTrigger` fires on `DocumentUnderstood` when there is at least one exam with a date ahead. It creates a suggestion: "Your end sems start on Dec 15: 5 exams in 9 days. I've drafted 4 weeks of preparation around your evenings. Review it?" Accepting opens the draft; the draft is accepted through the normal `accept_draft`.
- **Bookkeeping:** on accept, an `exam_season` project (Package F) is created or reused, with one goal per subject (deadline = exam date). The syllabus and timetable are moved into the project, and `DocumentLinkListener` links the new tasks to the syllabus as it already does.
- **Ask when it must:** if a timetable arrives with no syllabus, the suggestion says which subjects have no syllabus yet and plans them coarsely; uploading one later refines only that subject's tasks (a new draft, again offered).

**Tests (minimum)**
- `test_budget__five_exams_four_weeks__earlier_exams_prepared_first`.
- `test_budget__not_enough_days__practice_cut_before_revision_and_reported`.
- `test_budget__night_before_exam__only_light_revision`.
- `test_study_plan__tasks_never_scheduled_inside_rules_or_class_slots` (real CP-SAT).
- `test_exam_prep__upload_timetable_and_syllabus__suggestion_then_draft_nothing_written_until_accept` (behaviour test over the real chat turn).
- `test_exam_prep__accept_twice__no_duplicate_tasks`.

**Done when:** uploading the two demo PDFs produces a dashboard suggestion, and accepting it shows weeks of subject-wise tasks ending the day before each exam, each task's workspace opening on the right syllabus unit.

---

## Package J: the plan stays honest (backend) — needs I

**Files**
- Create: `app/services/exam_prep/readiness.py`, `app/services/anticipation/triggers/deadline_risk.py`
- Modify: `app/services/user_model_service.py` or a new `GET /api/v1/projects/{id}/readiness`
- Test: `tests/test_readiness.py`, `tests/test_deadline_risk_trigger.py`

**Design**
- **Readiness per subject:** units learned, practised and revised out of the total, from task outcomes. A number with its evidence, never a guess.
- **Deadline risk trigger** (named in the capstone docs as not built): once a day, compare the remaining budget with the remaining days. If a subject no longer fits, suggest a recalibrated draft ("Networks is 3 days behind; I can move Unit 5 practice into Saturday and shorten two revisions"). Consent as always.
- **Recalibrate on new information:** a past paper uploaded to the project adds practice tasks for the matching subject; a changed exam date (new timetable) redrafts that subject only.

**Done when:** setting aside three days of a subject's tasks produces a suggestion with a concrete recalibrated draft.

---

## Package K: the exam season on screen (frontend) — needs I; richer with J

**Files**
- Modify: `app/(app)/dashboard/page.tsx`, `app/(app)/projects/[projectId]/page.tsx`
- Create: `components/app/ExamCountdown.tsx`, `components/app/SubjectReadiness.tsx`, `lib/exam-prep.ts`
- Test: `lib/__tests__/exam-prep.test.ts`

**Design**
- Dashboard card when an exam season is active: next exam and days left, and one line per subject with its readiness bar.
- Project page: the exams in date order, each subject's units with learned / practised / revised marks, and its documents.
- No streaks, guilt or red warnings; behind schedule is shown as what Jarvis proposes to do about it.

---

## Beyond (not in these packages; record ideas here, do not build yet)

- Past-paper analysis: which units the papers favour, feeding the unit weights.
- Readiness from quiz results in the workspace (needs the structured quiz, companion plan Package C).
- Class timetable and exam timetable together: fewer study hours on heavy class days, automatically.
- Other document kinds on the same rails: an assignment sheet (deliverables and a due date), a job description (interview prep), a conference schedule.
- A summary of each uploaded document on its viewer page, and "ask this document" from the viewer.

## Review focus

1. A timetable photo with a misread date: the date guard drops it and Jarvis says which exam it could not read, instead of planning for the wrong day.
2. Two uploads of the same timetable: one project, one set of goals, no duplicate tasks.
3. A syllabus for a subject with no exam in the timetable: no plan is invented for it.
4. An exam date already in the past: ignored, with a note.
5. The model is unavailable: the timetable and syllabus are still stored and searchable; the suggestion appears later, when extraction succeeds on retry, or the user is told it could not be read.

## Order and size

| Package | Needs | Size | Suggested owner |
|---|---|---|---|
| F files belong somewhere | none | 1 to 1.5 days | backend teammate 1 |
| G files on screen | F | 1 day | frontend teammate |
| H read timetable and syllabus | none | 1 day | backend teammate 2 |
| I study plan builds itself | H | 2 days | Madhav + backend teammate 2 |
| J plan stays honest | I | 1 day | backend teammate 1 |
| K exam season on screen | I | 1 day | frontend teammate |

**For the demo on the 17th and 18th:** all of this does not fit. The slice that shows the idea end to end is **H, then I without projects** (the suggestion, the draft, and tasks whose workspaces open on the syllabus). F, G, J and K follow after the evaluation. Do not start a package on the 17th that cannot be merged and verified with the full test suite before the demo.
