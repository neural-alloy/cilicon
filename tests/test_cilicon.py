"""Unit tests for cilicon's Modal-free logic: config parsing + matrix + board +
preset resolution, the pass/fail judge, phase-script parsing, and reports.

These never touch Modal — they exercise the parts that decide *what* runs and
*whether it passed*, which is where the correctness lives.
"""
import os
import tempfile
import textwrap

import pytest

from cilicon import config as cfgmod
from cilicon import presets
from cilicon import report
from cilicon import runner
from cilicon.config import Target


def _load(yml: str) -> cfgmod.Config:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "cilicon.yml")
    with open(path, "w") as f:
        f.write(textwrap.dedent(yml))
    return cfgmod.load(path)


# ---- preset resolution -----------------------------------------------------

def test_preset_defaults_fill_qemu_bin():
    cfg = _load("""
        targets:
          - id: rv/linux
            build: "true"
            validate: qemu_user_riscv64
            artifact: build/app
    """)
    t = cfg.targets[0]
    assert t.qemu_bin == "qemu-riscv64"          # filled from preset default
    r = presets.resolve(t)
    assert "qemu-riscv64 ./build/app" in r.cmd
    assert r.full_system is False


def test_qemu_system_is_full_system_and_uses_machine():
    cfg = _load("""
        targets:
          - id: mcu/cortex-m
            build: "true"
            validate: qemu_system
            machine: lm3s6965evb
            artifact: build/fw.elf
            expect: "BOOT OK"
    """)
    r = presets.resolve(cfg.targets[0])
    assert r.full_system is True
    assert "-M lm3s6965evb" in r.cmd and "qemu-system-arm" in r.cmd


def test_custom_tier_uses_run_verbatim():
    cfg = _load("""
        targets:
          - id: weird/box
            build: "true"
            validate: custom
            run: "my-emulator --boot ./out.bin"
            expect: "alive"
    """)
    r = presets.resolve(cfg.targets[0])
    assert r.cmd == "my-emulator --boot ./out.bin"
    assert r.full_system is True  # custom defaults to full-system when timed


def test_custom_without_run_is_rejected():
    with pytest.raises(SystemExit):
        _load("""
            targets:
              - id: bad
                build: "true"
                validate: custom
        """)


def test_real_gpu_requests_a_gpu():
    cfg = _load("""
        targets:
          - id: infer/cuda
            build: "true"
            validate: real_gpu
            artifact: build/infer
    """)
    assert presets.resolve(cfg.targets[0]).gpu == "T4"


# ---- board catalog ---------------------------------------------------------

def test_board_expands_to_toolchain_bundle():
    cfg = _load("""
        targets:
          - id: pi/arm
            board: arm-linux
            build: "true"
            artifact: build/app
            expect: "ok"
    """)
    t = cfg.targets[0]
    assert t.validate == "qemu_user"
    assert "gcc-arm-linux-gnueabihf" in t.apt
    assert t.qemu_bin == "qemu-arm"


def test_explicit_field_overrides_board():
    cfg = _load("""
        targets:
          - id: pi/arm
            board: arm-linux
            build: "true"
            base: ubuntu:22.04
            artifact: build/app
    """)
    assert cfg.targets[0].base == "ubuntu:22.04"   # explicit wins over board default


def test_unknown_board_is_rejected():
    with pytest.raises(SystemExit):
        _load("""
            targets:
              - id: x
                board: nope
                build: "true"
        """)


# ---- matrix expansion ------------------------------------------------------

def test_matrix_fans_one_entry_into_many():
    cfg = _load("""
        targets:
          - id: linux-{arch}
            matrix:
              arch: [arm, aarch64, riscv64]
            build: "gcc-{arch} -o build/app"
            validate: custom
            run: "qemu-{arch} ./build/app"
            expect: "ok on {arch}"
    """)
    ids = [t.id for t in cfg.targets]
    assert ids == ["linux-arm", "linux-aarch64", "linux-riscv64"]
    riscv = cfg.targets[2]
    assert riscv.build == "gcc-riscv64 -o build/app"
    assert riscv.run == "qemu-riscv64 ./build/app"
    assert riscv.expect == ["ok on riscv64"]


def test_matrix_substitution_leaves_shell_braces_alone():
    cfg = _load("""
        targets:
          - id: t-{n}
            matrix:
              n: [1]
            build: "echo $((1+1)) && export X=${HOME}"
            validate: native
    """)
    # only {n} is a matrix var; shell ${HOME} / $((..)) must survive verbatim
    assert cfg.targets[0].build == "echo $((1+1)) && export X=${HOME}"


