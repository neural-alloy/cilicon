"""the CLI evidence orchestration (_handle_evidence): the gate runs, a red target is
never signed, a green one is, and the bundle is written. tools are monkeypatched so
no syft/grype/cosign is needed."""
import argparse
import json

from cilicon import cli, tools
from cilicon.config import Target
from cilicon.runner import StepResult, TargetResult


def _built(tid, artifact):
    r = TargetResult(target=Target(id=tid, validate="native", vuln_gate="critical",
                                   artifacts=["build/*.elf"]))
    r.build = StepResult("build", True, 1.0, "", "ok")
    r.validate = StepResult("validate", True, 1.0, "ran", "ok")
    r.artifacts = [artifact]        # a real, pulled file
    return r


def _args(evidence_path):
    return argparse.Namespace(evidence=evidence_path, sbom=True, sign=True,
                              cosign_key=None, kev_catalog=None)


def test_gate_blocks_and_only_green_is_signed(tmp_path, monkeypatch):
    clean = tmp_path / "clean.elf"; clean.write_bytes(b"clean")
    dirty = tmp_path / "dirty.elf"; dirty.write_bytes(b"dirty")
    good = _built("good", str(clean))
    bad = _built("bad", str(dirty))

    # monkeypatch the impure adapters: 'bad' scans up a critical CVE, 'good' is clean.
    monkeypatch.setattr(tools, "digest_file", lambda p: "deadbeef")
    monkeypatch.setattr(tools, "sbom", lambda p, warn=print: "{}")
    monkeypatch.setattr(tools, "sign_blob", lambda p, k=None, warn=print: ("SIG", "CERT"))

    def fake_scan(path, warn=print):
        return {"matches": [{"vulnerability": {"id": "CVE-X", "severity": "Critical"},
                             "artifact": {"name": "openssl"}}]} if "dirty" in path else {"matches": []}
    monkeypatch.setattr(tools, "scan", fake_scan)

    out = tmp_path / "evidence.json"
    cli._handle_evidence(_args(str(out)), [good, bad])

    # the gate flipped 'bad' red; 'good' stayed green
    assert bad.vuln is not None and bad.vuln.ok is False and "CVE-X" in bad.vuln.detail
    assert bad.ok is False and good.ok is True

    # green 'good' got a signed evidence entry; red 'bad' was NOT signed
    assert good.evidence and good.evidence[0]["signed"] is True
    assert all(not e.get("signed") for e in bad.evidence)   # (in fact bad has no entry)
    assert bad.evidence == []

    bundle = json.loads(out.read_text())
    assert bundle["artifacts"] == 1 and bundle["signed"] == 1
    assert bundle["entries"][0]["target"] == "good"
