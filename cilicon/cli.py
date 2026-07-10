"""cilicon CLI — `cilicon run`, `cilicon targets`, `cilicon presets`, `cilicon boards`."""
from __future__ import annotations

import argparse
import sys
import time

from . import config as cfgmod
from . import presets as presetmod
from . import report as reportmod
from . import telemetry as telemetrymod
from . import baseline as baselinemod
from . import attest as attestmod
from .runner import TargetResult, run_matrix

G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
DIM = "\033[2m"
B = "\033[1m"
X = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def c(s: str, color: str) -> str:
    return f"{color}{s}{X}" if _supports_color() else s


def _fmt_secs(s: float) -> str:
    if s >= 60:
        return f"{int(s)//60}m{int(s)%60:02d}s"
    return f"{s:.0f}s"


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024*1024):.1f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n}B"


def _cell(text: str, width: int, color: str = "") -> str:
    """Pad/truncate to a fixed VISIBLE width, then colorize (so ANSI codes
    don't break column alignment)."""
    if len(text) > width:
        text = text[: width - 1] + "…"
    padded = text + " " * (width - len(text))
    return c(padded, color) if color else padded


def _row_cells(id_cell: str, build_cell: str, check_cell: str) -> str:
    return f"  │ {id_cell} │ {build_cell} │ {check_cell} │"


def cmd_run(args) -> int:
    cfg = cfgmod.load(args.config)
    # Validate the signing key BEFORE spinning up (paid) cloud containers — a
    # missing key must fail fast, not after a full matrix has run.
    signing_key = _attestation_preflight(args)
    targets = cfg.targets if not args.target else [cfg.get(args.target)]
    targets = [t for t in targets if t]
    if not targets:
        print(f"cilicon: no target matching '{args.target}'")
        return 2

    targets = _filter_changed(targets, args)
    if not targets:
        print("\n  cilicon · no targets affected by the changed files — nothing to run\n")
        return 0

    w_id = max(28, max(len(t.id) for t in targets) + 1)
    print()
    print(c(f"  cilicon · {len(targets)} target(s) · fanned across Modal cloud containers", B))
    print()
    sep_top = "  ┌" + "─" * (w_id + 2) + "┬" + "─" * 16 + "┬" + "─" * 36 + "┐"
    sep_mid = "  ├" + "─" * (w_id + 2) + "┼" + "─" * 16 + "┼" + "─" * 36 + "┤"
    sep_bot = "  └" + "─" * (w_id + 2) + "┴" + "─" * 16 + "┴" + "─" * 36 + "┘"
    print(sep_top)
    print(_row_cells(_cell("TARGET", w_id), _cell("BUILD", 14), _cell("ON-TARGET CHECK", 34)))
    print(sep_mid)

    rec = telemetrymod.Recorder(telemetrymod.make_sink(
        path=getattr(args, "telemetry", "") or "",
        to_stdout=getattr(args, "telemetry_stdout", False),
    ))
    rec.run_started([t.id for t in targets])
    by_id = {t.id: t for t in targets}
    printed_running = set()

    def on_update(tid, phase, result: TargetResult | None):
        if phase == "running" and tid not in printed_running:
            printed_running.add(tid)
            print(c(f"  → {tid}: building + validating in its own container…", DIM))
            tg = by_id.get(tid)
            rec.target_started(tid, tier=tg.validate if tg else "", gpu=(tg.gpu or None) if tg else None)
        elif phase == "done":
            _print_result_line(tid, result, w_id)
            if result is not None:
                rec.target_completed(result)

    t0 = time.time()
    results = run_matrix(cfg, args.target, on_update, artifacts_dir=args.artifacts or "")
    wall = time.time() - t0
    rec.run_completed(results, wall)
    rec.close()

    print(sep_bot)
    passed = sum(1 for r in results if r.ok)
    seq = sum(r.seconds for r in results)
    summary = f"  {passed} / {len(results)} passed · wall-clock {_fmt_secs(wall)}"
    if seq > wall:
        summary += f" · vs ~{_fmt_secs(seq)} sequential"
    print(c(summary, B if passed == len(results) else Y))

    for r in results:
        if not r.ok:
            print()
            print(c(f"  ✗ {r.target.id}", R))
            if r.error:
                print(f"      orchestration error: {r.error}")
            for step in (r.build, r.size, r.validate, r.test):
                if step and not step.ok:
                    print(f"      {step.name}: {step.detail}")
                    tail = "\n".join(step.output.strip().splitlines()[-6:])
                    for ln in tail.splitlines():
                        print(c(f"        {ln}", DIM))
    if any(r.artifacts for r in results):
        print()
        for r in results:
            for a in r.artifacts:
                print(c(f"  ⬇ {r.target.id}: {a}", DIM))
    print()

    regressed = _handle_baseline(args, results)
    _write_reports(args, results, wall)
    _write_attestation(args, results, signing_key)
    failed = passed != len(results) or (regressed and getattr(args, "fail_on_regression", False))
    return 1 if failed else 0


