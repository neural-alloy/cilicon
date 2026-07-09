"""cilicon triage: collapse a wall of red into a handful of root causes.

A 40-target sweep that fails the same way on 30 boards is one bug, not thirty. This
buckets failing targets by a structural *fingerprint* — (which phase failed, which
crash signature, a normalized shingle of the tail) — so the report says "30 targets
→ 2 root causes" and, with a history file, "same failure as last run."

Pure, and it reuses two cilicon primitives so it stays in the house style: the
tier's `presets.crash_signatures` (the fault-marker dictionary) and
`baseline.normalize_log` (which already scrubs addresses/timestamps so the same
crash on two boards hashes alike). Advisory only: triage never flips a pass/fail —
a green check is still judged entirely by the runner (cilicon never overclaims).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import baseline, presets

_PHASES = ("build", "size", "validate", "test")


def _first_fail(result):
    """(phase-name, StepResult) of the first failed step in pipeline order, or
    ('infra', None) for an orchestration error, or (None, None) if it passed."""
    if getattr(result, "error", ""):
        return "infra", None
    for name in _PHASES:
        step = getattr(result, name, None)
        if step is not None and not step.ok:
            return name, step
    return None, None


def _signature(tier: str, output: str) -> str:
    """The first tier crash marker present in the output (the human-legible bucket),
    or "" if none — a build error has no crash signature, that's fine."""
    for marker in presets.crash_signatures(tier):
        if marker and marker in output:
            return marker
    return ""


def _shingle(output: str, lines: int = 6) -> str:
    """A normalized tail of the failing output — addresses/timestamps scrubbed so the
    same fault on different boards collapses to one shingle."""
    norm = baseline.normalize_log(output or "")
    return "\n".join(norm.splitlines()[-lines:]).strip()


@dataclass
class Fingerprint:
    phase: str            # build | size | validate | test | infra
    signature: str        # crash marker, or ""
    key: str              # stable short hash of (tier, phase, signature, shingle)
    detail: str           # one-line human description

    def to_dict(self) -> dict:
        return {"phase": self.phase, "signature": self.signature,
                "key": self.key, "detail": self.detail}


def fingerprint(result) -> Fingerprint | None:
    """A structural fingerprint of a *failed* target, or None if it passed."""
    phase, step = _first_fail(result)
    if phase is None:
        return None
    tier = getattr(result.target, "validate", "")
    if phase == "infra":
        output, sig = getattr(result, "error", ""), ""
        detail = f"infra · {output[:60]}"
    else:
        output = step.output if step else ""
        sig = _signature(tier, output)
        detail = f"{phase} · {sig or (step.detail if step else '')}"[:80]
    shingle = "" if phase == "infra" else _shingle(output)
    raw = "\x1f".join([tier, phase, sig, shingle])
    key = hashlib.sha1(raw.encode()).hexdigest()[:8]
    return Fingerprint(phase=phase, signature=sig, key=key, detail=detail)


@dataclass
class Cluster:
    key: str
    phase: str
    signature: str
    detail: str
    targets: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.targets)

    def to_dict(self) -> dict:
        return {"key": self.key, "phase": self.phase, "signature": self.signature,
                "detail": self.detail, "count": self.count, "targets": self.targets}


def cluster(results) -> list[Cluster]:
    """Group the failing targets by fingerprint, most-common root cause first."""
    by: dict[str, Cluster] = {}
    for r in results:
        if getattr(r, "ok", False):
            continue
        fp = fingerprint(r)
        if fp is None:
            continue
        cl = by.get(fp.key)
        if cl is None:
            cl = Cluster(key=fp.key, phase=fp.phase, signature=fp.signature, detail=fp.detail)
            by[fp.key] = cl
        cl.targets.append(r.target.id)
    return sorted(by.values(), key=lambda c: (-c.count, c.key))


def summarize(results, history: dict | None = None) -> dict:
    """Report-ready triage: how many failures, and the ranked root-cause clusters.
    `history` (key -> first-seen tag) marks a cluster 'known' vs 'new' — advisory."""
    clusters = cluster(results)
    hist = history or {}
    out = []
    for c in clusters:
        d = c.to_dict()
        d["status"] = "known" if c.key in hist else "new"
        if c.key in hist:
            d["since"] = hist[c.key]
        out.append(d)
    failures = sum(1 for r in results if not getattr(r, "ok", False))
    return {"failures": failures, "root_causes": len(clusters), "clusters": out}


def merge_history(results, history: dict | None, tag: str) -> dict:
    """Fold this run's clusters into a fingerprint history (key -> first-seen tag),
    so a later run can say 'known failure since <tag>'. Never mutates the input."""
    hist = dict(history or {})
    for c in cluster(results):
        hist.setdefault(c.key, tag)
    return hist
