"""cilicon regression tracking: compare a run to a saved baseline.

A green check tells you it builds and boots *today*. A baseline tells you whether
this PR made it **worse** — the binary grew, the boot got slower, or the boot log
drifted. Same machinery as the run, no extra emulation: we already measure flash/
RAM (sizes.py) and time each phase, so the baseline is just "remember last good
and diff."

A baseline file is JSON: `{ "<target id>": {flash, ram, boot_seconds, log} }`.
  * `cilicon run --update-baseline b.json`  → write the current run as the baseline
  * `cilicon run --baseline b.json`         → compare, surface regressions
  * `--fail-on-regression`                  → a size regression fails the run
    (boot-time and log drift are noisy in emulation, so they only warn)

Pure functions over runner.TargetResult — no Modal, no clock.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Optional


# ---- normalize a boot log so diffs are meaningful, not noise ---------------

_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_TS_CLOCK = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
_TS_PAREN = re.compile(r"\(\s*\d+(?:\.\d+)?\s*\)")     # ESP-IDF "(1234)" / "(1.23)"
_TRAIL = re.compile(r"[ \t]+$", re.MULTILINE)


def normalize_log(s: str) -> str:
    s = _ADDR.sub("0xADDR", s or "")
    s = _TS_CLOCK.sub("TS", s)
    s = _TS_PAREN.sub("(T)", s)
    s = _TRAIL.sub("", s)
    return s.strip()


# ---- build / save / load ---------------------------------------------------

def build(results) -> dict:
    """Snapshot the measurable, regress-able facts of a run."""
    out: dict = {}
    for r in results:
        if not r.ok:
            continue  # only baseline known-good targets
        entry: dict = {}
        if r.sizes.get("flash") is not None:
            entry["flash"] = r.sizes["flash"]
        if r.sizes.get("ram") is not None:
            entry["ram"] = r.sizes["ram"]
        if r.validate:
            entry["boot_seconds"] = round(r.validate.seconds, 3)
            entry["log"] = normalize_log(r.validate.output)
        out[r.target.id] = entry
    return out


def save(baseline: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(baseline, f, indent=2)


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


# ---- compare ---------------------------------------------------------------

@dataclass
class Regression:
    target: str
    metric: str          # "flash" | "ram" | "boot" | "log"
    detail: str
    severe: bool         # eligible to FAIL the run (only size, by default)


def _pct(old: float, new: float) -> float:
    return ((new - old) / old * 100.0) if old else 0.0


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n/1024/1024:.1f}M"
    if n >= 1024:
        return f"{n/1024:.1f}K"
    return f"{n}B"


def compare(results, baseline: dict, pct: float = 5.0) -> list[Regression]:
    """Regressions of this run vs the baseline. Size growth past `pct` is severe
    (can fail); boot-time growth and log drift only warn."""
    regs: list[Regression] = []
    for r in results:
        base = baseline.get(r.target.id)
        if not base:
            continue

        for metric in ("flash", "ram"):
            old = base.get(metric)
            new = r.sizes.get(metric)
            if old is None or new is None:
                continue
            grew = _pct(old, new)
            if new > old and grew >= pct:
                regs.append(Regression(
                    r.target.id, metric,
                    f"{metric} {_human(old)} → {_human(new)} (+{grew:.1f}%)",
                    severe=True))

        old_b = base.get("boot_seconds")
        new_b = r.validate.seconds if r.validate else None
        if old_b and new_b and new_b > old_b and _pct(old_b, new_b) >= max(pct, 20.0) and (new_b - old_b) >= 0.5:
            regs.append(Regression(
                r.target.id, "boot",
                f"boot {old_b:.1f}s → {new_b:.1f}s (+{_pct(old_b, new_b):.0f}%)",
                severe=False))

        old_log = base.get("log")
        new_log = normalize_log(r.validate.output) if r.validate else None
        if old_log is not None and new_log is not None and old_log != new_log:
            diff = "\n".join(list(difflib.unified_diff(
                old_log.splitlines(), new_log.splitlines(),
                "baseline", "current", lineterm=""))[:12])
            regs.append(Regression(r.target.id, "log", f"boot log changed:\n{diff}", severe=False))
    return regs


def has_severe(regs: list[Regression]) -> bool:
    return any(g.severe for g in regs)
