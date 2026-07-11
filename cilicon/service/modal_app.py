"""cilicon hosted service, deployed on Modal.

    modal deploy cilicon/service/modal_app.py

gives a public HTTPS webhook + dashboard (the `web` ASGI function) and a separate
`runner` function that does the heavy clone+build+boot for each PR — so the
webhook returns to GitHub instantly while each run gets its own container and a
long timeout. The runner itself spawns the per-target Modal sandboxes (the same
engine `cilicon run` uses), so the whole thing lives on the one Modal workspace.

Config comes from a Modal Secret named `cilicon-service`:
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET,
  GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
  CLICKHOUSE_URL, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD, CLICKHOUSE_DATABASE,
  CILICON_BASE_URL (the deployed URL), CILICON_SESSION_SECRET,
  CILICON_ORG_MONTHLY_SECONDS (soft quota).
→ see docs/hosted.md for the full deploy + GitHub App registration walkthrough.
"""

from __future__ import annotations

import modal

APP_NAME = "cilicon-service"

# The image carries the service deps + the cilicon engine source. add_local_python_source
# ships the local `cilicon` package so runner/web import the same code you deploy.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi>=0.110",
        "uvicorn[standard]>=0.29",
        "httpx>=0.27",
        "pyjwt[crypto]>=2.8",
        "jinja2>=3.1",
        "itsdangerous>=2.1",
        "pyyaml>=6.0",
        "modal>=1.0",
    )
    .add_local_python_source("cilicon")
)

app = modal.App(APP_NAME)
secret = modal.Secret.from_name("cilicon-service")


@app.function(
    image=image,
    secrets=[secret],
    timeout=3600,                 # a full matrix can take a while
    max_containers=32,            # cap concurrent runs
)
def runner(run_id: str, event_dict: dict) -> None:
    """Execute one run in its own container: clone @sha, run the matrix on Modal
    sandboxes, persist results + logs, complete the PR check."""
    from cilicon.service import github, orchestrator
    orchestrator.execute_run(run_id, github.Event(**event_dict))


@app.function(image=image, secrets=[secret], min_containers=1)
@modal.asgi_app()
def web():
    """The FastAPI webhook + dashboard. Overrides the orchestrator's dispatch so a
    webhook spawns the `runner` function instead of a local thread."""
    import dataclasses

    from cilicon.service import orchestrator
    from cilicon.service.app import app as fastapi_app

    def _spawn(run_id, e):
        runner.spawn(run_id, dataclasses.asdict(e))

    orchestrator.dispatch = _spawn
    return fastapi_app
