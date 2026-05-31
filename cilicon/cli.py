"""cilicon CLI — `cilicon run`, `cilicon targets`, `cilicon presets`, `cilicon boards`."""
from __future__ import annotations

import argparse
import sys
import time

from . import config as cfgmod
from . import presets as presetmod
from . import report as reportmod
from . import telemetry as telemetrymod
from .runner import TargetResult, run_matrix

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; DIM = "\033[2m"; B = "\033[1m"; X = "\033[0m"


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
    targets = cfg.targets if not args.target else [cfg.get(args.target)]
    targets = [t for t in targets if t]
    if not targets:
        print(f"cilicon: no target matching '{args.target}'")
        return 2

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

    _write_reports(args, results, wall)
    return 0 if passed == len(results) else 1


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


def cmd_targets(args) -> int:
    cfg = cfgmod.load(args.config)
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
                print(line); line = "    "
            line += mark + "  "
        if line.strip():
            print(line)
    print()
    print(c("  use:  board: <name>   ·   define your own under `boards:` in cilicon.yml", DIM))
    print(c("  (a board sets any target field; yours extend/override these).", DIM))
    print()
    return 0


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
    pr.set_defaults(func=cmd_run)

    pt = sub.add_parser("targets", help="list configured targets")
    pt.set_defaults(func=cmd_targets)

    pp = sub.add_parser("presets", help="list built-in validation tiers")
    pp.set_defaults(func=cmd_presets)

    pb = sub.add_parser("boards", help="list built-in board presets")
    pb.set_defaults(func=cmd_boards)

    pg = sub.add_parser("gpus", help="list Modal GPU types for the real_gpu tier")
    pg.set_defaults(func=cmd_gpus)

    ps = sub.add_parser("sensors", help="list modeled sensor peripherals (for Renode)")
    ps.set_defaults(func=cmd_sensors)

    args = p.parse_args(argv)
    if not args.cmd:
        # bare `cilicon` == `cilicon run`
        args.target = None
        args.json = args.junit = args.summary = args.artifacts = args.telemetry = None
        args.telemetry_stdout = False
        return cmd_run(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
