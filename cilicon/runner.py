"""cilicon runner: fan every target out across Modal cloud containers, in parallel.

Each target gets its OWN fully-custom image (its toolchain) and runs in its own
Modal Sandbox. Inside that same container we (1) build the artifact, (2) prove
it actually runs by booting/running it (the validate tier, co-located with the
build), and (3) optionally run a test suite ON the target. No hardware in the room.

`modal` is imported lazily so the pure logic here — the phase script, the
output parser, the pass/fail judge — stays importable and unit-testable without
Modal installed or authenticated.
"""
from __future__ import annotations

import os
import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from . import presets, sizes, testparse
from .config import Config, Target

APP_NAME = "cilicon"

# files we never want to drag into the sandbox mount
_MOUNT_IGNORE = [
    "**/.git", "**/__pycache__", "**/*.pyc", "**/.venv", "**/venv",
    "**/node_modules", "**/.mypy_cache", "**/.pytest_cache", "**/cilicon-artifacts",
    "**/*.egg-info",
]


@dataclass
class StepResult:
    name: str
    ok: bool
    seconds: float
    output: str
    detail: str = ""


@dataclass
class TargetResult:
    target: Target
    build: Optional[StepResult] = None
    validate: Optional[StepResult] = None
    test: Optional[StepResult] = None
    size: Optional[StepResult] = None
    sizes: dict = field(default_factory=dict)   # {text,data,bss,flash,ram,...}
    test_cases: list = field(default_factory=list)  # parsed Unity/TAP {name, ok}
    artifacts: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        if self.error or not (self.build and self.build.ok):
            return False
        if not (self.validate and self.validate.ok):
            return False
        if self.test is not None and not self.test.ok:
            return False
        if self.size is not None and not self.size.ok:
            return False
        return True

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in (self.build, self.size, self.validate, self.test) if s)


# ---- image: the target's own toolchain world -------------------------------

def _image_for(t: Target, project_dir: str):
    import modal

    if t.dockerfile:
        img = modal.Image.from_dockerfile(os.path.join(project_dir, t.dockerfile))
    else:
        img = modal.Image.from_registry(t.base)
        if t.apt:
            img = img.apt_install(*t.apt)
    # mount the WHOLE project at runtime (copy=False) so "any code" builds and
    # editing source doesn't invalidate the cached toolchain layers.
    img = img.add_local_dir(project_dir, "/work", copy=False, ignore=_MOUNT_IGNORE)
    return img


# ---- validation tier -> shell command (data-driven, see presets.py) --------

def _validate_cmd(t: Target) -> str:
    return presets.resolve(t).cmd


# Markers let us run build+validate+test as a single container command (no
# sb.exec, which opens a separate TLS channel) and still split phases / timing.
_MK = "::cilicon::"


def _exports(t: Target) -> str:
    if not t.env:
        return ""
    return "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in t.env.items())


def _phase(name: str, cmd: str, skip_on_fail: bool = True) -> str:
    """Emit a timed, marker-delimited phase. On a nonzero rc we record it and
    (by default) bail so later phases don't run against a broken artifact."""
    bail = (
        f'if [ "$RC" -ne 0 ]; then echo "{_MK}ABORTED after {name}"; '
        f'exit 0; fi\n' if skip_on_fail else ""
    )
    return (
        f'echo "{_MK}{name}_BEGIN"\n'
        f"T0=$(ms)\n"
        f"( {cmd} ) 2>&1\n"
        f"RC=$?\n"
        f"T1=$(ms)\n"
        f'echo "{_MK}{name}_END rc=$RC ms=$((T1-T0))"\n'
        f"{bail}"
    )


def _script(t: Target) -> str:
    parts = [
        "set +e",
        "ms() { echo $(($(date +%s%N)/1000000)); }",
        _exports(t).rstrip("\n"),
        "mkdir -p build",
        _phase("BUILD", t.build),
    ]
    if t.has_size:
        # measure the just-built artifact; never aborts the run (budget is
        # judged in Python so we can report numbers even when it overflows).
        tool = t.size_tool or "size"
        parts.append(_phase("SIZE", f"{tool} {t.artifact}", skip_on_fail=False))
    parts.append(_phase("VALIDATE", _validate_cmd(t), skip_on_fail=t.has_test))
    if t.has_test:
        parts.append(_phase("TEST", t.test, skip_on_fail=False))
    if t.artifacts:
        globs = " ".join(t.artifacts)
        parts.append(
            f"tar czf /tmp/cilicon_artifacts.tgz {globs} 2>/dev/null && "
            f'echo "{_MK}ARTIFACTS ok" || true'
        )
    return "\n".join(p for p in parts if p) + "\n"


