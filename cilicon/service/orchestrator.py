"""cilicon orchestrator — webhook event → a real CI run.

This is the glue between the GitHub App, the `cilicon` engine, and ClickHouse:

  push/PR ─▶ create run row (queued) ─▶ in-progress check-run
          ─▶ clone @sha ─▶ cilicon.run_matrix ─▶ persist results + logs + artifacts
          ─▶ complete check-run with the matrix table ─▶ run row finalized

`on_webhook` is fast (it only writes the queued row); `execute_run` does the
heavy clone+build and is meant to run in a background worker so GitHub's webhook
delivery doesn't time out.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone

from .. import config as cfgmod
from .. import report as reportmod
from ..runner import TargetResult, run_matrix
from . import github
from .db import db
from .settings import settings


# ── webhook entry (cheap; returns work to do in the background) ──────────────

def on_webhook(kind: str, payload: dict) -> dict:
    """Process a verified webhook. Returns {'run': (run_id, event)} when a run
    should be executed in the background, else {} (handled synchronously)."""
    e = github.parse_event(kind, payload)
    d = db()

    if kind in ("installation", "installation_repositories"):
        _sync_installation(payload, e)
        return {}

    if not e.should_run or not e.installation_id:
        return {}

    project = d.project_by_repo_id(e.repo_id) or _autoregister(e)
    run = d.create_run(
        project_id=project["id"],
        commit_sha=e.sha, ref=e.ref, event=e.kind, pr_number=e.pr_number,
        triggered_by=e.sender, message=e.message[:200], status="queued",
    )
    return {"run": (run["id"], e)}


def _sync_installation(payload: dict, e: github.Event) -> None:
    """App installed / repos added → make sure org, install, and projects exist."""
    d = db()
    if not e.account_login:
        return
    org = d.upsert_org(e.account_login)
    d.upsert_installation(e.installation_id, org["id"], e.account_login, e.account_type)
    for repo in payload.get("repositories", []) or payload.get("repositories_added", []):
        d.upsert_project(
            org_id=org["id"], github_repo_id=repo["id"],
            full_name=repo["full_name"], installation_id=e.installation_id,
            private=repo.get("private", True),
        )


def _autoregister(e: github.Event) -> dict:
    """A run arrived for a repo we haven't seen (install webhook missed/raced).
    Create the org + project on the fly so the run isn't dropped."""
    d = db()
    owner = e.full_name.split("/")[0] if e.full_name else e.account_login
    org = d.upsert_org(owner)
    d.upsert_installation(e.installation_id, org["id"], owner, e.account_type)
    return d.upsert_project(
        org_id=org["id"], github_repo_id=e.repo_id, full_name=e.full_name,
        installation_id=e.installation_id, default_branch=e.default_branch,
        private=e.private,
    )


# ── the heavy pipeline ───────────────────────────────────────────────────────

def dispatch(run_id: str, e: github.Event) -> None:
    """Schedule execute_run off the request path. Default: a daemon thread (fine
    for a single-container dev/self-host). The Modal deploy target overrides this
    with `runner.spawn(...)` so each run gets its own container + long timeout."""
    import threading
    threading.Thread(target=execute_run, args=(run_id, e), daemon=True).start()


def execute_run(run_id: str, e: github.Event) -> None:
    """Clone, run the matrix, persist everything, update the check-run.
    Swallows nothing silently — failures land on the run row + check-run."""
    d = db()
    s = settings()
    app = github.GitHubApp()
    run = d.get_run(run_id)
    project = d.get_project(run["project_id"]) if run else None
    if project is None:
        return

    # soft quota: protect the shared platform Modal workspace
    over = _quota_exceeded(project["org_id"])
    if over:
        d.update_run(run_id, status="error", error=over, finished_at=_now())
        _safe_check(app, e, success=False, title="quota exceeded", summary=over)
        return

    check_id = None
    try:
        check_id = app.create_check_run(
            e.installation_id, e.full_name, e.sha,
            title="cilicon is building + booting your matrix…",
            summary="Fanning every target out to its own cloud container.",
        )
        d.update_run(run_id, status="running", started_at=_now(), check_run_id=check_id)

        workdir = tempfile.mkdtemp(prefix="cilicon-run-")
        try:
            _clone(app, e, workdir)
            cfg_path = os.path.join(workdir, "cilicon.yml")
            if not os.path.exists(cfg_path):
                raise RuntimeError("no cilicon.yml at repo root")

            cfg = cfgmod.load(cfg_path)
            artifacts_dir = os.path.join(workdir, "_artifacts")
            t0 = datetime.now(timezone.utc)
            results = run_matrix(
                cfg, only=None, on_update=lambda *a: None,
                artifacts_dir=artifacts_dir, max_workers=s.max_parallel_targets,
            )
            wall = (datetime.now(timezone.utc) - t0).total_seconds()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        passed = sum(1 for r in results if r.ok)
        _persist_results(run_id, results)
        d.update_run(
            run_id, status="passed" if passed == len(results) else "failed",
            passed=passed, total=len(results), wall_seconds=round(wall, 2),
            finished_at=_now(),
        )

        summary = reportmod.to_markdown(results, wall)
        title = f"{passed}/{len(results)} targets built and booted"
        _safe_check(app, e, success=(passed == len(results)),
                    title=title, summary=summary, check_id=check_id,
                    details_url=f"{s.base_url}/runs/{run_id}")

    except Exception as ex:  # noqa: BLE001
        msg = f"{type(ex).__name__}: {ex}"
        d.update_run(run_id, status="error", error=msg, finished_at=_now())
        _safe_check(app, e, success=False, title="cilicon run failed",
                    summary=f"```\n{msg}\n```", check_id=check_id,
                    details_url=f"{s.base_url}/runs/{run_id}")