def _attestation_preflight(args):
    """Resolve the signing key up front when --attestation was requested, so a
    missing/bad key aborts before any cloud spend. Returns the key or None."""
    if not getattr(args, "attestation", None):
        return None
    if not getattr(args, "artifacts", None):
        raise SystemExit(
            "cilicon: --attestation needs --artifacts <dir> (the built binary must "
            "be pulled back so its real bytes can be hashed and signed)"
        )
    import os
    try:
        return attestmod.load_signing_key(
            getattr(args, "signing_key", None), os.environ.get("CILICON_SIGNING_KEY")
        )
    except (ValueError, OSError) as e:
        raise SystemExit(f"cilicon: {e}")


def _write_attestation(args, results, signing_key) -> None:
    """Sign a DSSE boot-test attestation over the run and write the envelope. Only
    reached once the key is known-good; a build with no pulled-back artifact bytes
    is an honest error, never a silent empty claim."""
    if not getattr(args, "attestation", None):
        return
    runner = f"cilicon/{_version()} qemu@modal"
    try:
        envelope = attestmod.make_attestation(results, signing_key, runner)
    except ValueError as e:
        raise SystemExit(f"cilicon: cannot attest — {e}")
    import json
    with open(args.attestation, "w") as f:
        f.write(json.dumps(envelope, indent=2))
    subj = len(json_loads_payload(envelope)["subject"])
    print(c(f"  wrote {args.attestation}  ({subj} artifact digest(s), SELF_ATTESTED)", DIM))


def json_loads_payload(envelope) -> dict:
    """Decode a DSSE envelope's in-toto statement (for display only — verification
    uses the raw payload bytes, never this)."""
    import base64
    import json
    return json.loads(base64.b64decode(envelope["payload"]))


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("cilicon")
    except Exception:
        return "0"


def _changed_files(args) -> list[str] | None:
    """Files changed, from --changed-files or `git diff --name-only <ref>`.
    Returns None when no filter was requested (→ run everything)."""
    if getattr(args, "changed_files", None):
        return [f.strip() for f in args.changed_files.split(",") if f.strip()]
    if getattr(args, "changed_since", None):
        import subprocess
        try:
            out = subprocess.run(
                ["git", "diff", "--name-only", args.changed_since],
                capture_output=True, text=True, check=True).stdout
            return [ln.strip() for ln in out.splitlines() if ln.strip()]
        except Exception as e:  # noqa: BLE001
            print(c(f"  could not compute changed files ({e}); running all targets", DIM))
            return None
    return None


def _filter_changed(targets, args):
    changed = _changed_files(args)
    if changed is None:
        return targets
    import fnmatch
    kept = []
    for t in targets:
        # a target with no `paths` always runs; otherwise it must match a change
        if not t.paths or any(fnmatch.fnmatch(f, pat) for pat in t.paths for f in changed):
            kept.append(t)
    skipped = len(targets) - len(kept)
    if skipped:
        print(c(f"  ↓ {skipped} target(s) skipped — no matching changed files", DIM))
    return kept


def _handle_baseline(args, results) -> bool:
    """Update or compare against a baseline. Returns True if a SEVERE regression
    (a size growth past the threshold) was found."""
    if getattr(args, "update_baseline", None):
        baselinemod.save(baselinemod.build(results), args.update_baseline)
        print(c(f"  baseline written to {args.update_baseline}", DIM))
    if not getattr(args, "baseline", None):
        return False
    try:
        base = baselinemod.load(args.baseline)
    except FileNotFoundError:
        print(c(f"  baseline {args.baseline} not found — skipping regression check", DIM))
        return False
    regs = baselinemod.compare(results, base, pct=getattr(args, "regression_pct", 5.0))
    if not regs:
        print(c("  ✓ no regressions vs baseline", DIM))
        return False
    print()
    for g in regs:
        mark = c("✗", R) if g.severe else c("⚠", Y)
        print(f"  {mark} {g.target}: {g.detail}")
    print()
    return baselinemod.has_severe(regs)