# ---- output parsing --------------------------------------------------------

def _slice(out: str, begin: str, end: str) -> str:
    keep, on = [], False
    for ln in out.splitlines():
        if ln.startswith(_MK + begin):
            on = True
            continue
        if ln.startswith(_MK + end):
            break
        if on:
            keep.append(ln)
    return "\n".join(keep)


def _marker(out: str, name: str) -> dict:
    for ln in out.splitlines():
        if ln.startswith(_MK + name):
            kv = {}
            for tok in ln[len(_MK + name):].split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v
            return kv
    return {}


def _phase_result(out: str, name: str, label: str) -> Optional[StepResult]:
    m = _marker(out, f"{name}_END")
    if not m:
        return None
    rc = m.get("rc", "?")
    secs = int(m.get("ms", "0")) / 1000.0
    body = _slice(out, f"{name}_BEGIN", f"{name}_END")
    ok = rc == "0"
    return StepResult(label, ok, secs, body, detail=("ok" if ok else f"exit {rc}"))


# ---- the pass/fail judge ---------------------------------------------------

@dataclass
class _Judgement:
    ok: bool
    detail: str


def _expectations_met(t: Target, out: str) -> tuple[bool, str]:
    """Check expect substrings + regex. Returns (ok, first-unmet-description)."""
    for sub in t.expect:
        if sub not in out:
            return False, f"missing proof '{sub}'"
    if t.expect_regex and not re.search(t.expect_regex, out):
        return False, f"output didn't match /{t.expect_regex}/"
    return True, ""


def _forbidden_hit(t: Target, out: str) -> Optional[str]:
    """A fault string that means 'ran but crashed' — user `expect_not` plus the
    tier's auto crash markers (unless crash_check is off). Returns the first hit."""
    forbidden = list(t.expect_not)
    if t.crash_check:
        forbidden += presets.crash_signatures(t.validate)
    return next((f for f in forbidden if f and f in out), None)


def _judge(t: Target, code: int, out: str, seconds: Optional[float] = None) -> _Judgement:
    """Decide whether 'it actually runs'. The expect string(s) are the on-target
    smoke proof; for full-system boots we don't require a clean emulator exit."""
    full_system = presets.resolve(t).full_system
    met, why = _expectations_met(t, out)
    proof = " + ".join(t.expect) if t.expect else (
        f"/{t.expect_regex}/" if t.expect_regex else "runs"
    )

    # a fault marker fails the target even if `expect` was printed first
    fault = _forbidden_hit(t, out)
    if fault:
        return _Judgement(False, f"fault after start ('{fault}')")

    if t.expect_exit is not None:
        if code != t.expect_exit:
            return _Judgement(False, f"exit {code}, expected {t.expect_exit}")
        if not met:
            return _Judgement(False, why)
        return _Judgement(True, f"exit {code} + proof ('{proof}')")

    if full_system:
        # full-system boot: the expect string IS the proof; the emulator is
        # killed by the boot timeout so a clean exit isn't meaningful.
        if met:
            return _Judgement(True, f"boots, reaches main ('{proof}')")
        # not met: hung (ran out the clock) vs exited/crashed early
        if seconds is not None and t.boot_timeout and seconds >= t.boot_timeout * 0.9:
            return _Judgement(False, f"hung — no '{proof}' in {int(seconds)}s")
        return _Judgement(False, why or f"exited before reaching '{proof}'")

    # qemu_user / native / real_gpu: require clean exit AND the expected proof
    if code != 0:
        sig = {139: "SIGSEGV", 132: "SIGILL", 136: "SIGFPE", 134: "SIGABRT"}.get(
            code, f"exit {code}"
        )
        return _Judgement(False, f"crash ({sig}), caught pre-flash")
    if not met:
        return _Judgement(False, why)
    return _Judgement(True, f"loads + runs ('{proof}')")


def _rc_int(rc: str) -> int:
    return 0 if rc == "0" else (int(rc) if rc.lstrip("-").isdigit() else 1)


# ---- one target ------------------------------------------------------------

