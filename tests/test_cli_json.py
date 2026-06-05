"""The --json report builders behind `cilicon targets` / `cilicon doctor`.

These power the offline, agent-consumable commands, so they're pure (cfg in,
dict out) and tested without Modal."""
import textwrap

from cilicon import config as cfgmod
from cilicon.cli import _doctor_report, _targets_report


def _load(tmp_path, yaml_text):
    p = tmp_path / "cilicon.yml"
    p.write_text(textwrap.dedent(yaml_text))
    return cfgmod.load(str(p))


def test_targets_report_shape(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: node/arm
            board: arm-linux
            build: echo hi
            artifact: build/x
            expect: "engine ok"
    """)
    rep = _targets_report(cfg)
    assert rep["tool"] == "cilicon"
    assert rep["count"] == 1
    t = rep["targets"][0]
    assert t["id"] == "node/arm"
    assert t["validate"] == "qemu_user"
    assert t["proves"] == "engine ok"
    assert t["test"] is None


def test_doctor_report_ok(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: node/arm
            board: arm-linux
            build: echo hi
            artifact: build/x
            expect: "engine ok"
    """)
    rep = _doctor_report(cfg)
    assert rep["ok"] is True
    assert rep["errors"] == 0
    assert rep["targets"][0]["ok"] is True
    assert rep["targets"][0]["warnings"] == []


def test_doctor_report_warns_on_no_expect(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: weak
            board: arm-linux
            build: echo hi
            artifact: build/x
    """)
    rep = _doctor_report(cfg)
    assert rep["ok"] is True   # a missing expect is a warning, not an error
    warns = rep["targets"][0]["warnings"]
    assert any("only means 'didn't crash'" in w for w in warns)


def test_doctor_report_errors_on_bad_tier(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: needs-script
            build: echo hi
            validate: renode
            artifact: build/x
            expect: "ok"
    """)
    rep = _doctor_report(cfg)
    assert rep["ok"] is False
    assert rep["errors"] == 1
    t = rep["targets"][0]
    assert t["ok"] is False
    assert any("renode" in e for e in t["errors"])
