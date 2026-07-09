"""cilicon vuln gate: judge an artifact's SBOM/scan against a policy — the part of
"is it safe to ship" you otherwise only learn after it's fielded.

The twin of `sizes.py`: pure functions over a scanner's JSON. The runner (or the
Action) runs `grype` on the artifact in the sandbox / on the pulled binary;
everything here parses that output and decides pass/fail against a policy. No
network, no scanner needed to test — feed it a scan dict.

Policies (`vuln_gate:` on a target):
  * ""/"none"  — report findings, never gate (the default)
  * "kev"      — block on an unwaived CISA Known-Exploited vuln (needs the KEV set)
  * "critical" — block on an unwaived Critical (and any KEV)
  * "high"     — block on an unwaived High or Critical (and any KEV)

`waivers:` is a list of CVE ids that are still *reported* but don't gate — a
signed, time-boxed exception is the carbonium DELIVERY_SPEC posture; here it's the
id list, and `doctor` reminds you an unexplained waiver is a smell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

_RANK = {"negligible": 0, "unknown": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_FLOOR = {"high": 3, "critical": 4}  # a severity policy blocks at/above this rank


@dataclass
class VulnHit:
    id: str
    severity: str            # normalized lower-case
    package: str = ""
    version: str = ""
    fixed_in: str = ""
    kev: bool = False        # on the CISA Known-Exploited list

    def to_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "package": self.package,
                "version": self.version, "fixed_in": self.fixed_in, "kev": self.kev}


@dataclass
class VulnReport:
    hits: list[VulnHit]
    policy: str = ""
    ok: bool = True
    detail: str = ""
    blocked: list[str] = field(default_factory=list)   # ids that actually gated
    waived: list[str] = field(default_factory=list)     # matched a waiver

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "policy": self.policy, "detail": self.detail,
            "blocked": self.blocked, "waived": self.waived,
            "counts": self.counts(), "hits": [h.to_dict() for h in self.hits],
        }

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for h in self.hits:
            out[h.severity] = out.get(h.severity, 0) + 1
        if any(h.kev for h in self.hits):
            out["kev"] = sum(1 for h in self.hits if h.kev)
        return out


def parse(scan, kev_ids: Optional[set] = None) -> list[VulnHit]:
    """Parse a scanner's JSON into hits. Understands grype's `{"matches": [...]}`
    and a plain list of `{id, severity, package, ...}`. `kev_ids` flags which are
    known-exploited. Returns [] if nothing parsed (an empty/None scan is 'clean')."""
    kev_ids = kev_ids or set()
    if not scan:
        return []
    matches = scan.get("matches") if isinstance(scan, dict) else scan
    if not isinstance(matches, list):
        return []
    hits: list[VulnHit] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        v = m.get("vulnerability", m)
        art = m.get("artifact", {}) if isinstance(m.get("artifact"), dict) else {}
        vid = str(v.get("id") or m.get("id") or "").strip()
        if not vid:
            continue
        sev = str(v.get("severity") or m.get("severity") or "unknown").strip().lower()
        fix = ""
        f = v.get("fix")
        if isinstance(f, dict):
            fix = ", ".join(f.get("versions", []) or [])
        hits.append(VulnHit(
            id=vid, severity=sev if sev in _RANK else "unknown",
            package=str(art.get("name") or m.get("package") or ""),
            version=str(art.get("version") or m.get("version") or ""),
            fixed_in=fix, kev=vid in kev_ids,
        ))
    return hits


def evaluate(scan, policy: str, waivers=None, kev_ids: Optional[set] = None) -> VulnReport:
    """Parse + judge. A policy of ""/"none" reports without gating. `kev_ids` is the
    CISA KEV catalog (passed in; None means we can't identify KEV, so a "kev" policy
    honestly degrades to report-only)."""
    waiver_set = {w.strip() for w in (waivers or []) if w and w.strip()}
    hits = parse(scan, kev_ids=kev_ids)
    pol = (policy or "").strip().lower()

    if pol in ("", "none"):
        return VulnReport(hits, policy=pol, ok=True,
                          detail=_summary(hits) or "no findings")

    if pol == "kev" and kev_ids is None:
        return VulnReport(hits, policy=pol, ok=True,
                          detail="kev policy set but no KEV catalog available — reporting only")

    floor = _FLOOR.get(pol)
    if pol != "kev" and floor is None:
        return VulnReport(hits, policy=pol, ok=True,
                          detail=f"unknown vuln policy '{pol}' — reporting only")

    blocked, waived = [], []
    for h in hits:
        gates = h.kev or (floor is not None and _RANK.get(h.severity, 0) >= floor)
        if not gates:
            continue
        if h.id in waiver_set:
            waived.append(h.id)
        else:
            blocked.append(h.id)

    if blocked:
        detail = f"blocked by {pol}: " + ", ".join(sorted(set(blocked))[:8])
        if len(set(blocked)) > 8:
            detail += f" (+{len(set(blocked)) - 8} more)"
        return VulnReport(hits, policy=pol, ok=False, detail=detail,
                          blocked=sorted(set(blocked)), waived=sorted(set(waived)))

    detail = _summary(hits) or "clean"
    if waived:
        detail += f" · {len(set(waived))} waived"
    return VulnReport(hits, policy=pol, ok=True, detail=detail, waived=sorted(set(waived)))


def _summary(hits: list[VulnHit]) -> str:
    if not hits:
        return ""
    by: dict[str, int] = {}
    for h in hits:
        by[h.severity] = by.get(h.severity, 0) + 1
    order = ["critical", "high", "medium", "low", "unknown", "negligible"]
    parts = [f"{by[s]} {s}" for s in order if by.get(s)]
    kev = sum(1 for h in hits if h.kev)
    if kev:
        parts.insert(0, f"{kev} KEV")
    return ", ".join(parts)
