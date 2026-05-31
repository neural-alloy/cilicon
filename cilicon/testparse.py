"""cilicon on-target test parsing: turn a test runner's output into per-test rows.

The `test:` phase passes on exit 0 — fine, but "exit 0" hides which of your 24
on-target tests actually ran. The two formats embedded test suites overwhelmingly
emit are Unity (`file.c:42:test_name:PASS`) and TAP (`ok 1 - desc`). Parsing them
turns "tests passed" into "23/24 passed, here's the one that didn't" in the report.

Pure string parsing; set `test_format: unity|tap` on a target, or leave it blank
to auto-detect.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TestRun:
    cases: list = field(default_factory=list)   # list of {"name": str, "ok": bool}

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if not c["ok"])

    @property
    def passed(self) -> int:
        return self.total - self.failed

    @property
    def parsed(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        return f"{self.passed}/{self.total} passed"

    def failures(self) -> list[str]:
        return [c["name"] for c in self.cases if not c["ok"]]


_UNITY = re.compile(r":(\w+):(PASS|FAIL|IGNORE)\b")
_TAP = re.compile(r"^(not ok|ok)\s+\d+\s*-?\s*(.*)$")


def detect(out: str) -> str:
    if re.search(r"^(?:not ok|ok)\s+\d+", out, re.M) or re.search(r"^\d+\.\.\d+", out, re.M):
        return "tap"
    if _UNITY.search(out) or re.search(r"\d+ Tests \d+ Failures", out):
        return "unity"
    return ""


def _parse_unity(out: str) -> TestRun:
    cases = []
    for line in out.splitlines():
        m = None
        for m in _UNITY.finditer(line):
            pass  # keep the last result token on the line
        if m:
            name, result = m.group(1), m.group(2)
            if result != "IGNORE":
                cases.append({"name": name, "ok": result == "PASS"})
    return TestRun(cases)


def _parse_tap(out: str) -> TestRun:
    cases = []
    for line in out.splitlines():
        m = _TAP.match(line.strip())
        if not m:
            continue
        status, desc = m.group(1), m.group(2).strip()
        # ignore TODO/SKIP directives as non-failures
        if re.search(r"#\s*(SKIP|TODO)", desc, re.I):
            continue
        name = re.sub(r"\s*#.*$", "", desc) or f"test {len(cases)+1}"
        cases.append({"name": name, "ok": status == "ok"})
    return TestRun(cases)


def parse(out: str, fmt: str = "") -> TestRun:
    fmt = fmt or detect(out or "")
    if fmt == "tap":
        return _parse_tap(out)
    if fmt == "unity":
        return _parse_unity(out)
    return TestRun([])
