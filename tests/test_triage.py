"""triage.py is pure: fingerprint + cluster failing targets, advisory only."""
from cilicon import triage
from cilicon.config import Target
from cilicon.runner import StepResult, TargetResult


def _res(tid, tier="qemu_user", *, build_ok=True, validate_ok=True, out="", error=""):
    r = TargetResult(target=Target(id=tid, validate=tier))
    r.error = error
    if not error:
        r.build = StepResult("build", build_ok, 1.0, "" if build_ok else out, "" if build_ok else "exit 1")
        if build_ok:
            r.validate = StepResult("validate", validate_ok, 1.0, out, "ok" if validate_ok else "crash")
    return r


def test_passing_target_has_no_fingerprint():
    assert triage.fingerprint(_res("ok1")) is None


def test_fingerprint_names_the_failing_phase_and_signature():
    fp = triage.fingerprint(_res("t1", out="boot\nSegmentation fault\n", validate_ok=False))
    assert fp.phase == "validate" and fp.signature == "Segmentation fault"


def test_same_failure_across_boards_is_one_cluster():
    # two targets, same tier, same normalized crash -> one root cause of count 2
    rs = [
        _res("a", out="run at 0x4001\nSegmentation fault\n", validate_ok=False),
        _res("b", out="run at 0x8ffe\nSegmentation fault\n", validate_ok=False),  # diff addr, same fault
        _res("c", out="linker error: undefined ref", build_ok=False),            # different root cause
        _res("ok", out="all good"),                                              # passes
    ]
    clusters = triage.cluster(rs)
    assert len(clusters) == 2                       # not 3: a+b collapse, c separate
    top = clusters[0]
    assert top.count == 2 and set(top.targets) == {"a", "b"} and top.phase == "validate"


def test_summarize_counts_failures_and_root_causes():
    rs = [_res("a", out="Segmentation fault", validate_ok=False),
          _res("b", out="Segmentation fault", validate_ok=False),
          _res("ok", out="fine")]
    s = triage.summarize(rs)
    assert s["failures"] == 2 and s["root_causes"] == 1
    assert s["clusters"][0]["status"] == "new"


def test_history_marks_known_vs_new():
    rs = [_res("a", out="Segmentation fault", validate_ok=False)]
    hist = triage.merge_history(rs, {}, tag="2026-06-30")
    s = triage.summarize(rs, history=hist)
    assert s["clusters"][0]["status"] == "known" and s["clusters"][0]["since"] == "2026-06-30"


def test_infra_error_is_its_own_bucket():
    fp = triage.fingerprint(_res("x", error="ModalError: sandbox died"))
    assert fp.phase == "infra"
