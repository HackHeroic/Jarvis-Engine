# Supabase Migrations

Run these SQL migrations in your Supabase project's SQL Editor to enable the Autonomous Extraction Pipeline.

## 001_pending_calendar_and_behavioral.sql

Creates:
- `pending_calendar_updates` - Calendar extractions awaiting user approval
- `behavioral_constraints` - User preferences (Strategy Hub L7)

## 002_habit_recurrence_and_validity.sql

Extends `behavioral_constraints` with recurrence, valid_from, valid_until, structured_semantics.
Adds `valid_until` to `pending_calendar_updates`.

## 003_habit_trackers.sql

SM-2 spaced repetition: `habit_trackers` with repetitions, ef, next_interval_days, last_done_at.

## 004_user_tasks_and_task_materials.sql

- `user_tasks` - Persisted decomposition from PLAN_DAY (for task-material linking)
- `task_materials` - Links documents to tasks via embedding similarity

## 005_user_calendar_anchors.sql

Resolves validity placeholders (e.g. "finals", "semester_end") when exam schedules are uploaded.

Required for:
- `POST /api/v1/ingestion/process` (CALENDAR_SYNC pipeline)
- `GET /api/v1/ingestion/pending-calendar`
- `POST /api/v1/ingestion/pending-calendar/{id}/approve`
- `POST /api/v1/ingestion/pending-calendar/{id}/reject`
- BEHAVIORAL_CONSTRAINT pipeline storage