def test_duplicate_ids_rejected():
    with pytest.raises(SystemExit):
        _load("""
            targets:
              - id: dup
                build: "true"
                validate: native
              - id: dup
                build: "true"
                validate: native
        """)


def test_unknown_field_rejected():
    with pytest.raises(SystemExit):
        _load("""
            targets:
              - id: t
                build: "true"
                vaildate: native
        """)


def test_qemu_user_bin_alias_still_works():
    cfg = _load("""
        targets:
          - id: legacy
            build: "true"
            validate: qemu_user
            qemu_user_bin: qemu-arm
            artifact: build/app
    """)
    assert cfg.targets[0].qemu_bin == "qemu-arm"


# ---- the judge -------------------------------------------------------------

def _t(**kw) -> Target:
    kw.setdefault("id", "t")
    kw.setdefault("build", "true")
    return Target(**kw)


def test_judge_user_clean_exit_with_proof_passes():
    t = _t(validate="qemu_user", expect=["perception: engine ok"])
    assert runner._judge(t, 0, "perception: engine ok\n").ok


def test_judge_user_segfault_is_caught():
    t = _t(validate="qemu_user", expect=["loadtest: ok"])
    j = runner._judge(t, 139, "loadtest: starting up\n")
    assert not j.ok and "SIGSEGV" in j.detail


def test_judge_user_clean_exit_but_missing_proof_fails():
    t = _t(validate="qemu_user", expect=["never printed"])
    j = runner._judge(t, 0, "ran fine but quiet\n")
    assert not j.ok and "missing proof" in j.detail


def test_judge_full_system_ignores_exit_code():
    t = _t(validate="qemu_system", expect=["BOOT OK"])
    # emulator killed by timeout -> nonzero, but the boot proof is present
    assert runner._judge(t, 124, "...\nBOOT OK\n...").ok


def test_judge_regex_expectation():
    t = _t(validate="qemu_user", expect_regex=r"heartbeat #\d+")
    assert runner._judge(t, 0, "heartbeat #42\n").ok
    assert not runner._judge(t, 0, "no pulse\n").ok


def test_judge_expect_exit_code():
    t = _t(validate="native", expect_exit=3)
    assert runner._judge(t, 3, "").ok
    assert not runner._judge(t, 0, "").ok


def test_judge_multiple_substrings_all_required():
    t = _t(validate="native", expect=["a", "b"])
    assert runner._judge(t, 0, "a then b").ok
    assert not runner._judge(t, 0, "only a").ok


# ---- phase script + parsing ------------------------------------------------

def test_script_includes_test_phase_only_when_present():
    no_test = runner._script(_t(validate="native", artifact="build/x"))
    assert "TEST_BEGIN" not in no_test
    with_test = runner._script(_t(validate="native", artifact="build/x", test="pytest -q"))
    assert "TEST_BEGIN" in with_test and "pytest -q" in with_test


def test_script_exports_env():
    s = runner._script(_t(validate="native", env={"TOKEN": "abc 123"}))
    assert "export TOKEN='abc 123'" in s


def test_marker_and_slice_roundtrip():
    out = "\n".join([
        "::cilicon::BUILD_BEGIN",
        "compiling...",
        "::cilicon::BUILD_END rc=0 ms=1200",
        "::cilicon::VALIDATE_BEGIN",
        "BOOT OK",
        "::cilicon::VALIDATE_END rc=0 ms=300",
    ])
    assert runner._marker(out, "BUILD_END") == {"rc": "0", "ms": "1200"}
    assert runner._slice(out, "VALIDATE_BEGIN", "VALIDATE_END") == "BOOT OK"
    step = runner._phase_result(out, "BUILD", "build")
    assert step.ok and step.seconds == 1.2


# ---- reports ---------------------------------------------------------------

def _result(ok=True, with_test=False):
    t = _t(validate="qemu_user", expect=["ok"])
    r = runner.TargetResult(target=t)
    r.build = runner.StepResult("build", True, 1.0, "built", "ok")
    r.validate = runner.StepResult("validate", ok, 0.5, "ok\n", "loads + runs" if ok else "crash")
    if with_test:
        r.test = runner.StepResult("test", True, 0.2, "3 passed", "tests passed")
    return r