def _write_reports(args, results, wall: float) -> None:
    if getattr(args, "json", None):
        with open(args.json, "w") as f:
            f.write(reportmod.to_json(results, wall))
        print(c(f"  wrote {args.json}", DIM))
    if getattr(args, "junit", None):
        with open(args.junit, "w") as f:
            f.write(reportmod.to_junit(results, wall))
        print(c(f"  wrote {args.junit}", DIM))
    if getattr(args, "summary", None):
        with open(args.summary, "w") as f:
            f.write(reportmod.to_markdown(results, wall))
        print(c(f"  wrote {args.summary}", DIM))


def _print_result_line(tid, r: TargetResult, w_id):
    if r is None:
        return
    if r.ok:
        build = _cell(f"✓ {_fmt_secs(r.build.seconds):>5}", 14, G)
        note = " · tests ✓" if r.test else ""
        if r.sizes.get("flash"):
            note += f" · {_fmt_bytes(r.sizes['flash'])} flash"
        check = _cell(f"✓ {r.validate.detail}{note}", 34, G)
    elif r.build and not r.build.ok:
        build = _cell(f"✗ {r.build.detail}", 14, R)
        check = _cell("— build failed", 34, DIM)
    elif r.error:
        build = _cell("✗ infra", 14, R)
        check = _cell(r.error, 34, R)
    elif r.size and not r.size.ok:
        build = _cell(f"✓ {_fmt_secs(r.build.seconds):>5}", 14, G)
        check = _cell(f"✗ {r.size.detail}", 34, R)
    elif r.validate and not r.validate.ok:
        build = _cell(f"✓ {_fmt_secs(r.build.seconds):>5}", 14, G)
        check = _cell(f"✗ {r.validate.detail}", 34, R)
    else:  # test phase failed
        build = _cell(f"✓ {_fmt_secs(r.build.seconds):>5}", 14, G)
        check = _cell(f"✗ test: {r.test.detail}", 34, R)
    print(_row_cells(_cell(tid, w_id), build, check))


def _targets_report(cfg) -> dict:
    """The data behind `cilicon targets` — pure, so agents can consume it via
    `--json` (and it's unit-testable without a render)."""
    out = []
    for t in cfg.targets:
        proof = " + ".join(t.expect) if t.expect else (t.expect_regex or None)
        out.append({
            "id": t.id,
            "build": t.build,
            "validate": t.validate,
            "machine": t.machine if t.validate.startswith("qemu_system") else None,
            "proves": proof,
            "test": t.test or None,
            "flash_max": t.flash_max,
            "ram_max": t.ram_max,
            "gpu": t.gpu or None,
        })
    return {"tool": "cilicon", "count": len(cfg.targets), "targets": out}


def cmd_targets(args) -> int:
    cfg = cfgmod.load(args.config)
    if getattr(args, "json", False):
        import json
        print(json.dumps(_targets_report(cfg), indent=2))
        return 0
    print(f"\n  cilicon · {len(cfg.targets)} targets in {args.config}\n")
    for t in cfg.targets:
        print(f"  • {c(t.id, B)}")
        print(f"      build:    {t.build}")
        extra = f" ({t.machine})" if t.validate.startswith("qemu_system") else ""
        print(f"      validate: {t.validate}{extra}")
        proof = " + ".join(t.expect) if t.expect else (t.expect_regex or "(exit code)")
        print(f"      proves:   '{proof}'")
        if t.has_test:
            print(f"      test:     {t.test}")
        if t.has_size:
            budget = []
            if t.flash_max is not None:
                budget.append(f"flash≤{_fmt_bytes(t.flash_max)}")
            if t.ram_max is not None:
                budget.append(f"ram≤{_fmt_bytes(t.ram_max)}")
            print(f"      fits:     {', '.join(budget) or '(report only)'}")
    print()
    return 0


def cmd_presets(args) -> int:
    print(f"\n  cilicon · {len(presetmod.PRESETS)} validation tiers\n")
    for name, p in presetmod.PRESETS.items():
        flags = []
        if p.full_system:
            flags.append("full-system boot")
        if p.gpu:
            flags.append(f"gpu:{p.gpu}")
        tag = c("  [" + ", ".join(flags) + "]", DIM) if flags else ""
        print(f"  • {c(name, B)}{tag}")
        print(f"      {p.blurb}")
    print()
    return 0


