# cilicon service — OPTIONAL reference dashboard (not the product)

> ⚠️ **This layer is an optional reference implementation, not how you're meant
> to use cilicon.** The product is the **engine** (`cilicon/`) + the **GitHub
> Action** (`action.yml`): you run cilicon as a *step inside* your existing CI
> and it reports a normal PR check. You do **not** need to host this service.
>
> A self-hosted webhook + dashboard + run database overlaps heavily with what
> GitHub already does for you — so this exists only to show how the engine *can*
> be wrapped as a hosted product, and as a demo dashboard if you want a
> persisted view. For real use, see **[docs/github-actions.md](docs/github-actions.md)**.
> Service deps are opt-in: `pip install -e ".[service]"`.

---

This document covers that optional layer: code pushed to GitHub gets
built-and-booted automatically, the result lands as a single PR check, and every
run is persisted with logs and artifacts you can open from a dashboard.

```
   GitHub push / PR
        │  webhook (HMAC-verified)
        ▼
  ┌─────────────────────────────────────────────┐
  │ cilicon.service (FastAPI)                       │
  │   • verify + parse event                      │      ┌───────────────┐
  │   • create run row ──────────────────────────┼─────▶│ Supabase       │
  │   • in-progress check-run ───┐                │      │  Postgres      │
  │   • clone @sha               │                │      │  Storage:      │
  │   • cilicon.run_matrix ────────┼──▶ Modal       │      │   logs/        │
  │       (the CLI engine)       │   (1 workspace,│      │   artifacts/   │
  │   • persist results/logs ────┼───────────────┼─────▶│               │
  │   • complete check-run       │                │      └───────────────┘
  │   • dashboard (Jinja + htmx) │                │
  └──────────────────────────────┴───────────────┘
```

The engine (`cilicon/config.py`, `runner.py`, `presets.py`, `report.py`) has **no
idea any of this exists** — the service imports it and wraps a product around it.

## The three layers built here

1. **It's actually CI now.** `cilicon/service/github.py` + `orchestrator.py`: a
   GitHub App runs the matrix on every push and PR and reports back one
   check-run (`cilicon / build + boot`) whose body is the matrix table +
   failing-target logs (`report.to_markdown`). Before, the matrix only existed
   when you typed `cilicon run`.

2. **Hosted, persisted, multi-tenant.** Supabase Postgres stores orgs, projects,
   runs, and per-target results; Storage holds each target's full log and pulled
   artifacts. A server-rendered dashboard (GitHub-OAuth login, scoped to the
   orgs you belong to) lists projects → runs → a run's matrix, with live status
   (htmx polling) and signed-URL log/artifact downloads.

3. **Richer, trustworthy emulation validation.** The point of cilicon is you
   *don't* need hardware in the loop. So the green check has to mean more than
   "it linked." The engine now supports on-target **test suites** (`test:` +
   `test_expect:`), structured assertions (`expect:` list, `expect_regex`,
   `expect_exit`), and **flash/RAM size budgets** (`size_tool` + `flash_max` /
   `ram_max`) — an overflow fails the check before it would ever overflow a real
   linker. See `cilicon/sizes.py`.

## Setup

### 1. Supabase
Create a project, then run the migration (SQL editor, or `supabase db push`):
```
supabase/migrations/0001_init.sql
```
It creates the tables, enables RLS (deny-all to anon/auth — the service uses the
service-role key, which bypasses RLS), and creates the private `logs` and
`artifacts` Storage buckets.

### 2. GitHub App
Create one at <https://github.com/settings/apps/new> with the permissions,
webhook URL, secret, and OAuth callback listed in `.env.example`. Download the
private key (`.pem`). Subscribe to `push`, `pull_request`, `installation`,
`installation_repositories`.

### 3. Modal
`pip install -e ".[service]"` and `modal token new` (or set `MODAL_TOKEN_*`).
Cilicon owns **one** Modal workspace; all tenants' runs execute in it. The guard
rails for that shared blast radius are `CILICON_MAX_PARALLEL_TARGETS` (concurrency
ceiling per run) and `CILICON_ORG_MONTHLY_SECONDS` (soft per-org compute quota).

> **Isolation note.** Tenant `build:`/`validate:` commands are untrusted code.
> They already run in per-target Modal Sandboxes (good), but on a shared
> workspace you should also: scope Modal Secrets per project, keep the quota on,
> and treat the workspace as hostile (no ambient cloud creds). A stricter model
> is one Modal environment per org — a natural next step.

### 4. Run it
```bash
set -a; source .env; set +a
uvicorn cilicon.service.app:app --host 0.0.0.0 --port 8000
```
Install the App on a repo that has a `cilicon.yml` at its root, push a commit, and
watch the check appear — then open the run in the dashboard.

## What's intentionally simple (and where it goes next)

- **Background work is a thread per run** (`app.py`). Fine for a single instance;
  swap in a real queue (Modal `.spawn`, a Postgres/Redis queue, or
  `LISTEN/NOTIFY` workers) before you scale horizontally.
- **Login is the App's GitHub OAuth**, session in a signed cookie — no separate
  auth service. Supabase Auth could replace it if you want non-GitHub identity.
- **Read-scoping** is by GitHub org membership captured at login. Fine-grained
  per-repo roles would live in the `memberships` table.

## Tests
```bash
pip install -e ".[dev,service]"
pytest
```
The security- and correctness-critical pure logic is covered without needing
Modal, Supabase, or GitHub live: size parsing/budgets, webhook signature
verification, event parsing, config/matrix expansion, the sandbox script +
output parser, and the report renderers.
