"""cilicon persistence — a thin, synchronous client over ClickHouse's HTTP API.

Design: ClickHouse is append-optimized, so every mutable record is a
`ReplacingMergeTree` keyed on its id with a monotonic `ver` (microsecond unix);
an "update" is an insert of a new version and reads take the latest via `FINAL`.
Each row carries its full JSON in a `doc` column plus a few extracted key columns
for querying, so the service's flexible `**fields` records need no rigid schema.

We deliberately avoid a heavy SDK: everything is a POST of SQL to the HTTP
interface (the same lightweight approach the Supabase client used). Blobs (logs,
artifacts) live in a `blobs` table and are served back by the service's /dl route.

→ init the schema once with `init_schema()` (idempotent). Config: CLICKHOUSE_URL,
  CLICKHOUSE_USER, CLICKHOUSE_PASSWORD, CLICKHOUSE_DATABASE (see settings.py).
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from .settings import settings

# Deterministic-id namespace: upsert-by-natural-key tables derive a stable id
# from the key (so re-upserting the same org/user/project keeps one id, which
# ReplacingMergeTree then dedups on — a fresh uuid4 each time would not).
_NS = uuid.UUID("c1110c00-0000-4000-8000-000000000001")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ver() -> int:
    return time.time_ns() // 1000  # microsecond unix, monotonic enough per-write


def _uid(*parts) -> str:
    return str(uuid.uuid5(_NS, ":".join(str(p) for p in parts)))


DDL = [
    "CREATE DATABASE IF NOT EXISTS {db}",
    # mutable records — ReplacingMergeTree(ver), read latest with FINAL
    "CREATE TABLE IF NOT EXISTS {db}.orgs (id String, github_login String, doc String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY id",
    "CREATE TABLE IF NOT EXISTS {db}.users (id String, github_id Int64, doc String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY id",
    "CREATE TABLE IF NOT EXISTS {db}.memberships (org_id String, user_id String, doc String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY (org_id, user_id)",
    "CREATE TABLE IF NOT EXISTS {db}.installations (id Int64, doc String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY id",
    "CREATE TABLE IF NOT EXISTS {db}.projects (id String, github_repo_id Int64, org_id String, doc String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY id",
    "CREATE TABLE IF NOT EXISTS {db}.runs (id String, project_id String, status String, wall_seconds Float64,"
    " created_at DateTime64(3), doc String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY id",
    # append-only
    "CREATE TABLE IF NOT EXISTS {db}.target_results (id String, run_id String, target_id String,"
    " doc String, created_at DateTime64(3) DEFAULT now64(3))"
    " ENGINE = MergeTree ORDER BY (run_id, target_id)",
    # blobs (logs + artifacts) — upsert on (bucket, path)
    "CREATE TABLE IF NOT EXISTS {db}.blobs (bucket String, path String, content_type String, data String, ver UInt64)"
    " ENGINE = ReplacingMergeTree(ver) ORDER BY (bucket, path)",
]


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("'", "\\'")


class Store:
    def __init__(self) -> None:
        s = settings()
        if not s.ch_url:
            raise RuntimeError("ClickHouse not configured (CLICKHOUSE_URL)")
        self._db = s.ch_database
        self._url = s.ch_url.rstrip("/")
        auth = (s.ch_user, s.ch_password) if s.ch_user else None
        # No `database=` param: every table is fully qualified as {db}.table, so
        # the connection stays on `default` and DDL (CREATE DATABASE) can't fail
        # on "current database doesn't exist yet".
        self._c = httpx.Client(timeout=60.0, auth=auth)

    def close(self) -> None:
        self._c.close()

    # ── low-level HTTP ───────────────────────────────────────────────────

    def _exec(self, sql: str) -> str:
        r = self._c.post(self._url, content=sql.encode())
        if r.status_code >= 400:
            raise RuntimeError(f"clickhouse: {r.status_code} {r.text[:300]}")
        return r.text

    def _rows(self, sql: str) -> list[dict]:
        """Run a SELECT and parse each JSONEachRow line to a dict."""
        out = self._exec(sql + " FORMAT JSONEachRow").strip()
        return [json.loads(ln) for ln in out.splitlines() if ln]

    def _docs(self, sql_select_doc: str) -> list[dict]:
        """Run a `SELECT doc ...` and return the parsed docs."""
        return [json.loads(r["doc"]) for r in self._rows(sql_select_doc)]

    def _put(self, table: str, keycols: dict, doc: dict) -> dict:
        """Insert one row: extracted keycols + the full doc + a fresh ver."""
        row = {**keycols, "doc": json.dumps(doc), "ver": _ver()}
        self._exec(f"INSERT INTO {self._db}.{table} FORMAT JSONEachRow\n{json.dumps(row)}")
        return doc

    # ── orgs / users / memberships ───────────────────────────────────────

    def upsert_org(self, github_login: str, name: str = "") -> dict:
        oid = _uid("org", github_login)
        doc = {"id": oid, "github_login": github_login, "name": name or github_login,
               "updated_at": _now()}
        return self._put("orgs", {"id": oid, "github_login": github_login}, doc)

    def upsert_user(self, github_id: int, login: str, **extra) -> dict:
        uid = _uid("user", github_id)
        doc = {"id": uid, "github_id": github_id, "login": login, "updated_at": _now(), **extra}
        return self._put("users", {"id": uid, "github_id": github_id}, doc)

    def ensure_membership(self, org_id: str, user_id: str, role: str = "member") -> None:
        doc = {"org_id": org_id, "user_id": user_id, "role": role, "updated_at": _now()}
        self._put("memberships", {"org_id": org_id, "user_id": user_id}, doc)

    def orgs_for_user(self, user_id: str) -> list[dict]:
        rows = self._rows(
            f"SELECT doc FROM {self._db}.memberships FINAL WHERE user_id = '{_esc(user_id)}'")
        out = []
        for r in rows:
            m = json.loads(r["doc"])
            org = self.get_org(m["org_id"])
            if org:
                out.append({**org, "role": m.get("role", "member")})
        return out

    def get_org(self, org_id: str) -> Optional[dict]:
        d = self._docs(f"SELECT doc FROM {self._db}.orgs FINAL WHERE id = '{_esc(org_id)}'")
        return d[0] if d else None

    def orgs_by_logins(self, logins: list[str]) -> list[dict]:
        if not logins:
            return []
        inlist = ",".join("'" + _esc(x) + "'" for x in logins)
        return self._docs(
            f"SELECT doc FROM {self._db}.orgs FINAL WHERE github_login IN ({inlist})"
            " ORDER BY github_login ASC")

    # ── installs + projects ──────────────────────────────────────────────

    def upsert_installation(self, inst_id: int, org_id: str, account_login: str,
                            account_type: str = "") -> dict:
        doc = {"id": inst_id, "org_id": org_id, "account_login": account_login,
               "account_type": account_type, "updated_at": _now()}
        return self._put("installations", {"id": inst_id}, doc)

    def upsert_project(self, *, org_id: str, github_repo_id: int, full_name: str,
                       installation_id: int, default_branch: str = "main",
                       private: bool = True) -> dict:
        pid = _uid("project", github_repo_id)
        doc = {"id": pid, "org_id": org_id, "github_repo_id": github_repo_id,
               "full_name": full_name, "installation_id": installation_id,
               "default_branch": default_branch, "private": private, "updated_at": _now()}
        return self._put("projects",
                         {"id": pid, "github_repo_id": github_repo_id, "org_id": org_id}, doc)

    def project_by_repo_id(self, github_repo_id: int) -> Optional[dict]:
        d = self._docs(
            f"SELECT doc FROM {self._db}.projects FINAL WHERE github_repo_id = {int(github_repo_id)}")
        return d[0] if d else None

    def get_project(self, project_id: str) -> Optional[dict]:
        d = self._docs(f"SELECT doc FROM {self._db}.projects FINAL WHERE id = '{_esc(project_id)}'")
        return d[0] if d else None

    def projects_for_org(self, org_id: str) -> list[dict]:
        return self._docs(
            f"SELECT doc FROM {self._db}.projects FINAL WHERE org_id = '{_esc(org_id)}'"
            " ORDER BY id ASC")

    # ── runs ─────────────────────────────────────────────────────────────

    def create_run(self, **fields) -> dict:
        rid = fields.get("id") or str(uuid.uuid4())
        fields["id"] = rid
        fields.setdefault("status", "queued")
        fields.setdefault("created_at", _now())
        fields.setdefault("wall_seconds", 0)
        return self._put(
            "runs",
            {"id": rid, "project_id": fields.get("project_id", ""), "status": fields["status"],
             "wall_seconds": float(fields.get("wall_seconds") or 0), "created_at": fields["created_at"]},
            fields)

    def update_run(self, run_id: str, **patch) -> dict:
        cur = self.get_run(run_id) or {"id": run_id, "created_at": _now()}
        doc = {**cur, **patch, "updated_at": _now()}
        return self._put(
            "runs",
            {"id": run_id, "project_id": doc.get("project_id", ""), "status": doc.get("status", ""),
             "wall_seconds": float(doc.get("wall_seconds") or 0),
             "created_at": doc.get("created_at") or _now()},
            doc)

    def get_run(self, run_id: str) -> Optional[dict]:
        d = self._docs(f"SELECT doc FROM {self._db}.runs FINAL WHERE id = '{_esc(run_id)}'")
        return d[0] if d else None

    def runs_for_project(self, project_id: str, limit: int = 50) -> list[dict]:
        return self._docs(
            f"SELECT doc FROM {self._db}.runs FINAL WHERE project_id = '{_esc(project_id)}'"
            f" ORDER BY created_at DESC LIMIT {int(limit)}")

    def add_target_result(self, **fields) -> dict:
        tid = fields.get("id") or str(uuid.uuid4())
        fields["id"] = tid
        return self._put(
            "target_results",
            {"id": tid, "run_id": fields.get("run_id", ""), "target_id": fields.get("target_id", "")},
            fields)

    def target_results(self, run_id: str) -> list[dict]:
        return self._docs(
            f"SELECT doc FROM {self._db}.target_results WHERE run_id = '{_esc(run_id)}'"
            " ORDER BY target_id ASC")

    def target_result(self, tr_id: str) -> Optional[dict]:
        d = self._docs(
            f"SELECT doc FROM {self._db}.target_results WHERE id = '{_esc(tr_id)}' LIMIT 1")
        return d[0] if d else None

    # ── usage / quota ────────────────────────────────────────────────────

    def org_seconds_since(self, org_id: str, since_iso: str) -> float:
        projs = self.projects_for_org(org_id)
        if not projs:
            return 0.0
        inlist = ",".join("'" + _esc(p["id"]) + "'" for p in projs)
        rows = self._rows(
            f"SELECT sum(wall_seconds) AS s FROM {self._db}.runs FINAL"
            f" WHERE project_id IN ({inlist})"
            f" AND created_at >= parseDateTime64BestEffort('{_esc(since_iso)}')")
        return float(rows[0].get("s") or 0) if rows else 0.0

    # ── Storage (logs + artifacts) ───────────────────────────────────────

    def upload(self, bucket: str, path: str, data: bytes,
               content_type: str = "text/plain; charset=utf-8") -> str:
        row = {"bucket": bucket, "path": path, "content_type": content_type,
               "data": base64.b64encode(data).decode(), "ver": _ver()}
        self._exec(f"INSERT INTO {self._db}.blobs FORMAT JSONEachRow\n{json.dumps(row)}")
        return path

    def get_blob(self, bucket: str, path: str) -> Optional[tuple[bytes, str]]:
        rows = self._rows(
            f"SELECT data, content_type FROM {self._db}.blobs FINAL"
            f" WHERE bucket = '{_esc(bucket)}' AND path = '{_esc(path)}'")
        if not rows:
            return None
        return base64.b64decode(rows[0]["data"]), rows[0].get("content_type", "application/octet-stream")

    def signed_url(self, bucket: str, path: str, expires_in: int = 3600) -> str:
        # ClickHouse has no signed URLs; the service serves blobs from its /dl route.
        return f"/dl/blob/{bucket}/{path}"


def init_schema() -> None:
    """Create the database + tables (idempotent). Run once at deploy/startup."""
    s = settings()
    st = Store()
    for stmt in DDL:
        st._exec(stmt.format(db=s.ch_database))
    st.close()


_client: Optional[Store] = None


def db() -> Store:
    """Process-wide singleton (httpx.Client is thread-safe for our usage)."""
    global _client
    if _client is None:
        _client = Store()
    return _client