def _clone(app: github.GitHubApp, e: github.Event, dest: str) -> None:
    """Fetch the exact commit (works for PR head shas, not just branch tips)."""
    url = app.clone_url(e.installation_id, e.full_name)
    def run(*c):
        return subprocess.run(c, cwd=dest, check=True, capture_output=True)

    run("git", "init", "-q")
    run("git", "remote", "add", "origin", url)
    try:
        run("git", "fetch", "--depth", "1", "origin", e.sha)
        run("git", "checkout", "-q", "FETCH_HEAD")
    except subprocess.CalledProcessError:
        # some servers won't fetch an arbitrary sha; fall back to the ref tip
        run("git", "fetch", "--depth", "1", "origin", e.ref or e.default_branch)
        run("git", "checkout", "-q", "FETCH_HEAD")


def _persist_results(run_id: str, results: list[TargetResult]) -> None:
    d = db()
    s = settings()
    for r in results:
        slug = r.target.slug
        log_path = artifact_path = None
        try:
            log_path = d.upload(s.log_bucket, f"{run_id}/{slug}.log",
                                _full_log(r).encode("utf-8", "replace"))
        except Exception:
            pass
        blob = _artifact_tar(r)
        if blob:
            try:
                artifact_path = d.upload(
                    s.artifact_bucket, f"{run_id}/{slug}.tgz", blob,
                    content_type="application/gzip")
            except Exception:
                artifact_path = None
        d.add_target_result(
            run_id=run_id, target_id=r.target.id, validate=r.target.validate,
            ok=r.ok, seconds=round(r.seconds, 2),
            build_ok=bool(r.build and r.build.ok),
            validate_ok=bool(r.validate and r.validate.ok),
            test_ok=(None if r.test is None else r.test.ok),
            size_ok=(None if r.size is None else r.size.ok),
            detail=_detail(r), sizes=(r.sizes or None),
            log_path=log_path, artifact_path=artifact_path,
        )


def _full_log(r: TargetResult) -> str:
    parts = [f"# cilicon target: {r.target.id}", f"# validate tier: {r.target.validate}", ""]
    if r.error:
        parts += ["## orchestration error", r.error, ""]
    for step in (r.build, r.size, r.validate, r.test):
        if step is None:
            continue
        status = "ok" if step.ok else "FAILED"
        parts += [f"## {step.name} [{status}] ({step.seconds:.1f}s) — {step.detail}",
                  step.output.rstrip(), ""]
    return "\n".join(parts)


def _detail(r: TargetResult) -> str:
    if r.error:
        return f"infra: {r.error}"
    if r.build and not r.build.ok:
        return "build failed"
    if r.size and not r.size.ok:
        return r.size.detail
    if r.test and not r.test.ok:
        return f"test: {r.test.detail}"
    return r.validate.detail if r.validate else "—"


def _artifact_tar(r: TargetResult) -> bytes | None:
    """Re-tar the artifacts the runner pulled back to disk, for Storage."""
    files = [p for p in r.artifacts if os.path.isfile(p)]
    if not files:
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in files:
            tf.add(p, arcname=os.path.basename(p))
    return buf.getvalue()


def _quota_exceeded(org_id: str) -> str:
    s = settings()
    if s.org_monthly_seconds <= 0:
        return ""
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0,
                                               microsecond=0).isoformat()
    used = db().org_seconds_since(org_id, start)
    if used >= s.org_monthly_seconds:
        return (f"monthly compute quota reached "
                f"({int(used)}s / {s.org_monthly_seconds}s). Resets on the 1st.")
    return ""


def _safe_check(app, e, *, success, title, summary, check_id=None, details_url="") -> None:
    """Update (or create+complete) the check-run; never raise into the pipeline."""
    try:
        if check_id is None:
            check_id = app.create_check_run(e.installation_id, e.full_name, e.sha,
                                            title=title, summary=summary)
        app.complete_check_run(
            e.installation_id, e.full_name, check_id,
            success=success, title=title, summary=summary[:65000],
            details_url=details_url)
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
