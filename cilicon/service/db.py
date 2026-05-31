"""cilicon persistence — a thin, synchronous client over Supabase's REST APIs.

We deliberately avoid the heavy supabase-py SDK: everything cilicon needs is a
handful of PostgREST + Storage calls, and a small wrapper keeps the dependency
surface (httpx only) tiny and the behaviour obvious. All calls use the SERVICE
ROLE key and run server-side, so they bypass RLS (see migrations/0001_init.sql).

Tables are plain dicts here; the orchestrator and dashboard own their shapes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .settings import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Supabase:
    def __init__(self) -> None:
        s = settings()
        if not s.configured:
            raise RuntimeError("Supabase not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY)")
        self._rest = f"{s.supabase_url}/rest/v1"
        self._storage = f"{s.supabase_url}/storage/v1"
        self._base = s.supabase_url
        self._h = {
            "apikey": s.supabase_service_key,
            "Authorization": f"Bearer {s.supabase_service_key}",
        }
        self._c = httpx.Client(timeout=30.0, headers=self._h)

    def close(self) -> None:
        self._c.close()

    # ── low-level PostgREST ──────────────────────────────────────────────

    def _select(self, table: str, *, params: dict) -> list[dict]:
        r = self._c.get(f"{self._rest}/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def _insert(self, table: str, row: dict, *, upsert_on: str = "") -> dict:
        headers = {"Prefer": "return=representation"}
        params = {}
        if upsert_on:
            headers["Prefer"] = "return=representation,resolution=merge-duplicates"
            params["on_conflict"] = upsert_on
        r = self._c.post(f"{self._rest}/{table}", json=row, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) else data

    def _update(self, table: str, patch: dict, *, params: dict) -> dict:
        params = {**params, "select": "*"}
        r = self._c.patch(
            f"{self._rest}/{table}", json=patch, params=params,
            headers={"Prefer": "return=representation"},
        )
        r.raise_for_status()
        data = r.json()
        return data[0] if data else {}

    def _one(self, rows: list[dict]) -> Optional[dict]:
        return rows[0] if rows else None

    # ── identity ─────────────────────────────────────────────────────────

    def upsert_org(self, github_login: str, name: str = "") -> dict:
        return self._insert(
            "orgs", {"github_login": github_login, "name": name or github_login},
            upsert_on="github_login",
        )

    def upsert_user(self, github_id: int, login: str, **extra) -> dict:
        row = {"github_id": github_id, "login": login, **extra}
        return self._insert("users", row, upsert_on="github_id")

    def ensure_membership(self, org_id: str, user_id: str, role: str = "member") -> None:
        self._insert(
            "memberships", {"org_id": org_id, "user_id": user_id, "role": role},
            upsert_on="org_id,user_id",
        )

    def orgs_for_user(self, user_id: str) -> list[dict]:
        rows = self._select(
            "memberships",
            params={"user_id": f"eq.{user_id}", "select": "role,orgs(*)"},
        )
        return [{**r["orgs"], "role": r["role"]} for r in rows if r.get("orgs")]

    def get_org(self, org_id: str) -> Optional[dict]:
        return self._one(self._select("orgs", params={"id": f"eq.{org_id}", "select": "*"}))

    def orgs_by_logins(self, logins: list[str]) -> list[dict]:
        """Orgs whose GitHub login is in the set (the dashboard read-scope)."""
        if not logins:
            return []
        quoted = ",".join('"' + l.replace('"', "") + '"' for l in logins)
        return self._select("orgs", params={
            "github_login": f"in.({quoted})", "select": "*", "order": "github_login.asc"})

    # ── installs + projects ──────────────────────────────────────────────

    def upsert_installation(self, inst_id: int, org_id: str, account_login: str,
                            account_type: str = "") -> dict:
        return self._insert(
            "installations",
            {"id": inst_id, "org_id": org_id, "account_login": account_login,
             "account_type": account_type},
            upsert_on="id",
        )

    def upsert_project(self, *, org_id: str, github_repo_id: int, full_name: str,
                       installation_id: int, default_branch: str = "main",
                       private: bool = True) -> dict:
        return self._insert(
            "projects",
            {"org_id": org_id, "github_repo_id": github_repo_id,
             "full_name": full_name, "installation_id": installation_id,
             "default_branch": default_branch, "private": private},
            upsert_on="github_repo_id",
        )

    def project_by_repo_id(self, github_repo_id: int) -> Optional[dict]:
        return self._one(self._select(
            "projects", params={"github_repo_id": f"eq.{github_repo_id}", "select": "*"}))

    def get_project(self, project_id: str) -> Optional[dict]:
        return self._one(self._select(
            "projects", params={"id": f"eq.{project_id}", "select": "*"}))

    def projects_for_org(self, org_id: str) -> list[dict]:
        return self._select(
            "projects",
            params={"org_id": f"eq.{org_id}", "select": "*", "order": "full_name.asc"})

    # ── runs ─────────────────────────────────────────────────────────────

    def create_run(self, **fields) -> dict:
        fields.setdefault("status", "queued")
        return self._insert("runs", fields)

    def update_run(self, run_id: str, **patch) -> dict:
        return self._update("runs", patch, params={"id": f"eq.{run_id}"})

    def get_run(self, run_id: str) -> Optional[dict]:
        return self._one(self._select("runs", params={"id": f"eq.{run_id}", "select": "*"}))

    def runs_for_project(self, project_id: str, limit: int = 50) -> list[dict]:
        return self._select("runs", params={
            "project_id": f"eq.{project_id}", "select": "*",
            "order": "created_at.desc", "limit": str(limit)})

    def add_target_result(self, **fields) -> dict:
        return self._insert("target_results", fields)

    def target_results(self, run_id: str) -> list[dict]:
        return self._select("target_results", params={
            "run_id": f"eq.{run_id}", "select": "*", "order": "target_id.asc"})

    # ── usage / quota ────────────────────────────────────────────────────

    def org_seconds_since(self, org_id: str, since_iso: str) -> float:
        """Sum of run wall-seconds for an org's projects since a timestamp.
        Used as a soft quota gate before scheduling on the platform Modal."""
        projs = self.projects_for_org(org_id)
        if not projs:
            return 0.0
        ids = ",".join(p["id"] for p in projs)
        rows = self._select("runs", params={
            "project_id": f"in.({ids})", "created_at": f"gte.{since_iso}",
            "select": "wall_seconds"})
        return float(sum((r.get("wall_seconds") or 0) for r in rows))

    # ── Storage (logs + artifacts) ───────────────────────────────────────

    def upload(self, bucket: str, path: str, data: bytes,
               content_type: str = "text/plain; charset=utf-8") -> str:
        r = self._c.post(
            f"{self._storage}/object/{bucket}/{path}",
            content=data,
            headers={**self._h, "content-type": content_type, "x-upsert": "true"},
        )
        r.raise_for_status()
        return path

    def signed_url(self, bucket: str, path: str, expires_in: int = 3600) -> str:
        r = self._c.post(
            f"{self._storage}/object/sign/{bucket}/{path}",
            json={"expiresIn": expires_in},
        )
        r.raise_for_status()
        # PostgREST returns a path relative to /storage/v1
        return self._base + "/storage/v1" + r.json()["signedURL"]


_client: Optional[Supabase] = None


def db() -> Supabase:
    """Process-wide singleton (httpx.Client is thread-safe for our usage)."""
    global _client
    if _client is None:
        _client = Supabase()
    return _client
