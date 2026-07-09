"""evidence.py is pure: the SBOM/sign/provenance bundle shape, no syft/cosign."""
import base64
import json

from cilicon import evidence


def test_sha256_and_b64_roundtrip():
    assert evidence.sha256_hex(b"hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert base64.b64decode(evidence.b64("hi")).decode() == "hi"


def test_provenance_is_intoto_slsa():
    ci = evidence.ci_context({
        "GITHUB_WORKFLOW": "ci", "GITHUB_RUN_ID": "42",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "acme/fw",
        "GITHUB_SHA": "deadbeef",
    })
    p = evidence.provenance("firmware.elf", "abc123", ci)
    assert p["_type"] == "https://in-toto.io/Statement/v1"
    assert p["predicateType"] == "https://slsa.dev/provenance/v1"
    assert p["subject"][0]["digest"]["sha256"] == "abc123"
    assert p["predicate"]["builder"]["id"] == "ci/42"
    assert p["predicate"]["invocation"]["configSource"]["uri"] == "https://github.com/acme/fw"
    assert p["predicate"]["invocation"]["configSource"]["digest"]["sha1"] == "deadbeef"


def test_entry_marks_signed_and_omits_missing_receipts():
    signed = evidence.entry("t1", "a.elf", "d1", sbom="{}", signature="MEUC", cert="---")
    assert signed["signed"] is True
    assert signed["digest"] == "sha256:d1"
    assert signed["sbom_format"] == "cyclonedx-json"
    assert base64.b64decode(signed["sbom"]).decode() == "{}"

    bare = evidence.entry("t2", "b.elf", "d2")   # no tools ran
    assert bare["signed"] is False
    assert bare["sbom"] is None and bare["signature"] is None


def test_bundle_counts_only_actually_signed():
    entries = [
        evidence.entry("t1", "a.elf", "d1", signature="s"),
        evidence.entry("t2", "b.elf", "d2"),              # unsigned
    ]
    b = evidence.bundle(entries)
    assert b["artifacts"] == 2 and b["signed"] == 1
    assert b["schema_version"] == evidence.SCHEMA_VERSION and b["signing"] == "keyless"
    # provenance embeds as decodable json
    p = evidence.provenance("x", "d", evidence.ci_context({}))
    e = evidence.entry("t", "x", "d", prov=p)
    assert json.loads(base64.b64decode(e["provenance"]))["predicate"]["buildType"] == "cilicon/v1"
