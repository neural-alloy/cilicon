"""Tests for the check-quality features: stronger judge, regression baseline,
on-target test parsing, changed-path filtering, and doctor. All Modal-free."""
import json
import os
import tempfile
import types

import pytest

from cilicon import baseline, testparse, runner
from cilicon.config import Target
from cilicon.runner import TargetResult, StepResult


def _t(**kw):
    kw.setdefault("id", "t"); kw.setdefault("build", "x")
    return Target(**kw)


# ---- feature 1+2: stronger judge -------------------------------------------

def test_fault_after_expected_string_fails():
    # firmware prints BOOT OK then HardFaults — must fail despite expect match
    j = runner._judge(_t(validate="qemu_system", expect=["BOOT OK"]), 124, "BOOT OK\nHardFault!\n", 5.0)
    assert not j.ok and "fault" in j.detail.lower()


def test_user_expect_not_fails():
    j = runner._judge(_t(validate="native", expect=["done"], expect_not=["LEAK"]), 0, "done\nLEAK\n")
    assert not j.ok and "LEAK" in j.detail


def test_crash_check_can_be_disabled():
    j = runner._judge(_t(validate="qemu_system", expect=["BOOT OK"], crash_check=False),
                      124, "BOOT OK\nHardFault\n", 5.0)
    assert j.ok   # crash markers ignored when crash_check is off


def test_hung_vs_exited_early():
    t = _t(validate="qemu_system", expect=["BOOT OK"], boot_timeout=20)
    hung = runner._judge(t, 124, "booting...\n", 19.0)
    early = runner._judge(t, 124, "booting...\n", 1.0)
    assert "hung" in hung.detail
    assert "hung" not in early.detail


def test_esp32_guru_meditation_is_a_fault():
    j = runner._judge(_t(validate="qemu_esp32", expect=["Hello"]), 124,
                      "Hello\nGuru Meditation Error: Core 0 panic'ed\n", 5.0)
    assert not j.ok


# ---- feature 3+4+5: regression baseline ------------------------------------

def _res(tid, ok=True, flash=None, ram=None, boot=0.5, log="boot ok"):
    t = _t(id=tid, validate="qemu_system", expect=["ok"])
    r = TargetResult(target=t)
    r.build = StepResult("build", True, 1.0, "", "ok")
    r.validate = StepResult("validate", ok, boot, log, "boots")
    if flash is not None:
        r.sizes = {"flash": flash, "ram": ram or 0}
    return r


def test_baseline_build_and_compare_size_regression():
    base = baseline.build([_res("mcu", flash=10000, ram=2000)])
    assert base["mcu"]["flash"] == 10000
    # +8% flash → severe regression at default 5% threshold
    regs = baseline.compare([_res("mcu", flash=10800, ram=2000)], base, pct=5.0)
    flash_regs = [g for g in regs if g.metric == "flash"]
    assert flash_regs and flash_regs[0].severe
    assert baseline.has_severe(regs)


def test_small_size_growth_is_not_a_regression():
    base = baseline.build([_res("mcu", flash=10000, ram=2000)])
    regs = baseline.compare([_res("mcu", flash=10100, ram=2000)], base, pct=5.0)  # +1%
    assert not any(g.metric == "flash" for g in regs)


def test_boot_log_drift_warns_not_fails():
    base = baseline.build([_res("mcu", flash=10000, log="boot ok\nstage 1\n")])
    regs = baseline.compare([_res("mcu", flash=10000, log="boot ok\nstage 2\n")], base)
    log_regs = [g for g in regs if g.metric == "log"]
    assert log_regs and not log_regs[0].severe
    assert not baseline.has_severe(regs)


def test_normalize_log_strips_addresses_and_timestamps():
    a = baseline.normalize_log("at 0x40001234 (1234) 12:00:01 ready")
    b = baseline.normalize_log("at 0xdeadbeef (5678) 09:30:42 ready")
    assert a == b   # only the varying noise differed


def test_baseline_only_snapshots_passing_targets():
    base = baseline.build([_res("ok1", flash=1), _res("bad", ok=False, flash=2)])
    assert "ok1" in base and "bad" not in base


# ---- feature 6: test parsing -----------------------------------------------

def test_parse_unity():
    out = "f.c:1:test_a:PASS\nf.c:2:test_b:FAIL: x\nf.c:3:test_c:IGNORE\n2 Tests 1 Failures 1 Ignored"
    r = testparse.parse(out)
    assert r.total == 2 and r.failed == 1 and r.failures() == ["test_b"]


def test_parse_tap_skips_directives():
    out = "1..3\nok 1 - a\nnot ok 2 - b\nok 3 - c # SKIP"
    r = testparse.parse(out)
    assert r.total == 2 and r.failures() == ["b"]


def test_parse_autodetect_and_empty():
    assert testparse.detect("ok 1 - x") == "tap"
    assert testparse.detect("f.c:1:t:PASS") == "unity"
    assert not testparse.parse("just some logs").parsed


# ---- feature 7: changed-path filtering -------------------------------------

def _args(**kw):
    base = dict(changed_files=None, changed_since=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_changed_path_filter_runs_only_matching():
    from cilicon import cli
    targets = [
        _t(id="esp", paths=["src/esp32/**"]),
        _t(id="arm", paths=["src/arm/**"]),
        _t(id="always"),   # no paths → always runs
    ]
    kept = cli._filter_changed(targets, _args(changed_files="src/esp32/main.c"))
    ids = {t.id for t in kept}
    assert ids == {"esp", "always"}   # arm skipped, always always runs


def test_no_filter_runs_everything():
    from cilicon import cli
    targets = [_t(id="a", paths=["x/**"]), _t(id="b")]
    assert len(cli._filter_changed(targets, _args())) == 2


# ---- feature 8: doctor -----------------------------------------------------

def _load(yml):
    from cilicon import config
    d = tempfile.mkdtemp(); p = os.path.join(d, "cilicon.yml"); open(p, "w").write(yml)
    return p


def test_doctor_passes_good_config():
    from cilicon import cli
    p = _load("targets:\n  - id: t\n    board: cortex-m\n    build: make\n    artifact: a.elf\n    expect: OK\n")
    assert cli.cmd_doctor(types.SimpleNamespace(config=p)) == 0


def test_doctor_flags_sim_without_bin():
    from cilicon import cli
    # sim tier without sim_bin loads fine but fails to resolve → doctor catches it
    p = _load("targets:\n  - id: t\n    validate: sim\n    build: make\n    artifact: a.elf\n    expect: OK\n")
    assert cli.cmd_doctor(types.SimpleNamespace(config=p)) == 1
