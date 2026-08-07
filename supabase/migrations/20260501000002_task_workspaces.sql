-- Workspace persistence with 24h TTL.
-- Workspaces are rebuilt on miss/stale; bust automatically when task_material_linker
-- links a new document to the task.

create table if not exists public.task_workspaces (
  id           uuid primary key default gen_random_uuid(),
  user_id      text not null,
  task_id      text not null,
  workspace_json jsonb not null,
  built_at     timestamptz not null default now(),
  expires_at   timestamptz not null,
  created_at   timestamptz not null default now(),
  unique (user_id, task_id)
);

create index if not exists task_workspaces_user_task_idx
  on public.task_workspaces (user_id, task_id);

create index if not exists task_workspaces_expires_idx
  on public.task_workspaces (expires_at);