def cmd_boards(args) -> int:
    # built-in catalog + any boards the user defined in their config
    user = {}
    try:
        import yaml
        with open(args.config) as f:
            user = (yaml.safe_load(f) or {}).get("boards") or {}
    except Exception:
        pass
    catalog = {**presetmod.BOARDS, **user}

    # group by validation tier so 100+ entries stay scannable
    by_tier: dict[str, list[str]] = {}
    for name, b in catalog.items():
        by_tier.setdefault(b.get("validate", "?"), []).append(name)

    total = len(catalog)
    print(f"\n  cilicon · {total} boards"
          + (f" ({len(user)} of your own)" if user else "")
          + "  ·  one-word aliases for a toolchain + tier\n")
    for tier in sorted(by_tier):
        names = sorted(by_tier[tier])
        print(f"  {c(tier, B)} {c('(' + str(len(names)) + ')', DIM)}")
        # wrap names a few per line
        line = "    "
        for n in names:
            mark = c(n, DIM) if n in user else n
            if len(line) + len(n) > 76:
                print(line)
                line = "    "
            line += mark + "  "
        if line.strip():
            print(line)
    print()
    print(c("  use:  board: <name>   ·   define your own under `boards:` in cilicon.yml", DIM))
    print(c("  (a board sets any target field; yours extend/override these).", DIM))
    print()
    return 0


def _doctor_report(cfg) -> dict:
    """The data behind `cilicon doctor` — pure, so agents can consume it via
    `--json`. Resolves every tier and flags weak checks without running anything.
    Returns {ok, errors, targets:[{id,validate,ok,errors,warnings}]}."""
    targets = []
    errors = 0
    for t in cfg.targets:
        problems, warns = [], []
        try:
            presetmod.resolve(t)     # tier guards: custom/sim/renode/unknown
        except Exception as e:       # noqa: BLE001
            problems.append(str(e).split(": ", 1)[-1])
        if not (t.expect or t.expect_regex or t.expect_exit is not None):
            warns.append("no expect/expect_regex/expect_exit — a green check only means 'didn't crash'")
        if t.gpu and not presetmod.gpu_known(t.gpu):
            warns.append(f"gpu '{t.gpu}' isn't in the known list (still passed to Modal)")
        if (t.flash_max is not None or t.ram_max is not None) and not t.size_tool and t.validate != "native":
            warns.append("flash_max/ram_max set but no size_tool — size check may not run")
        if t.test_format and not t.test:
            warns.append("test_format set but no test command")
        if problems:
            errors += 1
        targets.append({"id": t.id, "validate": t.validate, "ok": not problems,
                        "errors": problems, "warnings": warns})
    return {"tool": "cilicon", "ok": errors == 0, "errors": errors, "targets": targets}


def cmd_doctor(args) -> int:
    """Validate cilicon.yml without running anything: parse, resolve every tier,
    and flag weak checks — config errors in 50ms instead of a cloud round-trip."""
    cfg = cfgmod.load(args.config)   # raises SystemExit on parse errors
    report = _doctor_report(cfg)
    if getattr(args, "json", False):
        import json
        print(json.dumps(report, indent=2))
        return 1 if report["errors"] else 0

    print(f"\n  cilicon doctor · {len(cfg.targets)} target(s) in {args.config}\n")
    for t in report["targets"]:
        if t["ok"]:
            print(c(f"  ✓ {t['id']}", G) + c(f"  {t['validate']}", DIM))
        else:
            print(c(f"  ✗ {t['id']}", R))
            for p in t["errors"]:
                print(c(f"      error: {p}", R))
        for w in t["warnings"]:
            print(c(f"      ⚠ {w}", Y))
    print()
    if report["errors"]:
        print(c(f"  {report['errors']} target(s) with errors", R))
    else:
        print(c("  all targets resolve — ready to run", B))
    print()
    return 1 if report["errors"] else 0


def cmd_sensors(args) -> int:
    s = presetmod.SENSORS
    print(f"\n  cilicon · {len(s)} modeled sensors (peripherals, not boot targets)\n")
    for name, desc in s.items():
        print(f"  • {c(name, B)}  {c('— ' + desc, DIM)}")
    print()
    print(c("  attach these to a board inside a Renode .resc (validate: renode);", DIM))
    print(c("  they're I2C/SPI peripherals, so they don't boot on their own.", DIM))
    print()
    return 0


def cmd_gpus(args) -> int:
    print(f"\n  cilicon · {len(presetmod.GPUS)} Modal GPU types for the real_gpu tier\n")
    for g in presetmod.GPUS:
        print(f"  • {c(g, B)}")
    print()
    print(c("  use any in a target's `gpu:` field; add a count like \"H100:2\".", DIM))
    print(c("  unknown names still pass through to Modal (new GPUs work day one).", DIM))
    print()
    return 0


