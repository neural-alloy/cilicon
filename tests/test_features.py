"""Tests for the cilicon feature additions: Renode/sim tiers, the GPU catalog,
and telemetry. All Modal-free."""
import json
import os
import tempfile

import pytest

from cilicon import presets, telemetry
from cilicon.config import Target
from cilicon.runner import TargetResult, StepResult


def _t(**kw):
    kw.setdefault("id", "t")
    kw.setdefault("build", "true")
    return Target(**kw)


# ---- Renode tier -----------------------------------------------------------

def test_renode_resolves_and_captures_uart():
    t = _t(validate="renode", renode_script="examples/renode/x.resc", artifact="build/fw.elf")
    r = presets.resolve(t)
    assert r.full_system is True
    assert "renode" in r.cmd and "include @examples/renode/x.resc" in r.cmd
    # the default uart-log path is teed back to stdout for expect matching
    assert "/tmp/cilicon_uart.log" in r.cmd


def test_renode_requires_a_script():
    with pytest.raises(ValueError):
        presets.resolve(_t(validate="renode", artifact="build/fw.elf"))


def test_renode_uart_log_is_overridable():
    t = _t(validate="renode", renode_script="x.resc", renode_uart_log="/tmp/u2.log", artifact="a")
    assert "/tmp/u2.log" in presets.resolve(t).cmd


# ---- sim / FVP tier --------------------------------------------------------

def test_sim_resolves_with_bin_and_args():
    t = _t(validate="sim", sim_bin="FVP_MPS2_Cortex-M4", sim_args="--stat", artifact="build/app.axf")
    r = presets.resolve(t)
    assert r.full_system is True
    assert r.cmd == "timeout 60 FVP_MPS2_Cortex-M4 --stat ./build/app.axf 2>&1 || true"


def test_sim_requires_a_binary():
    with pytest.raises(ValueError):
        presets.resolve(_t(validate="sim", artifact="a"))


# ---- GPU catalog -----------------------------------------------------------

def test_gpu_split_and_known():
    assert presets.split_gpu("A100-80GB:2") == ("A100-80GB", 2)
    assert presets.split_gpu("T4") == ("T4", 1)
    assert presets.gpu_known("H100") and presets.gpu_known("A100-80GB:4")
    assert not presets.gpu_known("MADE-UP")


def test_cuda_board_selects_real_gpu():
    b = presets.BOARDS["cuda"]
    assert b["validate"] == "real_gpu" and b["gpu"] == "T4"


def test_real_gpu_field_overrides_default_and_supports_count():
    t = _t(validate="real_gpu", gpu="H100:2", artifact="build/infer")
    assert presets.resolve(t).gpu == "H100:2"


# ---- user-defined boards ---------------------------------------------------

def _load_yml(yml: str):
    import tempfile, os
    from cilicon import config
    d = tempfile.mkdtemp(); p = os.path.join(d, "cilicon.yml")
    open(p, "w").write(yml)
    return config.load(p)


def test_user_can_define_their_own_board():
    cfg = _load_yml("""
boards:
  my-mcu:
    base: my-registry/toolchain:latest
    apt: [gcc-arm-none-eabi, qemu-system-arm]
    validate: qemu_system
    machine: mps2-an385
targets:
  - id: widget
    board: my-mcu
    build: "make"
    artifact: build/fw.elf
    expect: "READY"
""")
    t = cfg.targets[0]
    assert t.base == "my-registry/toolchain:latest"
    assert t.validate == "qemu_system" and t.machine == "mps2-an385"


def test_user_board_can_override_a_builtin():
    cfg = _load_yml("""
boards:
  cortex-m:                       # same name as a built-in starter
    base: my/custom-arm-image
    apt: [gcc-arm-none-eabi, qemu-system-arm]
    validate: qemu_system
    machine: lm3s6965evb
targets:
  - id: w
    board: cortex-m
    build: "make"
    artifact: a.elf
""")
    assert cfg.targets[0].base == "my/custom-arm-image"   # user def wins


def test_catalog_has_100_plus_boards_and_sensors():
    from cilicon import presets
    assert len(presets.BOARDS) >= 100
    assert len(presets.SENSORS) >= 20


def test_every_catalog_board_loads_and_resolves():
    """Every built-in board must produce a valid Target + resolvable command
    through the real config pipeline (catches a typo'd field or tier)."""
    from cilicon import presets, config
    import tempfile, os
    for name, b in presets.BOARDS.items():
        yml = (f"targets:\n  - id: t\n    board: {name}\n"
               f"    build: \"true\"\n    artifact: a.bin\n    expect: \"X\"\n")
        d = tempfile.mkdtemp(); p = os.path.join(d, "cilicon.yml")
        open(p, "w").write(yml)
        cfg = config.load(p)              # must not SystemExit
        presets.resolve(cfg.targets[0])   # must not raise


def test_catalog_boards_only_use_real_target_fields():
    from cilicon import presets
    from cilicon.config import Target
    known = set(Target.__dataclass_fields__)
    for name, b in presets.BOARDS.items():
        bad = [k for k in b if k not in known]
        assert not bad, f"board {name} has non-Target fields: {bad}"


def test_unknown_board_lists_define_your_own_hint():
    import pytest
    with pytest.raises(SystemExit) as e:
        _load_yml("""
targets:
  - id: w
    board: nonexistent
    build: "make"
""")
    assert "boards:" in str(e.value)


# ---- telemetry: pure event builders ----------------------------------------

def _result(ok=True, tier="qemu_user", with_size=False):
    t = _t(validate=tier, expect=["ok"])
    r = TargetResult(target=t)
    r.build = StepResult("build", True, 1.0, "log", "ok")
    r.validate = StepResult("validate", ok, 0.5, "ok", "loads + runs" if ok else "crash")
    if with_size:
        r.size = StepResult("size", True, 0.0, "", "fits")
        r.sizes = {"flash": 12288, "ram": 2048}
    return r


