"""The sandbox script + output parser — pure string logic, no Modal."""
from cilicon import runner
from cilicon.config import Target


def test_script_includes_size_phase_when_budgeted():
    t = Target(id="m", build="make", validate="qemu_system",
               artifact="build/fw.elf", size_tool="arm-none-eabi-size",
               flash_max=1024)
    script = runner._script(t)
    assert "SIZE_BEGIN" in script
    assert "arm-none-eabi-size build/fw.elf" in script
    # size runs before validate
    assert script.index("SIZE_BEGIN") < script.index("VALIDATE_BEGIN")


def test_script_omits_size_phase_without_budget():
    t = Target(id="m", build="make", validate="native")
    assert "SIZE_BEGIN" not in runner._script(t)


def test_script_default_size_tool_is_host_size():
    t = Target(id="m", build="make", validate="native", artifact="a.out", flash_max=1024)
    assert " size a.out" in runner._script(t)   # falls back to host `size`


def test_parse_phase_markers_split_output():
    out = "\n".join([
        "::cilicon::BUILD_BEGIN", "compiling...", "::cilicon::BUILD_END rc=0 ms=1200",
        "::cilicon::VALIDATE_BEGIN", "BOOT OK", "::cilicon::VALIDATE_END rc=0 ms=400",
    ])
    b = runner._phase_result(out, "BUILD", "build")
    assert b.ok and b.seconds == 1.2 and "compiling" in b.output
    v = runner._phase_result(out, "VALIDATE", "validate")
    assert v.ok and "BOOT OK" in v.output


def test_judge_full_system_needs_expect_not_clean_exit():
    t = Target(id="m", build="make", validate="qemu_system", expect=["BOOT OK"])
    # killed by timeout (rc!=0) but the proof string is present -> pass
    assert runner._judge(t, 124, "...\nBOOT OK\n").ok
    assert not runner._judge(t, 124, "...nothing...").ok


def test_judge_user_tier_catches_crash():
    t = Target(id="m", build="make", validate="qemu_user", expect=["ok"])
    j = runner._judge(t, 139, "starting up")
    assert not j.ok and "SIGSEGV" in j.detail