def test_json_report_shape():
    import json
    out = report.to_json([_result(True, with_test=True), _result(False)], 2.5)
    data = json.loads(out)
    assert data["passed"] == 1 and data["total"] == 2
    assert data["targets"][0]["test"]["ok"] is True
    assert data["targets"][1]["ok"] is False


# ---- size budgets ----------------------------------------------------------

def test_flash_max_human_string_parses_to_bytes():
    cfg = _load("""
        targets:
          - id: mcu
            board: cortex-m
            build: "true"
            artifact: build/fw.elf
            expect: "BOOT OK"
            size_tool: arm-none-eabi-size
            flash_max: 256K
            ram_max: 64K
    """)
    t = cfg.targets[0]
    assert t.flash_max == 256 * 1024 and t.ram_max == 64 * 1024
    assert t.has_size


def test_size_phase_in_script_uses_size_tool():
    from cilicon.config import Target
    t = Target(id="m", build="true", artifact="build/fw.elf",
               size_tool="arm-none-eabi-size", flash_max=1024)
    s = runner._script(t)
    assert "SIZE_BEGIN" in s and "arm-none-eabi-size build/fw.elf" in s


def test_sizes_parse_berkeley():
    from cilicon import sizes
    out = ("   text\t   data\t    bss\t    dec\t    hex\tfilename\n"
           "  12000\t    400\t   2000\t  14400\t   3840\tbuild/fw.elf\n")
    s = sizes.parse(out)
    assert (s.text, s.data, s.bss) == (12000, 400, 2000)
    assert s.flash == 12400 and s.ram == 2400


def test_sizes_parse_sysv():
    from cilicon import sizes
    out = (".text   12000  134217728\n"
           ".data     400  536870912\n"
           ".bss     2000  536871312\n")
    s = sizes.parse(out)
    assert s.flash == 12400 and s.ram == 2400


def test_size_within_budget_passes():
    from cilicon import sizes
    out = "text data bss dec hex f\n100 50 30 180 b4 f\n"
    rep = sizes.evaluate(out, flash_max=1024, ram_max=1024)
    assert rep.ok and "flash" in rep.detail


def test_size_over_budget_fails():
    from cilicon import sizes
    out = "text data bss dec hex f\n2000 100 50 2150 866 f\n"
    rep = sizes.evaluate(out, flash_max=1024, ram_max=1024)
    assert not rep.ok and "over budget" in rep.detail
    assert any("flash" in o for o in rep.over)


def test_size_report_in_json():
    import json
    t = _t(validate="qemu_system", expect=["BOOT OK"], flash_max=1024)
    r = runner.TargetResult(target=t)
    r.build = runner.StepResult("build", True, 1.0, "", "ok")
    r.validate = runner.StepResult("validate", True, 0.1, "BOOT OK", "boots")
    r.size = runner.StepResult("size", True, 0.0, "", "flash 500B/1.0K (49%)")
    r.sizes = {"text": 400, "data": 100, "bss": 50, "flash": 500, "ram": 150}
    data = json.loads(report.to_json([r], 1.1))
    assert data["targets"][0]["sizes"]["flash"] == 500
    assert data["targets"][0]["size"]["ok"] is True


def test_junit_report_is_wellformed():
    import xml.etree.ElementTree as ET
    xml = report.to_junit([_result(True), _result(False)], 2.5)
    root = ET.fromstring(xml)
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "2" and root.attrib["failures"] == "1"
    cases = root.findall("testcase")
    assert cases[0].find("failure") is None
    assert cases[1].find("failure") is not None


def test_junit_message_names_the_real_failing_step():
    # validate PASSES but the size gate fails -> message must say "size", not validate
    t = _t(validate="qemu_system", expect=["BOOT OK"], flash_max=1024)
    r = runner.TargetResult(target=t)
    r.build = runner.StepResult("build", True, 1.0, "", "ok")
    r.size = runner.StepResult("size", False, 0.0, "", "over budget: flash 0.3M/256K")
    r.validate = runner.StepResult("validate", True, 0.1, "BOOT OK", "boots, reaches main")
    xml = report.to_junit([r], 1.1)
    assert 'message="size: over budget' in xml


def test_markdown_summary_table():
    md = report.to_markdown([_result(True, with_test=True), _result(False)], 2.5)
    assert "cilicon — 1/2 targets" in md
    assert "| ✅ |" in md and "| ❌ |" in md