def test_target_event_shape():
    ev = telemetry.target_event("run_x", _result(with_size=True), ts=123.456)
    assert ev["event"] == "target.completed"
    assert ev["run_id"] == "run_x" and ev["tier"] == "qemu_user"
    assert ev["phases"]["build"]["ok"] is True
    assert ev["phases"]["size"]["ok"] is True and ev["phases"]["test"] is None
    assert ev["sizes"]["flash"] == 12288


def test_run_summary_aggregates_by_tier_and_phase():
    results = [_result(True, "qemu_user"), _result(False, "qemu_user"), _result(True, "real_gpu")]
    s = telemetry.run_summary(results, 9.0)
    assert s["passed"] == 2 and s["failed"] == 1 and s["total"] == 3
    assert s["by_tier"] == {"qemu_user": 2, "real_gpu": 1}
    assert s["phase_seconds"]["build"] == 3.0  # 1.0 * 3


# ---- telemetry: sinks + recorder -------------------------------------------

def test_jsonl_sink_writes_one_object_per_line():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "sub", "events.jsonl")  # dir auto-created
    sink = telemetry.JsonlSink(path)
    sink.emit({"event": "a"}); sink.emit({"event": "b"}); sink.close()
    lines = open(path).read().strip().splitlines()
    assert [json.loads(l)["event"] for l in lines] == ["a", "b"]


def test_recorder_full_lifecycle_to_jsonl():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "run.jsonl")
    rec = telemetry.Recorder(telemetry.JsonlSink(path), run_id="run_fixed")
    rec.run_started(["a/x", "b/y"])
    rec.target_started("a/x", tier="qemu_user")
    rec.target_completed(_result(True))
    rec.run_completed([_result(True), _result(False)], 5.0)
    rec.close()
    events = [json.loads(l) for l in open(path)]
    kinds = [e["event"] for e in events]
    assert kinds == ["run.started", "target.started", "target.completed", "run.completed"]
    assert all(e["run_id"] == "run_fixed" for e in events)
    assert events[-1]["passed"] == 1 and events[-1]["failed"] == 1


def test_make_sink_defaults_to_null(monkeypatch):
    monkeypatch.delenv("CILICON_TELEMETRY", raising=False)
    monkeypatch.delenv("CILICON_TELEMETRY_URL", raising=False)
    monkeypatch.delenv("CILICON_TELEMETRY_STDOUT", raising=False)
    assert isinstance(telemetry.make_sink(), telemetry.NullSink)


def test_make_sink_from_env_path(monkeypatch):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.jsonl")
    monkeypatch.setenv("CILICON_TELEMETRY", p)
    sink = telemetry.make_sink()
    assert isinstance(sink, telemetry.JsonlSink) and sink.path == p


def test_telemetry_never_raises_on_bad_path():
    # a directory that can't be created shouldn't blow up the run
    sink = telemetry.JsonlSink("/proc/cannot/write/here.jsonl")
    sink.emit({"event": "x"})  # must not raise
    sink.close()


# ---- sweep grid in the PR summary ------------------------------------------

def _matrix_result(arch, opt, ok):
    from cilicon import report  # noqa
    t = Target(id=f"node-{arch}-{opt}", build="x", validate="native")
    t.matrix_values = {"arch": arch, "opt": opt}
    r = TargetResult(target=t)
    r.build = StepResult("build", True, 0.1, "", "ok")
    r.validate = StepResult("validate", ok, 0.1, "", "runs" if ok else "crash")
    return r


def test_matrix_values_preserved_through_expansion():
    import tempfile, os
    yml = (
        "targets:\n"
        "  - id: node-{arch}\n"
        "    matrix: {arch: [arm, riscv64]}\n"
        "    build: \"echo {arch}\"\n"
        "    validate: native\n"
    )
    d = tempfile.mkdtemp(); p = os.path.join(d, "cilicon.yml"); open(p, "w").write(yml)
    cfg = __import__("cilicon.config", fromlist=["load"]).load(p)
    assert [t.matrix_values for t in cfg.targets] == [{"arch": "arm"}, {"arch": "riscv64"}]


def test_two_axis_grid_marks_the_failing_cell():
    from cilicon import report
    results = [
        _matrix_result("arm", "O0", True), _matrix_result("arm", "O2", True),
        _matrix_result("riscv64", "O0", True), _matrix_result("riscv64", "O2", False),
    ]
    md = report.to_markdown(results, 1.0)
    assert "Fan-out sweep" in md and "sweep: `arch` × `opt`" in md
    # the riscv64 row has one pass and one fail
    row = [l for l in md.splitlines() if l.startswith("| riscv64 ")][0]
    assert row.count("✅") == 1 and row.count("❌") == 1


def test_one_axis_grid_is_a_single_row():
    from cilicon import report
    results = [_matrix_result("arm", "O0", True), _matrix_result("riscv64", "O0", False)]
    # collapse to a single axis by giving them only 'arch'
    for r in results:
        r.target.matrix_values = {"arch": r.target.matrix_values["arch"]}
    md = report.to_markdown(results, 1.0)
    assert "sweep: `arch`" in md and "| ✅ | ❌ |" in md


def test_no_grid_when_no_matrix():
    from cilicon import report
    t = Target(id="plain", build="x", validate="native")
    r = TargetResult(target=t); r.build = StepResult("build", True, 0.1, "", "ok")
    r.validate = StepResult("validate", True, 0.1, "", "runs")
    assert "Fan-out sweep" not in report.to_markdown([r], 1.0)
