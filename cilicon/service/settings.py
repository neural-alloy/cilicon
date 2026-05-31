"""cilicon service config — everything comes from the environment (12-factor).

Nothing here imports FastAPI/Modal, so it's safe to import in tests. Call
`settings()` for a cached, validated bundle; `require()` raises a clear error
the first time a needed secret is missing rather than 500-ing mid-request.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # --- Supabase (data + storage) ---------------------------------------
    supabase_url: str            # https://<ref>.supabase.co
    supabase_service_key: str    # service_role key — SERVER-SIDE ONLY

    # --- GitHub App ------------------------------------------------------
    gh_app_id: str
    gh_app_private_key: str      # PEM contents (or @path, resolved below)
    gh_webhook_secret: str
    gh_client_id: str            # OAuth (dashboard login)
    gh_client_secret: str

    # --- platform compute (Modal) ----------------------------------------
    # Cilicon owns one Modal workspace; tenant runs execute there. These cap the
    # blast radius of running many orgs' untrusted build commands in it.
    max_parallel_targets: int    # ceiling on concurrent sandboxes per run
    org_monthly_seconds: int     # soft quota: build+validate seconds / org / month

    # --- web --------------------------------------------------------------
    base_url: str                # public URL, for OAuth callback + check links
    session_secret: str          # signs the session cookie
    log_bucket: str
    artifact_bucket: str

    @property
    def configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


def _key(raw: str) -> str:
    """A PEM passed inline, or as @/path/to/key.pem, or with \\n escapes."""
    raw = raw.strip()
    if raw.startswith("@"):
        with open(os.path.expanduser(raw[1:])) as f:
            return f.read()
    return raw.replace("\\n", "\n")


@functools.lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        supabase_url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
        supabase_service_key=os.environ.get("SUPABASE_SERVICE_KEY", ""),
        gh_app_id=os.environ.get("GITHUB_APP_ID", ""),
        gh_app_private_key=_key(os.environ.get("GITHUB_APP_PRIVATE_KEY", "")),
        gh_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
        gh_client_id=os.environ.get("GITHUB_CLIENT_ID", ""),
        gh_client_secret=os.environ.get("GITHUB_CLIENT_SECRET", ""),
        max_parallel_targets=int(os.environ.get("CILICON_MAX_PARALLEL_TARGETS", "8")),
        org_monthly_seconds=int(os.environ.get("CILICON_ORG_MONTHLY_SECONDS", "36000")),
        base_url=os.environ.get("CILICON_BASE_URL", "http://localhost:8000").rstrip("/"),
        session_secret=os.environ.get("CILICON_SESSION_SECRET", "dev-insecure-change-me"),
        log_bucket=os.environ.get("CILICON_LOG_BUCKET", "logs"),
        artifact_bucket=os.environ.get("CILICON_ARTIFACT_BUCKET", "artifacts"),
    )


def require(*names: str) -> None:
    """Fail loudly if a required env var is unset (call at startup / per route)."""
    s = settings()
    missing = []
    mapping = {
        "supabase": (s.supabase_url, s.supabase_service_key),
        "github_app": (s.gh_app_id, s.gh_app_private_key, s.gh_webhook_secret),
        "github_oauth": (s.gh_client_id, s.gh_client_secret),
    }
    for n in names:
        vals = mapping.get(n, ())
        if not all(vals):
            missing.append(n)
    if missing:
        raise RuntimeError(
            "cilicon service missing config for: " + ", ".join(missing) +
            " — see .env.example / SERVICE.md"
        )
