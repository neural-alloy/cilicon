"""Reports: the markdown check-run summary + JSON/JUnit, including size."""
from cilicon import report
from cilicon.config import Target
from cilicon.runner import StepResult, TargetResult


def _passing():
    t = Target(id="stm32/cortex-m", build="make", validate="qemu_system")
    return TargetResult(
        target=t,
        build=StepResult("build", True, 1.2, "ok"),
        validate=StepResult("validate", True, 0.4, "BOOT OK", "boots, reaches main ('BOOT OK')"),
        sizes={"text": 12048, "data": 112, "bss": 560, "flash": 12160, "ram": 672},
        size=StepResult("size", True, 0.0, "", "flash 11.9K/64.0K"),
    )


def _failing():
    t = Target(id="pi/linux-arm", build="make", validate="qemu_user")
    return TargetResult(
        target=t,
        build=StepResult("build", True, 2.0, "ok"),
        validate=StepResult("validate", False, 0.1, "loadtest: starting up\nSegfault",
                            "crash (SIGSEGV), caught pre-flash"),
    )


def test_markdown_has_table_and_failures():
    md = report.to_markdown([_passing(), _failing()], wall_seconds=130)
    assert "1/2 targets" in md
    assert "| `stm32/cortex-m` |" in md
    assert "12K flash" in md
    assert "SIGSEGV" in md                     # failing tail surfaced
    assert "<details>" in md


def test_markdown_all_green():
    md = report.to_markdown([_passing()], wall_seconds=5)
    assert md.startswith("### ✅")
    assert "<details>" not in md               # no failures section


def test_json_includes_sizes():
    import json
    payload = json.loads(report.to_json([_passing()], 5.0))
    tgt = payload["targets"][0]
    assert tgt["sizes"]["flash"] == 12160
    assert tgt["size"]["ok"] is True
    assert payload["passed"] == 1


def test_junit_failure_names_real_culprit():
    xml = report.to_junit([_failing()], 5.0)
    assert 'failures="1"' in xml
    assert "validate:" in xml                  # the step that actually failed
    assert "SIGSEGV" in xml
