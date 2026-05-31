-- cilicon hosted service — initial schema.
--
-- Security model: the FastAPI service talks to Supabase with the SERVICE ROLE
-- key, server-side only. RLS is enabled on every table with NO permissive
-- policies, so the anon/auth keys can read nothing directly — all access goes
-- through the service, which authorizes via GitHub App installations + the
-- session's org memberships. (If you later add a browser client that talks to
-- PostgREST directly, add policies keyed on auth.uid(); until then, deny-all is
-- the safe default and the service role bypasses it.)

create extension if not exists "pgcrypto";

-- ── identity ────────────────────────────────────────────────────────────────

create table if not exists orgs (
  id            uuid primary key default gen_random_uuid(),
  github_login  text not null unique,          -- the GitHub org/user login
  name          text,
  created_at    timestamptz not null default now()
);

create table if not exists users (
  id            uuid primary key default gen_random_uuid(),
  github_id     bigint not null unique,
  login         text not null,
  name          text,
  avatar_url    text,
  email         text,
  created_at    timestamptz not null default now()
);

create table if not exists memberships (
  org_id        uuid not null references orgs(id) on delete cascade,
  user_id       uuid not null references users(id) on delete cascade,
  role          text not null default 'member',  -- 'admin' | 'member'
  created_at    timestamptz not null default now(),
  primary key (org_id, user_id)
);

-- ── GitHub App installs + the repos they cover ──────────────────────────────

create table if not exists installations (
  id              bigint primary key,            -- GitHub installation id
  org_id          uuid references orgs(id) on delete set null,
  account_login   text not null,
  account_type    text,                          -- 'Organization' | 'User'
  suspended       boolean not null default false,
  created_at      timestamptz not null default now()
);

create table if not exists projects (
  id              uuid primary key default gen_random_uuid(),
  org_id          uuid not null references orgs(id) on delete cascade,
  installation_id bigint references installations(id) on delete set null,
  github_repo_id  bigint not null unique,
  full_name       text not null,                 -- "owner/repo"
  default_branch  text not null default 'main',
  private         boolean not null default true,
  created_at      timestamptz not null default now()
);
create index if not exists projects_org_idx on projects(org_id);

-- ── runs + per-target results ───────────────────────────────────────────────

-- status: queued -> running -> (passed | failed | error)
create table if not exists runs (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  commit_sha    text not null,
  ref           text,                            -- "refs/heads/main"
  event         text,                            -- 'push' | 'pull_request'
  pr_number     integer,
  status        text not null default 'queued',
  passed        integer not null default 0,
  total         integer not null default 0,
  wall_seconds  numeric,
  check_run_id  bigint,                          -- GitHub check run we update
  triggered_by  text,                            -- github login
  message       text,                            -- commit message (first line)
  error         text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz
);
create index if not exists runs_project_created_idx on runs(project_id, created_at desc);
create index if not exists runs_commit_idx on runs(project_id, commit_sha);

create table if not exists target_results (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid not null references runs(id) on delete cascade,
  target_id     text not null,
  validate      text,
  ok            boolean not null default false,
  seconds       numeric,
  build_ok      boolean,
  validate_ok   boolean,
  test_ok       boolean,                         -- null = no test phase
  size_ok       boolean,                         -- null = no size budget
  detail        text,                            -- the on-target check summary
  sizes         jsonb,                           -- {text,data,bss,flash,ram,...}
  log_path      text,                            -- Storage path in 'logs' bucket
  artifact_path text,                            -- Storage path in 'artifacts' bucket
  created_at    timestamptz not null default now()
);
create index if not exists target_results_run_idx on target_results(run_id);

-- ── lock everything down (service role bypasses RLS) ────────────────────────

alter table orgs            enable row level security;
alter table users           enable row level security;
alter table memberships     enable row level security;
alter table installations   enable row level security;
alter table projects        enable row level security;
alter table runs            enable row level security;
alter table target_results  enable row level security;

-- ── private Storage buckets for logs + artifacts ────────────────────────────
-- (served to users only via short-lived signed URLs the service mints.)

insert into storage.buckets (id, name, public)
values ('logs', 'logs', false), ('artifacts', 'artifacts', false)
on conflict (id) do nothing;
