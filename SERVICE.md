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
  │   • create run row ──────────────────────────┼─────▶│ ClickHouse     │
  │   • in-progress check-run ───┐                │      │  (HTTP API)    │
  │   • clone @sha               │                │      │  runs +        │
  │   • cilicon.run_matrix ────────┼──▶ Modal       │      │  target_results│
  │       (the CLI engine)       │   (1 workspace,│      │  + log blobs   │
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

2. **Hosted, persisted, multi-tenant.** ClickHouse stores orgs, projects, runs,
   and per-target results (mutable rows are `ReplacingMergeTree`, read latest via
   `FINAL`); logs + pulled artifacts live in a `blobs` table, served by the
   service's `/dl` route. A server-rendered dashboard (GitHub-OAuth login, scoped
   to the orgs you belong to) lists projects → runs → a run's matrix, with live
   status (htmx polling) and log/artifact downloads. Schema is created on boot by
   `db.init_schema()` — no manual migration.

3. **Richer, trustworthy emulation validation.** The point of cilicon is you
   *don't* need hardware in the loop. So the green check has to mean more than
   "it linked." The engine now supports on-target **test suites** (`test:` +
   `test_expect:`), structured assertions (`expect:` list, `expect_regex`,
   `expect_exit`), and **flash/RAM size budgets** (`size_tool` + `flash_max` /
   `ram_max`) — an overflow fails the check before it would ever overflow a real
   linker. See `cilicon/sizes.py`.

## Setup — deploy on Modal (production)

The whole thing runs on **one Modal workspace**: the webhook + dashboard as a
Modal ASGI app, and each run as a spawned Modal function that itself fans the
per-target sandboxes. `runner.py` already runs the matrix on Modal, so hosting it
is just wrapping the service.

### 1. ClickHouse
Create a database (ClickHouse Cloud has a free tier). You need its **HTTPS
endpoint**, a **user**, and a **password** — that's it. The service creates its
database + tables itself on first boot (`db.init_schema()`, idempotent). No
manual migration.

### 2. GitHub App
Register one at <https://github.com/settings/apps/new>:
- **Webhook URL**: the deployed Modal URL + `/webhook/github` (fill in after step 4;
  you can edit it later).
- **Webhook secret**: generate a random string (you'll set it as `GITHUB_WEBHOOK_SECRET`).
- **Permissions**: Checks → **Read & write**; Contents → **Read**; Metadata → **Read**.
- **Subscribe to events**: `push`, `pull_request`, `installation`, `installation_repositories`.
- **OAuth** (for the dashboard login): set the callback to the deployed URL + `/auth/callback`.
- Generate a **private key** (`.pem`) and note the **App ID** + OAuth **client id/secret**.

### 3. Modal secret
Bundle every value into one Modal Secret named `cilicon-service`:
```bash
modal secret create cilicon-service \
  GITHUB_APP_ID=... \
  GITHUB_APP_PRIVATE_KEY="$(cat app-private-key.pem)" \
  GITHUB_WEBHOOK_SECRET=... \
  GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... \
  CLICKHOUSE_URL=https://<host>:8443 CLICKHOUSE_USER=default CLICKHOUSE_PASSWORD=... \
  CLICKHOUSE_DATABASE=cilicon \
  CILICON_BASE_URL=https://<your-modal-url> \
  CILICON_SESSION_SECRET="$(openssl rand -hex 32)" \
  CILICON_ORG_MONTHLY_SECONDS=36000
```

### 4. Deploy
```bash
modal deploy cilicon/service/modal_app.py
```
Modal prints the public URL of the `web` app. Put that URL (+ `/webhook/github`)
into the GitHub App's webhook, and `/auth/callback` into its OAuth callback, then
redeploy nothing — just save the App settings. (Update the `CILICON_BASE_URL`
secret to that URL if it changed.)

### 5. Install + go
Install the App on a repo that has a `cilicon.yml` at its root, push a commit, and
the check appears — no Modal token in the consumer's repo, because the run
executes on **your** Modal. That's the frictionless part.

> **Local / self-host alternative.** `pip install -e ".[service]"`, export the
> same env vars, and `uvicorn cilicon.service.app:app --port 8000`. The webhook
> then dispatches runs on a local thread (`orchestrator.dispatch`) instead of a
> Modal spawn. Fine for dev; Modal is the production path.

> **Isolation note.** Tenant `build:`/`validate:` commands are untrusted code.
> They run in per-target Modal Sandboxes (good), on a shared workspace bounded by
> `CILICON_MAX_PARALLEL_TARGETS` (concurrency) and `CILICON_ORG_MONTHLY_SECONDS`
> (soft per-org quota). Treat the workspace as hostile; one Modal environment per
> org is the stricter next step.

## What's intentionally simple (and where it goes next)

- **Background work is a spawned Modal function per run** (`modal_app.py` overrides
  `orchestrator.dispatch`). Locally it falls back to a daemon thread. Each run gets
  its own container + a 1h timeout, so the webhook returns to GitHub instantly.
- **Login is the App's GitHub OAuth**, session in a signed cookie — no separate
  auth service. A dedicated auth provider could replace it for non-GitHub identity.
- **Read-scoping** is by GitHub org membership captured at login. Fine-grained
  per-repo roles would live in the `memberships` table.

## Tests
```bash
pip install -e ".[dev,service]"
pytest
```
The security- and correctness-critical pure logic is covered without needing
Modal, ClickHouse, or GitHub live: size parsing/budgets, webhook signature
verification, event parsing, config/matrix expansion, the sandbox script +
output parser, and the report renderers.
