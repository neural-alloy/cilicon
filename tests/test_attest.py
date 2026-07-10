"""Attestation: DSSE envelope + in-toto boot-test statement, signed Ed25519.

All offline, no Modal. Uses `cryptography` (a core dep now) to mint an ephemeral
key per test."""
import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cilicon import attest
from cilicon.config import Target
from cilicon.runner import StepResult, TargetResult


def _key():
    return Ed25519PrivateKey.generate()


def _boot_result(tmp_path, artifact_bytes=b"\x7fELF-fake-arm64", tier="qemu_system_linux_aarch64"):
    p = tmp_path / "perception-arm64"
    p.write_bytes(artifact_bytes)
    t = Target(id="jetson-thor/linux-boot", build="make", validate=tier,
               artifact="build/perception-arm64", boot_timeout=120)
    return TargetResult(
        target=t,
        build=StepResult("build", True, 1.0, "ok"),
        validate=StepResult("validate", True, 3.2,
                            "::cilicon-boot:: kernel booted to PID1 --\nperception: engine ok",
                            "boots, reaches main ('perception: engine ok')"),
        sizes={"flash": 187},
        artifacts=[str(p)],
    )


# ---- PAE (DSSE Pre-Authentication Encoding) --------------------------------

def test_pae_vector():
    # lengths in BYTES, ASCII decimal, single spaces
    got = attest.pae("application/vnd.in-toto+json", b"hello")
    assert got == b"DSSEv1 28 application/vnd.in-toto+json 5 hello"


# ---- sign / verify roundtrip -----------------------------------------------

def test_sign_then_verify_roundtrips(tmp_path):
    priv = _key()
    stmt = attest.build_statement([_boot_result(tmp_path)], runner="cilicon/test")
    env = attest.sign_statement(stmt, priv)
    assert env["payloadType"] == attest.PAYLOAD_TYPE
    assert len(env["signatures"]) == 1                       # EXACTLY one signature
    ok, why = attest.verify_envelope(env, priv.public_key())
    assert ok, why


def test_tampered_signature_fails(tmp_path):
    priv = _key()
    env = attest.make_attestation([_boot_result(tmp_path)], priv, runner="r")
    raw = bytearray(base64.b64decode(env["signatures"][0]["sig"]))
    raw[0] ^= 0x01                                           # flip one bit of the sig
    env["signatures"][0]["sig"] = base64.b64encode(bytes(raw)).decode()
    ok, why = attest.verify_envelope(env, priv.public_key())
    assert not ok and "does not verify" in why


