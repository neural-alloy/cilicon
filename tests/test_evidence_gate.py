"""the evidence spine WIRED IN: the vuln gate really flips a target red via
runner.ok, and reports surface vuln / triage / evidence / signed markers."""
import json

from cilicon import report, vuln
from cilicon.config import Target
from cilicon.runner import StepResult, TargetResult


def _green(tid="stm32/cortex-m"):
    r = TargetResult(target=Target(id=tid, validate="qemu_system"))
    r.build = StepResult("build", True, 3.0, "", "ok")
    r.validate = StepResult("validate", True, 2.0, "boots, reaches main", "ok")
    r.sizes = {"flash": 12160}
    r.size = StepResult("size", True, 0.1, "", "flash 12K")
    return r


def _vuln_step(policy, scan, waivers=None):
    rep = vuln.evaluate(scan, policy, waivers=waivers)
    return StepResult("vuln", rep.ok, 0.0, json.dumps(rep.to_dict()), rep.detail)


def test_vuln_gate_flips_ok_red_through_the_runner():
    r = _green()
    assert r.ok is True                                  # built + booted + fits
    r.vuln = _vuln_step("critical", {"matches": [
        {"vulnerability": {"id": "CVE-1", "severity": "Critical"}, "artifact": {"name": "openssl"}}]})
    assert r.ok is False                                 # ...but a critical CVE gates it
    assert "CVE-1" in r.vuln.detail


def test_waived_cve_keeps_the_target_green():
    r = _green()
    r.vuln = _vuln_step("critical", {"matches": [
        {"vulnerability": {"id": "CVE-2", "severity": "Critical"}, "artifact": {"name": "z"}}]},
        waivers=["CVE-2"])
    assert r.ok is True                                  # waived -> reported, not gated


def test_reports_surface_vuln_triage_and_evidence():
    good = _green("a")
    good.evidence = [{"target": "a", "signed": True, "digest": "sha256:x"}]
    bad = _green("b")
    bad.vuln = _vuln_step("critical", {"matches": [
        {"vulnerability": {"id": "CVE-9", "severity": "Critical"}, "artifact": {"name": "p"}}]})

    payload = json.loads(report.to_json([good, bad], 5.0))
    tgts = {t["id"]: t for t in payload["targets"]}
    assert tgts["b"]["vuln"]["ok"] is False              # the gate shows in json
    assert tgts["a"]["evidence"][0]["signed"] is True    # evidence shows in json
    assert payload["passed"] == 1 and "triage" in payload

    md = report.to_markdown([good, bad], 5.0)
    assert "🛡" in md                                     # vuln gate rendered
    assert "🔏 signed" in md                              # signed marker on the green row
    assert "CVE-9" in md                                  # the failing cve named


def test_junit_names_the_vuln_culprit():
    r = _green("c")
    r.vuln = _vuln_step("high", {"matches": [
        {"vulnerability": {"id": "CVE-7", "severity": "High"}, "artifact": {"name": "q"}}]})
    xml = report.to_junit([r], 5.0)
    assert 'failures="1"' in xml and "vuln:" in xml and "CVE-7" in xml
