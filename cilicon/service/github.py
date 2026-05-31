"""cilicon ↔ GitHub — the App that makes cilicon actual CI.

Three jobs:
  * verify inbound webhooks (HMAC, constant-time) — `verify_signature`
  * act AS the App: mint a JWT, exchange it for a short-lived installation
    token, clone the repo and create/update a check-run — `GitHubApp`
  * the dashboard's user login (OAuth) — `exchange_oauth_code`, `oauth_user`

`verify_signature` and `parse_event` are pure (no network), so the webhook's
security-critical path is unit-tested without a live GitHub.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .settings import settings

API = "https://api.github.com"


# ── webhook verification (pure) ──────────────────────────────────────────────

def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Validate the `X-Hub-Signature-256: sha256=...` header, constant-time."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@dataclass
class Event:
    """The slice of a webhook payload cilicon actually acts on."""
    kind: str                  # 'push' | 'pull_request' | 'installation' | other
    repo_id: int = 0
    full_name: str = ""
    default_branch: str = "main"
    private: bool = True
    installation_id: int = 0
    account_login: str = ""
    account_type: str = ""
    sha: str = ""
    ref: str = ""
    pr_number: Optional[int] = None
    sender: str = ""
    message: str = ""
    should_run: bool = False   # does this event warrant a CI run?


def parse_event(kind: str, payload: dict) -> Event:
    """Map a raw webhook payload to an Event. Pure — no I/O."""
    inst = (payload.get("installation") or {}).get("id", 0)
    repo = payload.get("repository") or {}
    e = Event(
        kind=kind,
        repo_id=repo.get("id", 0),
        full_name=repo.get("full_name", ""),
        default_branch=repo.get("default_branch", "main"),
        private=repo.get("private", True),
        installation_id=inst,
        sender=(payload.get("sender") or {}).get("login", ""),
    )

    if kind == "push":
        e.sha = payload.get("after", "")
        e.ref = payload.get("ref", "")
        head = payload.get("head_commit") or {}
        e.message = (head.get("message") or "").splitlines()[0] if head else ""
        # skip branch deletes (all-zero sha) and tag noise; run branch pushes
        e.should_run = bool(e.sha) and e.sha != "0" * 40 and e.ref.startswith("refs/heads/")

    elif kind == "pull_request":
        action = payload.get("action", "")
        pr = payload.get("pull_request") or {}
        e.sha = (pr.get("head") or {}).get("sha", "")
        e.ref = (pr.get("head") or {}).get("ref", "")
        e.pr_number = pr.get("number")
        e.message = pr.get("title", "")
        e.should_run = action in ("opened", "synchronize", "reopened")

    elif kind in ("installation", "installation_repositories"):
        acct = payload.get("installation", {}).get("account", {})
        e.account_login = acct.get("login", "")
        e.account_type = acct.get("type", "")
        e.installation_id = payload.get("installation", {}).get("id", inst)

    return e


# ── acting as the App ────────────────────────────────────────────────────────

def _app_jwt() -> str:
    import jwt  # PyJWT
    s = settings()
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": s.gh_app_id}
    return jwt.encode(payload, s.gh_app_private_key, algorithm="RS256")


class GitHubApp:
    def __init__(self) -> None:
        self._c = httpx.Client(timeout=30.0)
        self._tokens: dict[int, tuple[str, float]] = {}  # inst_id -> (token, expiry)

    def _app_headers(self) -> dict:
        return {"Authorization": f"Bearer {_app_jwt()}",
                "Accept": "application/vnd.github+json"}

    def installation_token(self, installation_id: int) -> str:
        """Short-lived token scoped to one installation; cached until ~expiry."""
        tok = self._tokens.get(installation_id)
        if tok and tok[1] - time.time() > 60:
            return tok[0]
        r = self._c.post(
            f"{API}/app/installations/{installation_id}/access_tokens",
            headers=self._app_headers(),
        )
        r.raise_for_status()
        data = r.json()
        # tokens last 1h; cache for 50m
        self._tokens[installation_id] = (data["token"], time.time() + 50 * 60)
        return data["token"]

    def clone_url(self, installation_id: int, full_name: str) -> str:
        token = self.installation_token(installation_id)
        return f"https://x-access-token:{token}@github.com/{full_name}.git"

    def _inst_headers(self, installation_id: int) -> dict:
        return {"Authorization": f"Bearer {self.installation_token(installation_id)}",
                "Accept": "application/vnd.github+json"}

    def create_check_run(self, installation_id: int, full_name: str, sha: str,
                         *, title: str, summary: str) -> int:
        r = self._c.post(
            f"{API}/repos/{full_name}/check-runs",
            headers=self._inst_headers(installation_id),
            json={
                "name": "cilicon / build + boot",
                "head_sha": sha,
                "status": "in_progress",
                "started_at": _iso(),
                "output": {"title": title, "summary": summary},
            },
        )
        r.raise_for_status()
        return r.json()["id"]

    def complete_check_run(self, installation_id: int, full_name: str, check_run_id: int,
                           *, success: bool, title: str, summary: str,
                           details_url: str = "") -> None:
        body = {
            "status": "completed",
            "conclusion": "success" if success else "failure",
            "completed_at": _iso(),
            "output": {"title": title, "summary": summary},
        }
        if details_url:
            body["details_url"] = details_url
        r = self._c.patch(
            f"{API}/repos/{full_name}/check-runs/{check_run_id}",
            headers=self._inst_headers(installation_id), json=body,
        )
        r.raise_for_status()


def _iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── dashboard login (user OAuth via the App's client id/secret) ──────────────

def oauth_authorize_url(state: str) -> str:
    s = settings()
    redirect = f"{s.base_url}/auth/callback"
    return (
        "https://github.com/login/oauth/authorize"
        f"?client_id={s.gh_client_id}&redirect_uri={redirect}"
        f"&scope=read:user%20read:org&state={state}"
    )


def exchange_oauth_code(code: str) -> str:
    s = settings()
    r = httpx.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={"client_id": s.gh_client_id, "client_secret": s.gh_client_secret,
              "code": code, "redirect_uri": f"{s.base_url}/auth/callback"},
        timeout=30.0,
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError("GitHub did not return an access token")
    return token


def oauth_user(token: str) -> dict:
    """The logged-in user + the org logins they belong to (for access scoping)."""
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=30.0, headers=h) as c:
        u = c.get(f"{API}/user").json()
        orgs = c.get(f"{API}/user/orgs").json()
    return {
        "github_id": u["id"],
        "login": u["login"],
        "name": u.get("name"),
        "avatar_url": u.get("avatar_url"),
        "email": u.get("email"),
        "org_logins": [o["login"] for o in orgs] + [u["login"]],  # personal acct too
    }