def run_target(t: Target, project_dir: str, app, artifacts_dir: str = "") -> TargetResult:
    import modal

    res = TargetResult(target=t)
    sb = None
    try:
        resolved = presets.resolve(t)  # validates custom/run early
        img = _image_for(t, project_dir)
        kwargs = dict(image=img, app=app, timeout=t.timeout, workdir="/work")
        if resolved.gpu:
            kwargs["gpu"] = resolved.gpu
        if t.secrets:
            kwargs["secrets"] = [modal.Secret.from_name(s) for s in t.secrets]

        sb = modal.Sandbox.create("bash", "-lc", _script(t), **kwargs)
        sb.wait()
        out = sb.stdout.read() or ""

        # build
        res.build = _phase_result(out, "BUILD", "build")
        if res.build is None:
            res.error = "no build marker in output (sandbox died early)"
            return res
        if not res.build.ok:
            res.build.detail = f"exit {_marker(out, 'BUILD_END').get('rc','?')}"
            res.validate = StepResult("validate", False, 0, "", "skipped (build failed)")
            return res

        # size budget (does the artifact fit the silicon?) — informational
        # numbers always, a gate only when flash_max/ram_max are set.
        if t.has_size:
            sraw = _phase_result(out, "SIZE", "size")
            if sraw is not None:
                report = sizes.evaluate(sraw.output, t.flash_max, t.ram_max)
                res.sizes = report.to_dict()
                res.size = StepResult("size", report.ok, sraw.seconds, sraw.output, report.detail)

        # validate (the proof-it-runs)
        vraw = _phase_result(out, "VALIDATE", "validate")
        if vraw is None:
            res.validate = StepResult("validate", False, 0, "", "validator never ran")
        else:
            verdict = _judge(t, _rc_int(_marker(out, "VALIDATE_END").get("rc", "?")), vraw.output, vraw.seconds)
            res.validate = StepResult("validate", verdict.ok, vraw.seconds, vraw.output, verdict.detail)

        # optional test phase
        if t.has_test:
            traw = _phase_result(out, "TEST", "test")
            if traw is None:
                res.test = StepResult("test", False, 0, "", "test phase never ran")
            else:
                run = testparse.parse(traw.output, t.test_format)
                if run.parsed:
                    res.test_cases = run.cases
                    ok = traw.ok and run.failed == 0
                    detail = run.summary() if (traw.ok or run.failed) else run.summary() + " (runner exit nonzero)"
                else:
                    ok, detail = traw.ok, ("tests passed" if traw.ok else traw.detail)
                if ok and t.test_expect:
                    for sub in t.test_expect:
                        if sub not in traw.output:
                            ok, detail = False, f"missing '{sub}'"
                            break
                res.test = StepResult("test", ok, traw.seconds, traw.output, detail)

        # pull artifacts back out (best-effort; never fails the target)
        if t.artifacts and artifacts_dir and _marker(out, "ARTIFACTS"):
            res.artifacts = _fetch_artifacts(sb, t, artifacts_dir)

    except Exception as e:  # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"
    finally:
        if sb is not None:
            try:
                sb.terminate()
            except Exception:
                pass
    return res


def _fetch_artifacts(sb, t: Target, artifacts_dir: str) -> list[str]:
    import tarfile
    dest = os.path.join(artifacts_dir, t.slug)
    try:
        os.makedirs(dest, exist_ok=True)
        tgz = os.path.join(dest, "_artifacts.tgz")
        with sb.open("/tmp/cilicon_artifacts.tgz", "rb") as remote, open(tgz, "wb") as local:
            local.write(remote.read())
        with tarfile.open(tgz) as tf:
            names = tf.getnames()
            tf.extractall(dest)
        os.remove(tgz)
        return [os.path.join(dest, n) for n in names if n not in (".", "")]
    except Exception:
        return []


# ---- the matrix ------------------------------------------------------------

def run_matrix(cfg: Config, only: Optional[str], on_update, artifacts_dir: str = "",
               max_workers: Optional[int] = None) -> list[TargetResult]:
    import modal

    targets = cfg.targets
    if only:
        t = cfg.get(only)
        if not t:
            raise SystemExit(f"cilicon: no target matching '{only}'")
        targets = [t]

    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    results: dict[str, TargetResult] = {}

    # cap concurrent sandboxes — the hosted service passes its per-run ceiling
    # so one tenant can't fan a 200-target matrix across the platform at once.
    workers = max(1, min(len(targets), max_workers or len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_target, t, cfg.project_dir, app, artifacts_dir): t for t in targets}
        for t in targets:
            on_update(t.id, "running", None)
        for fut in as_completed(futs):
            t = futs[fut]
            r = fut.result()
            results[t.id] = r
            on_update(t.id, "done", r)

    return [results[t.id] for t in targets]
