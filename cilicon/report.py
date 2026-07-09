"""cilicon reports: turn a matrix run into machine-readable output.

`--json` is cilicon's own shape (every phase, timing, output tail, artifacts).
`--junit` is the lingua franca every CI dashboard already understands, so a
cilicon run drops straight into GitHub Actions / GitLab / Jenkins test reporting.

Pure functions over runner.TargetResult — no Modal, no I/O beyond the string
each returns, so they're trivially testable.
"""
from __future__ import annotations

import json
from xml.sax.saxutils import escape, quoteattr

from . import triage as triagemod


def _step_dict(step) -> dict | None:
    if step is None:
        return None
    return {
        "ok": step.ok,
        "seconds": round(step.seconds, 3),
        "detail": step.detail,
        "output_tail": "\n".join(step.output.strip().splitlines()[-12:]),
    }


def to_json(results, wall_seconds: float, triage_history: dict | None = None) -> str:
    payload = {
        "tool": "cilicon",
        "passed": sum(1 for r in results if r.ok),
        "total": len(results),
        "wall_seconds": round(wall_seconds, 3),
        "triage": triagemod.summarize(results, history=triage_history),
        "targets": [
            {
                "id": r.target.id,
                "validate": r.target.validate,
                "ok": r.ok,
                "seconds": round(r.seconds, 3),
                "error": r.error,
                "artifacts": r.artifacts,
                "build": _step_dict(r.build),
                "validate_step": _step_dict(r.validate),
                "test": _step_dict(r.test),
                "test_cases": r.test_cases or None,
                "size": _step_dict(r.size),
                "sizes": r.sizes or None,
                "vuln": _step_dict(getattr(r, "vuln", None)),
                "evidence": getattr(r, "evidence", None) or None,
            }
            for r in results
        ],
    }
    return json.dumps(payload, indent=2)


def _fmt_secs(s: float) -> str:
    s = s or 0
    return f"{int(s)//60}m{int(s)%60:02d}s" if s >= 60 else f"{s:.0f}s"


