"""cilicon hosted service — FastAPI app: the webhook + the dashboard.

Run it with:  uvicorn cilicon.service.app:app --reload
Endpoints:
  POST /webhook/github     ← GitHub App deliveries (push / PR / install)
  GET  /                    dashboard: your orgs → projects
  GET  /login /auth/callback /logout      GitHub OAuth
  GET  /projects/{id}       run history for a repo
  GET  /runs/{id}           one run: the matrix, per-target logs + artifacts
  GET  /runs/{id}/status    htmx partial — live status while a run is in flight
  GET  /dl/log|artifact/{target_result_id}   short-lived signed Storage URL
  GET  /healthz
"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, github, orchestrator
from .db import db
from .settings import require, settings

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

app = FastAPI(title="cilicon", docs_url=None, redoc_url=None)
_static = _HERE / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


# ── webhook ───────────────────────────────────────────────────────────────

@app.post("/webhook/github")
async def webhook(request: Request, background: BackgroundTasks):
    require("supabase", "github_app")
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not github.verify_signature(settings().gh_webhook_secret, body, sig):
        raise HTTPException(401, "bad signature")

    import json
    kind = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body or b"{}")

    result = orchestrator.on_webhook(kind, payload)
    if "run" in result:
        run_id, event = result["run"]
        # heavy work (clone + matrix) off the request path so GitHub's delivery
        # doesn't time out. A single worker thread per run; a production
        # deployment would hand this to a real queue.
        threading.Thread(
            target=orchestrator.execute_run, args=(run_id, event), daemon=True
        ).start()
        return {"ok": True, "run_id": run_id}
    return {"ok": True}


# ── auth ─────────────────────────────────────────────────────────────────

@app.get("/login")
def login():
    require("github_oauth")
    state = auth.new_state()
    resp = RedirectResponse(github.oauth_authorize_url(state))
    resp.set_cookie(auth.STATE_COOKIE, state, httponly=True, max_age=600,
                    samesite="lax", secure=_secure())
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    require("github_oauth")
    if not code or state != request.cookies.get(auth.STATE_COOKIE):
        raise HTTPException(400, "invalid OAuth state")

    token = github.exchange_oauth_code(code)
    gh = github.oauth_user(token)
    d = db()
    user = d.upsert_user(gh["github_id"], gh["login"], name=gh.get("name"),
                         avatar_url=gh.get("avatar_url"), email=gh.get("email"))
    # record membership for any org we already know about (best-effort)
    for org in d.orgs_by_logins(gh["org_logins"]):
        try:
            d.ensure_membership(org["id"], user["id"])
        except Exception:
            pass

    session = {"user_id": user["id"], "login": gh["login"],
               "name": gh.get("name"), "avatar": gh.get("avatar_url"),
               "org_logins": gh["org_logins"]}
    resp = RedirectResponse("/")
    resp.set_cookie(auth.COOKIE, auth.encode_session(session), httponly=True,
                    max_age=7 * 24 * 3600, samesite="lax", secure=_secure())
    resp.delete_cookie(auth.STATE_COOKIE)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(auth.COOKIE)
    return resp


# ── dashboard ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = auth.current_user(request)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request})

    d = db()
    orgs = d.orgs_by_logins(user.get("org_logins") or [])
    blocks = []
    for org in orgs:
        blocks.append({"org": org, "projects": d.projects_for_org(org["id"])})
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "blocks": blocks,
         "app_slug": settings().gh_app_id, "base_url": settings().base_url})


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_runs(request: Request, project_id: str):
    user = _require_user(request)
    d = db()
    project = d.get_project(project_id)
    _authz(user, project)
    runs = d.runs_for_project(project_id)
    return templates.TemplateResponse(
        "project.html",
        {"request": request, "user": user, "project": project, "runs": runs})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    user = _require_user(request)
    d = db()
    run = d.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    project = d.get_project(run["project_id"])
    _authz(user, project)
    results = d.target_results(run_id)
    return templates.TemplateResponse(
        "run.html",
        {"request": request, "user": user, "run": run, "project": project,
         "results": results, "live": run["status"] in ("queued", "running")})


@app.get("/runs/{run_id}/status", response_class=HTMLResponse)
def run_status(request: Request, run_id: str):
    """htmx polls this while a run is live; swaps in the finished view itself."""
    user = _require_user(request)
    d = db()
    run = d.get_run(run_id)
    if not run:
        raise HTTPException(404)
    project = d.get_project(run["project_id"])
    _authz(user, project)
    results = d.target_results(run_id)
    return templates.TemplateResponse(
        "_run_body.html",
        {"request": request, "run": run, "results": results,
         "live": run["status"] in ("queued", "running")})


@app.get("/dl/{kind}/{target_result_id}")
def download(request: Request, kind: str, target_result_id: str):
    user = _require_user(request)
    d = db()
    rows = d._select("target_results",
                     params={"id": f"eq.{target_result_id}", "select": "*,runs(project_id)"})
    if not rows:
        raise HTTPException(404)
    tr = rows[0]
    _authz(user, d.get_project(tr["runs"]["project_id"]))
    s = settings()
    if kind == "log" and tr.get("log_path"):
        url = d.signed_url(s.log_bucket, tr["log_path"])
    elif kind == "artifact" and tr.get("artifact_path"):
        url = d.signed_url(s.artifact_bucket, tr["artifact_path"])
    else:
        raise HTTPException(404, "no such file")
    return RedirectResponse(url)


@app.get("/healthz")
def healthz():
    return {"ok": True, "configured": settings().configured}


# ── helpers ────────────────────────────────────────────────────────────────

def _secure() -> bool:
    return settings().base_url.startswith("https://")


def _require_user(request: Request) -> dict:
    user = auth.current_user(request)
    if not user:
        raise HTTPException(401, "log in at /login")
    return user


def _authz(user: dict, project: dict | None) -> None:
    if not project:
        raise HTTPException(404, "not found")
    org = db().get_org(project["org_id"])
    if not org or not auth.can_see_org_login(user, org["github_login"]):
        raise HTTPException(403, "you don't have access to this project")
