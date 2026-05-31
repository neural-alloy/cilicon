"""cilicon telemetry: structured, append-only events for every run.

A cilicon run fans many builds+boots across the cloud; without observability you
only see the final table. This emits one JSON object per lifecycle event —
`run.started`, `target.started`, `target.completed`, `run.completed` — each with
timings, per-phase status, tier, GPU, and size numbers. Point it at a JSONL file
(or stdout, or an HTTP collector) and you get a queryable record: flaky targets,
slow phases, which tier fails most, build-vs-boot time over weeks.

The event-builders are pure functions over runner.TargetResult, so they're
unit-testable with no sink and no clock. Sinks never raise into the run — a
telemetry hiccup must not fail your CI.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("cilicon-ci")
    except Exception:
        return "0.0.0"


# ---- pure event builders (no I/O, no clock) --------------------------------

def _phase(step) -> Optional[dict]:
    if step is None:
        return None
    return {"ok": step.ok, "seconds": round(step.seconds, 3), "detail": step.detail}


def target_event(run_id: str, result, ts: float) -> dict:
    t = result.target
    from . import presets
    try:
        gpu = presets.resolve(t).gpu
    except Exception:
        gpu = t.gpu or None
    return {
        "ts": round(ts, 3),
        "run_id": run_id,
        "event": "target.completed",
        "target": t.id,
        "tier": t.validate,
        "gpu": gpu,
        "ok": result.ok,
        "seconds": round(result.seconds, 3),
        "phases": {
            "build": _phase(result.build),
            "size": _phase(result.size),
            "validate": _phase(result.validate),
            "test": _phase(result.test),
        },
        "sizes": result.sizes or None,
        "error": result.error,
    }


def run_summary(results, wall_seconds: float) -> dict:
    """Aggregate metrics — the body of the run.completed event, also reusable
    for a dashboard. Pure."""
    passed = sum(1 for r in results if r.ok)
    by_tier: dict[str, int] = {}
    totals = {"build": 0.0, "size": 0.0, "validate": 0.0, "test": 0.0}
    for r in results:
        by_tier[r.target.validate] = by_tier.get(r.target.validate, 0) + 1
        for name in totals:
            step = getattr(r, name, None)
            if step:
                totals[name] += step.seconds
    return {
        "passed": passed,
        "failed": len(results) - passed,
        "total": len(results),
        "wall_seconds": round(wall_seconds, 3),
        "phase_seconds": {k: round(v, 3) for k, v in totals.items()},
        "by_tier": by_tier,
    }


# ---- sinks (never raise into the run) --------------------------------------

class NullSink:
    def emit(self, event: dict) -> None: ...
    def close(self) -> None: ...


class JsonlSink:
    """Append one JSON object per line. Opened lazily, flushed per event."""
    def __init__(self, path: str):
        self.path = path
        self._f = None

    def emit(self, event: dict) -> None:
        try:
            if self._f is None:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                self._f = open(self.path, "a", encoding="utf-8")
            self._f.write(json.dumps(event) + "\n")
            self._f.flush()
        except Exception:
            pass  # telemetry must never break a run

    def close(self) -> None:
        try:
            if self._f is not None:
                self._f.close()
        except Exception:
            pass


class StdoutSink:
    def __init__(self, prefix: str = "cilicon.telemetry "):
        self.prefix = prefix

    def emit(self, event: dict) -> None:
        try:
            print(self.prefix + json.dumps(event), flush=True)
        except Exception:
            pass

    def close(self) -> None: ...


class HttpSink:
    """Best-effort POST of each event to a collector (OTLP-style ingestion).
    Failures are swallowed; never blocks a run for long."""
    def __init__(self, url: str, timeout: float = 2.0):
        self.url = url
        self.timeout = timeout

    def emit(self, event: dict) -> None:
        try:
            import urllib.request
            req = urllib.request.Request(
                self.url, data=json.dumps(event).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=self.timeout).close()
        except Exception:
            pass

    def close(self) -> None: ...


class MultiSink:
    def __init__(self, sinks):
        self.sinks = [s for s in sinks if s is not None]

    def emit(self, event: dict) -> None:
        for s in self.sinks:
            s.emit(event)

    def close(self) -> None:
        for s in self.sinks:
            s.close()


def make_sink(path: str = "", to_stdout: bool = False, url: str = "") -> object:
    """Build a sink from explicit args + env (CILICON_TELEMETRY[_URL],
    CILICON_TELEMETRY_STDOUT). Returns NullSink if nothing is configured."""
    path = path or os.environ.get("CILICON_TELEMETRY", "")
    url = url or os.environ.get("CILICON_TELEMETRY_URL", "")
    to_stdout = to_stdout or bool(os.environ.get("CILICON_TELEMETRY_STDOUT"))
    sinks = []
    if path:
        sinks.append(JsonlSink(path))
    if to_stdout:
        sinks.append(StdoutSink())
    if url:
        sinks.append(HttpSink(url))
    if not sinks:
        return NullSink()
    return sinks[0] if len(sinks) == 1 else MultiSink(sinks)


# ---- recorder: glues the run lifecycle to a sink ---------------------------

class Recorder:
    def __init__(self, sink=None, run_id: str = "", clock=time.time):
        self.sink = sink or NullSink()
        self.clock = clock
        self.run_id = run_id or self._gen_id()
        self.version = _version()

    def _gen_id(self) -> str:
        # time-based + a little entropy; this runs in normal Python (not the
        # workflow sandbox), so time/random are available.
        import random
        return f"run_{int(self.clock()*1000):x}{random.randint(0, 0xffff):04x}"

    def run_started(self, target_ids: list[str]) -> None:
        self.sink.emit({
            "ts": round(self.clock(), 3), "run_id": self.run_id,
            "event": "run.started", "version": self.version,
            "targets": list(target_ids), "count": len(target_ids),
        })

    def target_started(self, target_id: str, tier: str = "", gpu=None) -> None:
        self.sink.emit({
            "ts": round(self.clock(), 3), "run_id": self.run_id,
            "event": "target.started", "target": target_id,
            "tier": tier, "gpu": gpu,
        })

    def target_completed(self, result) -> None:
        self.sink.emit(target_event(self.run_id, result, self.clock()))

    def run_completed(self, results, wall_seconds: float) -> None:
        ev = {
            "ts": round(self.clock(), 3), "run_id": self.run_id,
            "event": "run.completed",
        }
        ev.update(run_summary(results, wall_seconds))
        self.sink.emit(ev)

    def close(self) -> None:
        self.sink.close()