def to_markdown(results, wall_seconds: float) -> str:
    """A GitHub-flavoured summary for the check-run body: the matrix as a table,
    then the failing output tails. This is what a reviewer reads on the PR."""
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    head = "✅" if passed == total else "❌"
    lines = [
        f"### {head} cilicon — {passed}/{total} targets built **and** booted",
        f"_wall-clock {_fmt_secs(wall_seconds)}, owning zero hardware_",
        "",
        "| | Target | Build | On-target check | Size |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        mark = "✅" if r.ok else "❌"
        build = _fmt_secs(r.build.seconds) if (r.build and r.build.ok) else "✗"
        if r.error:
            check = f"infra error: {r.error}"
        elif r.build and not r.build.ok:
            check = "build failed"
        elif r.size and not r.size.ok:
            check = f"⚠ {r.size.detail}"
        elif getattr(r, "vuln", None) and not r.vuln.ok:
            check = f"🛡 {r.vuln.detail}"
        elif r.validate:
            check = r.validate.detail + (" · tests ✓" if (r.test and r.test.ok) else "")
            if r.test and not r.test.ok:
                check = f"test: {r.test.detail}"
        else:
            check = "—"
        if r.ok and any(e.get("signed") for e in getattr(r, "evidence", []) or []):
            check += " · 🔏 signed"
        flash = r.sizes.get("flash")
        size = "" if not flash else f"{flash/1024:.0f}K flash"
        lines.append(f"| {mark} | `{r.target.id}` | {build} | {check} | {size} |")

    grids = _sweep_grids(results)
    if grids:
        lines += ["", "#### Fan-out sweep", "",
                  "_one spec → many cells, each its own cloud container_"] + grids

    lines += _triage_block(results) + _evidence_line(results)

    fails = [r for r in results if not r.ok]
    if fails:
        lines += ["", "<details><summary>Failure logs</summary>", ""]
        for r in fails:
            lines.append(f"**`{r.target.id}`**")
            failed_tests = [c["name"] for c in r.test_cases if not c["ok"]]
            if failed_tests:
                lines.append("failed tests: " + ", ".join(f"`{n}`" for n in failed_tests))
            lines += ["```", _failure_text(r)[:3000], "```"]
        lines.append("</details>")
    return "\n".join(lines)


# ---- sweep grid: turn a matrix fan-out into a ✅/❌ heatmap in the PR --------

def _ordered_unique(values):
    seen = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def _cell_status(rs) -> str:
    if not rs:
        return "·"
    return "✅" if all(r.ok for r in rs) else "❌"


def _grid_1d(axis: str, rs) -> list[str]:
    vals = _ordered_unique(r.target.matrix_values[axis] for r in rs)
    by: dict = {}
    for r in rs:
        by.setdefault(r.target.matrix_values[axis], []).append(r)
    return [
        "", f"**sweep: `{axis}`**", "",
        "| " + " | ".join(vals) + " |",
        "|" + "---|" * len(vals),
        "| " + " | ".join(_cell_status(by.get(v, [])) for v in vals) + " |",
    ]


def _grid_2d(a0: str, a1: str, rs) -> list[str]:
    rows = _ordered_unique(r.target.matrix_values[a0] for r in rs)
    cols = _ordered_unique(r.target.matrix_values[a1] for r in rs)
    cell: dict = {}
    for r in rs:
        cell.setdefault((r.target.matrix_values[a0], r.target.matrix_values[a1]), []).append(r)
    out = [
        "", f"**sweep: `{a0}` × `{a1}`**", "",
        f"| `{a0}` \\ `{a1}` | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for rv in rows:
        cells = " | ".join(_cell_status(cell.get((rv, cv), [])) for cv in cols)
        out.append(f"| {rv} | " + cells + " |")
    return out


def _sweep_grids(results) -> list[str]:
    """Render a ✅/❌ grid per matrix group (1 axis → a row, 2 axes → a table).
    Groups with 3+ axes are left to the flat table above."""
    groups: dict = {}
    for r in results:
        mv = getattr(r.target, "matrix_values", None) or {}
        if mv:
            groups.setdefault(tuple(sorted(mv)), []).append(r)
    out: list[str] = []
    for axes, rs in groups.items():
        if len(axes) == 1:
            out += _grid_1d(axes[0], rs)
        elif len(axes) == 2:
            out += _grid_2d(axes[0], axes[1], rs)
    return out


def _triage_block(results) -> list[str]:
    """Root-cause clusters — only when more than one target failed (a single red
    needs no clustering, and an all-green run stays untouched)."""
    tri = triagemod.summarize(results)
    if not (tri["failures"] > 1 and tri["root_causes"]):
        return []
    out = ["", f"#### {tri['root_causes']} root cause(s) across {tri['failures']} failures", ""]
    for cl in tri["clusters"]:
        tags = ", ".join(f"`{t}`" for t in cl["targets"])
        since = f" · seen since {cl['since']}" if cl.get("since") else ""
        label = cl["signature"] or cl["detail"]
        out.append(f"- **{cl['count']}×** {cl['phase']} · {label}{since} — {tags}")
    return out


def _evidence_line(results) -> list[str]:
    ev = [e for r in results for e in (getattr(r, "evidence", []) or [])]
    if not ev:
        return []
    signed = sum(1 for e in ev if e.get("signed"))
    return ["", f"🔏 evidence: {signed}/{len(ev)} artifact(s) signed · SBOM + SLSA provenance bundled"]


def _first_fail(r):
    """The step that actually failed, in pipeline order — so the one-line
    failure message names the real culprit (e.g. size), not whatever ran last."""
    for step in (r.build, r.size, getattr(r, "vuln", None), r.validate, r.test):
        if step and not step.ok:
            return step
    return None


def _failure_msg(r) -> str:
    if r.error:
        return r.error
    step = _first_fail(r)
    return f"{step.name}: {step.detail}" if step else "failed"


def _failure_text(r) -> str:
    if r.error:
        return f"orchestration error: {r.error}"
    bits = []
    for step in (r.build, r.size, getattr(r, "vuln", None), r.validate, r.test):
        if step and not step.ok:
            tail = "\n".join(step.output.strip().splitlines()[-12:])
            bits.append(f"[{step.name}] {step.detail}\n{tail}")
    return "\n\n".join(bits) or "failed"


def to_junit(results, wall_seconds: float) -> str:
    passed = sum(1 for r in results if r.ok)
    failures = len(results) - passed
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        f'<testsuite name="cilicon" tests="{len(results)}" failures="{failures}" '
        f'time="{wall_seconds:.3f}">'
    )
    for r in results:
        name = quoteattr(r.target.id)
        cls = quoteattr(f"cilicon.{r.target.validate}")
        lines.append(
            f'  <testcase name={name} classname={cls} time="{r.seconds:.3f}">'
        )
        if not r.ok:
            msg = quoteattr(_failure_msg(r))
            lines.append(f'    <failure message={msg}>{escape(_failure_text(r))}</failure>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines)