def cmd_verify_attestation(args) -> int:
    """Independently check a DSSE boot-test envelope against a public key — so a
    human (and our e2e) can confirm the signature without trusting the writer."""
    import json
    try:
        with open(args.envelope) as f:
            envelope = json.load(f)
        pub = attestmod.load_public_key(args.key)
    except Exception as e:  # noqa: BLE001
        print(c(f"  ✗ {type(e).__name__}: {e}", R))
        return 1
    ok, why = attestmod.verify_envelope(envelope, pub)
    if not ok:
        print(c(f"  ✗ attestation INVALID — {why}", R))
        return 1
    stmt = json_loads_payload(envelope)
    pred = stmt.get("predicate", {})
    print(c(f"  ✓ signature verifies (keyid {envelope['signatures'][0]['keyid'][:16]}…)", G))
    print(c(f"    assurance: {pred.get('assurance')}  ·  runner: {pred.get('runner')}", DIM))
    for s in stmt.get("subject", []):
        print(c(f"    subject: {s['name']}  sha256:{s['digest']['sha256']}", DIM))
    for rb in pred.get("results", []):
        mark = c("✓", G) if rb.get("passed") else c("✗", R)
        print(f"    {mark} {rb['target']}  [{rb['fidelity']}]  {rb['terminating_event']}  {rb['boot_ms']}ms")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cilicon", description="CI for real hardware — without touching metal.")
    p.add_argument("-c", "--config", default="cilicon.yml", help="path to cilicon.yml")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="build + validate the matrix in parallel")
    pr.add_argument("--target", "-t", help="run a single target")
    pr.add_argument("--json", help="write a JSON report to this path")
    pr.add_argument("--junit", help="write a JUnit XML report to this path")
    pr.add_argument("--summary", help="write a Markdown summary (e.g. $GITHUB_STEP_SUMMARY)")
    pr.add_argument("--artifacts", help="directory to pull built artifacts into")
    pr.add_argument("--telemetry", help="append JSONL run/target/phase events to this path")
    pr.add_argument("--telemetry-stdout", action="store_true", help="also print telemetry events to stdout")
    pr.add_argument("--baseline", help="compare flash/RAM/boot-time/log against this baseline JSON")
    pr.add_argument("--update-baseline", help="write this run as the new baseline JSON")
    pr.add_argument("--fail-on-regression", action="store_true", help="fail if a size regression exceeds the threshold")
    pr.add_argument("--regression-pct", type=float, default=5.0, help="size growth %% that counts as a regression (default 5)")
    pr.add_argument("--changed-files", help="comma-separated changed files; run only targets whose paths match")
    pr.add_argument("--changed-since", help="git ref; run only targets whose paths match files changed since it")
    pr.add_argument("--attestation", help="write a signed DSSE boot-test attestation (in-toto) to this path")
    pr.add_argument("--signing-key", help="Ed25519 key file (PKCS8 PEM/DER or 32-byte raw seed); or set CILICON_SIGNING_KEY")
    pr.set_defaults(func=cmd_run)

    pt = sub.add_parser("targets", help="list configured targets")
    pt.add_argument("--json", action="store_true", help="emit the target list as JSON")
    pt.set_defaults(func=cmd_targets)

    pp = sub.add_parser("presets", help="list built-in validation tiers")
    pp.set_defaults(func=cmd_presets)

    pb = sub.add_parser("boards", help="list built-in board presets")
    pb.set_defaults(func=cmd_boards)

    pg = sub.add_parser("gpus", help="list Modal GPU types for the real_gpu tier")
    pg.set_defaults(func=cmd_gpus)

    ps = sub.add_parser("sensors", help="list modeled sensor peripherals (for Renode)")
    ps.set_defaults(func=cmd_sensors)

    pd = sub.add_parser("doctor", help="validate cilicon.yml without running anything")
    pd.add_argument("--json", action="store_true", help="emit the doctor report as JSON")
    pd.set_defaults(func=cmd_doctor)

    pva = sub.add_parser("verify-attestation", help="verify a signed DSSE boot-test attestation")
    pva.add_argument("envelope", help="path to the DSSE envelope JSON written by `run --attestation`")
    pva.add_argument("--key", required=True, help="Ed25519 public key (raw/PEM/DER) to verify against")
    pva.set_defaults(func=cmd_verify_attestation)

    args = p.parse_args(argv)
    if not args.cmd:
        # bare `cilicon` == `cilicon run`
        args.target = None
        args.json = args.junit = args.summary = args.artifacts = args.telemetry = None
        args.telemetry_stdout = False
        args.baseline = args.update_baseline = args.changed_files = args.changed_since = None
        args.fail_on_regression = False
        args.regression_pct = 5.0
        args.attestation = args.signing_key = None
        return cmd_run(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