def test_tampered_payload_fails(tmp_path):
    priv = _key()
    env = attest.make_attestation([_boot_result(tmp_path)], priv, runner="r")
    stmt = json.loads(base64.b64decode(env["payload"]))
    stmt["predicate"]["assurance"] = "REAL_SILICON_ENCLAVE"  # forge a higher tier
    env["payload"] = base64.b64encode(
        json.dumps(stmt, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    ok, _ = attest.verify_envelope(env, priv.public_key())
    assert not ok


def test_wrong_key_fails(tmp_path):
    env = attest.make_attestation([_boot_result(tmp_path)], _key(), runner="r")
    ok, _ = attest.verify_envelope(env, _key().public_key())  # a different key
    assert not ok


def test_more_than_one_signature_rejected(tmp_path):
    priv = _key()
    env = attest.make_attestation([_boot_result(tmp_path)], priv, runner="r")
    env["signatures"].append(dict(env["signatures"][0]))
    ok, why = attest.verify_envelope(env, priv.public_key())
    assert not ok and "one signature" in why


# ---- subject digest is bound to REAL bytes ---------------------------------

def test_subject_digest_is_sha256_of_real_file(tmp_path):
    payload = b"the actual built binary bytes \x00\x01\x02"
    r = _boot_result(tmp_path, artifact_bytes=payload)
    stmt = attest.build_statement([r], runner="r")
    assert len(stmt["subject"]) == 1
    assert stmt["subject"][0]["name"] == "perception-arm64"
    assert stmt["subject"][0]["digest"]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_no_artifact_bytes_is_an_error():
    t = Target(id="x", build="make", validate="qemu_system_linux_aarch64")
    r = TargetResult(target=t, build=StepResult("build", True, 1.0, "ok"),
                     validate=StepResult("validate", True, 1.0, "ok", "ok"))  # no artifacts
    with pytest.raises(ValueError):
        attest.build_statement([r], runner="r")


# ---- fidelity comes from the tier, never config ----------------------------

def test_elf_load_result_is_never_a_boot(tmp_path):
    # a qemu_user target that "passed" is still ELF_LOAD in the statement
    r = _boot_result(tmp_path, tier="qemu_user_aarch64")
    block = attest.build_statement([r], runner="r")["predicate"]["results"][0]
    assert block["fidelity"] == "ELF_LOAD"
    assert block["passed"] is True and block["terminating_event"] == "boot_ok"


def test_assurance_is_self_attested(tmp_path):
    stmt = attest.build_statement([_boot_result(tmp_path)], runner="r")
    assert stmt["predicate"]["assurance"] == "SELF_ATTESTED"
    assert stmt["predicateType"] == "https://neuralalloy.dev/boot-test/v1"


# ---- terminating_event: syntactic buckets only -----------------------------

def _r(tier="qemu_system_linux_aarch64", **kw):
    t = Target(id="t", build="make", validate=tier, boot_timeout=120)
    return TargetResult(target=t, **kw)


def test_event_boot_ok():
    r = _r(build=StepResult("build", True, 1, "ok"),
           validate=StepResult("validate", True, 2, "perception: engine ok", "boots"))
    assert attest.terminating_event(r) == "boot_ok"


def test_event_did_not_run_when_build_failed():
    r = _r(build=StepResult("build", False, 1, "err", "exit 2"))
    assert attest.terminating_event(r) == "did_not_run"


def test_event_panic_on_crash_marker():
    r = _r(build=StepResult("build", True, 1, "ok"),
           validate=StepResult("validate", False, 2, "boot...\nKernel panic - not syncing",
                               "exited before reaching proof"))
    assert attest.terminating_event(r) == "panic"


def test_event_timeout_when_clock_runs_out():
    r = _r(build=StepResult("build", True, 1, "ok"),
           validate=StepResult("validate", False, 118, "still booting...",
                               "hung — no 'proof' in 118s"))
    assert attest.terminating_event(r) == "timeout"


def test_passed_but_not_boot_ok_is_refused(monkeypatch):
    # Invariant guard: passed=True with terminating_event != boot_ok is a
    # contradiction the block builder must REFUSE to sign. In practice the judge
    # keeps them consistent; force a divergence to prove the guard fires.
    r = _r(build=StepResult("build", True, 1, "ok"),
           validate=StepResult("validate", True, 2, "perception: engine ok", "boots"))
    assert r.ok
    monkeypatch.setattr(attest, "terminating_event", lambda _r: "panic")
    with pytest.raises(ValueError):
        attest._result_block(r)


# ---- keys ------------------------------------------------------------------

def test_no_key_raises_clear_error():
    with pytest.raises(ValueError) as e:
        attest.load_signing_key(None, None)
    assert "no signing key" in str(e.value)


def test_raw_seed_key_loads_and_keyid_is_stable(tmp_path):
    seed = bytes(range(32))
    kf = tmp_path / "key.seed"
    kf.write_bytes(seed)
    priv = attest.load_signing_key(str(kf), None)
    # keyid is sha256 of the raw public key, deterministic for a fixed seed
    expect = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    assert attest.keyid_of(priv.public_key()) == attest.keyid_of(expect)


def test_console_sha256_matches_reported_tail(tmp_path):
    from cilicon import report
    r = _boot_result(tmp_path)
    block = attest.build_statement([r], runner="r")["predicate"]["results"][0]
    tail = report.output_tail(r.validate)
    assert block["console_sha256"] == hashlib.sha256(tail.encode()).hexdigest()
