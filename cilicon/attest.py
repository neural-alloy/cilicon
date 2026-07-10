"""cilicon attestation: sign a boot-test statement with the user's own key.

`cilicon run --attestation <path>` emits a **DSSE envelope** over an **in-toto
Statement** describing what cilicon just built and booted. This is the free
tier's one cryptographic act: it binds a sha256 of the REAL artifact bytes to a
signed claim about the on-target run. It signs; it does not verify across a fleet
and it names no root cause — that reasoning lives in the paid control plane
(WEDGE_SPEC moat boundary).

Two honesty rails, both enforced here, never from user config:
  * **fidelity** (WHAT RAN) comes from the tier — an ELF_LOAD result can never be
    described as a boot (presets.fidelity_of). See WEDGE_SPEC §0.2.
  * **assurance** (WHO SIGNED) is fixed at SELF_ATTESTED — the user's own runner,
    the user's own key. cilicon cannot mint NEURAL_ALLOY_EMULATED / REAL_SILICON_
    ENCLAVE; those are the control plane's to grant.

Ed25519 only. No key ⇒ no envelope (we never emit an unsigned one).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Optional

from . import presets, report

# DSSE / in-toto constants (fixed by the specs, not configurable).
PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://neuralalloy.dev/boot-test/v1"

# assurance is WHO SIGNED. A standalone cilicon run is always the user attesting to
# their own runner with their own key — the lowest rung. Config cannot raise it.
ASSURANCE_SELF_ATTESTED = "SELF_ATTESTED"

# terminating_event: how the on-target run ended, detected SYNTACTICALLY only
# (validator ran? clean pass? a fixed panic marker? the boot timeout?) — never a
# root cause. Ranking *why* it panicked is the moat, and is not done here.
BOOT_OK = "boot_ok"
PANIC = "panic"
TIMEOUT = "timeout"
DID_NOT_RUN = "did_not_run"


# ---- keys ------------------------------------------------------------------
# We defer the `cryptography` import into the functions that need it so importing
# this module (e.g. for the pure helpers below, or under the test collector) never
# hard-requires the native lib to be present.

def _load_priv_bytes(data: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(data) == 32:                                   # raw 32-byte seed
        return Ed25519PrivateKey.from_private_bytes(data)
    stripped = data.lstrip()
    if stripped.startswith(b"-----BEGIN"):                # PEM PKCS8
        return serialization.load_pem_private_key(data, password=None)
    try:                                                  # DER PKCS8
        return serialization.load_der_private_key(data, password=None)
    except Exception:
        trimmed = data.rstrip(b"\r\n")                    # raw seed w/ trailing newline
        if len(trimmed) == 32:
            return Ed25519PrivateKey.from_private_bytes(trimmed)
        raise ValueError("unrecognized signing key (want a 32-byte raw seed or PKCS8 PEM/DER)")


def load_signing_key(path: Optional[str], env_value: Optional[str]):
    """Resolve the Ed25519 signing key from --signing-key or CILICON_SIGNING_KEY.

    NO key ⇒ a clear error and no file — we never silently emit an unsigned
    envelope. The env value may be a path or a base64 raw seed."""
    if path:
        with open(path, "rb") as f:
            return _load_priv_bytes(f.read())
    if env_value:
        if os.path.exists(env_value):
            with open(env_value, "rb") as f:
                return _load_priv_bytes(f.read())
        try:
            return _load_priv_bytes(base64.b64decode(env_value))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"CILICON_SIGNING_KEY is neither a path nor a base64 seed: {e}")
    raise ValueError(
        "attestation requested but no signing key — pass --signing-key <file> "
        "or set CILICON_SIGNING_KEY (Ed25519 PKCS8 or 32-byte raw seed)"
    )


def load_public_key(path: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    with open(path, "rb") as f:
        data = f.read()
    if len(data) == 32:
        return Ed25519PublicKey.from_public_bytes(data)
    if data.lstrip().startswith(b"-----BEGIN"):
        return serialization.load_pem_public_key(data)
    return serialization.load_der_public_key(data)


def keyid_of(pubkey) -> str:
    """A stable keyid: sha256 of the raw 32-byte public key, hex. Deterministic so
    a verifier can tie an envelope's `keyid` to a known public key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = pubkey.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


# ---- DSSE ------------------------------------------------------------------

def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding (lengths in BYTES, ASCII decimal):
    `DSSEv1 SP len(type) SP type SP len(payload) SP payload`. The signature is
    over THIS, so payloadType is bound to the payload and can't be swapped."""
    pt = payload_type.encode("utf-8")
    return b" ".join(
        [b"DSSEv1", str(len(pt)).encode(), pt, str(len(payload)).encode(), payload]
    )


def sign_statement(statement: dict, privkey) -> dict:
    """Serialize the statement ONCE, sign the PAE over those raw bytes, and carry
    exactly those bytes (base64) in the envelope. The verifier re-derives the PAE
    from the base64 payload — we never re-serialize, so formatting can't drift."""
    payload = json.dumps(statement, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = privkey.sign(pae(PAYLOAD_TYPE, payload))
    return {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        # EXACTLY ONE signature — one runner, one key, one claim.
        "signatures": [
            {
                "keyid": keyid_of(privkey.public_key()),
                "sig": base64.b64encode(sig).decode("ascii"),
            }
        ],
    }


def verify_envelope(envelope: dict, pubkey) -> tuple[bool, str]:
    """Check a DSSE envelope against a public key. Returns (ok, reason). Verifies
    the signature over the PAE of the RAW payload bytes — never a re-serialization."""
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        return False, f"unexpected payloadType {envelope.get('payloadType')!r}"
    sigs = envelope.get("signatures") or []
    if len(sigs) != 1:
        return False, f"expected exactly one signature, got {len(sigs)}"
    try:
        payload = base64.b64decode(envelope["payload"])
        sig = base64.b64decode(sigs[0]["sig"])
    except Exception as e:  # noqa: BLE001
        return False, f"malformed base64 in envelope: {e}"
    try:
        pubkey.verify(sig, pae(PAYLOAD_TYPE, payload))
    except Exception:
        return False, "signature does not verify against this key"
    return True, "ok"


# ---- statement -------------------------------------------------------------

def sha256_file(path: str) -> str:
    """sha256 over the REAL bytes on disk. This is the ONE place in the whole
    system where a digest is bound to actual artifact bytes — the control plane
    never sees the artifact, so this binding must happen here, at the runner."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def locate_artifact(result) -> Optional[str]:
    """The local, pulled-back file for a target's built artifact, or None. Matches
    the artifact basename among the files `--artifacts` fetched; falls back to the
    sole pulled file. A digest is only ever bound to bytes we actually have."""
    paths = list(getattr(result, "artifacts", []) or [])
    if not paths:
        return None
    want = os.path.basename(getattr(result.target, "artifact", "") or "")
    if want:
        for p in paths:
            if os.path.basename(p) == want:
                return p
    return paths[0] if len(paths) == 1 else None


def terminating_event(result) -> str:
    """How the on-target run ended — a coarse SYNTACTIC bucket, not a diagnosis:
      * did_not_run — build failed / infra error / the validator never executed
      * boot_ok     — the validate step passed (proof met, no fault marker)
      * panic       — a fixed crash marker was seen, OR the guest ended before the
                      proof without hitting the timeout (abnormal termination)
      * timeout     — the boot ran out its clock
    No root-cause, no suspect ranking (WEDGE_SPEC moat)."""
    if result.error or not (result.build and result.build.ok):
        return DID_NOT_RUN
    v = result.validate
    if v is None or v.detail in ("validator never ran", "skipped (build failed)") \
            or (v.detail or "").startswith("skipped"):
        return DID_NOT_RUN
    if v.ok:
        return BOOT_OK
    out = v.output or ""
    for marker in presets.crash_signatures(result.target.validate):
        if marker and marker in out:
            return PANIC
    bt = getattr(result.target, "boot_timeout", 0) or 0
    if (v.detail or "").startswith("hung") or (bt and v.seconds >= bt * 0.9):
        return TIMEOUT
    return PANIC


def _result_block(result) -> dict:
    event = terminating_event(result)
    passed = bool(result.ok)
    # A passing boot that didn't terminate boot_ok is a contradiction — refuse to
    # sign it rather than emit a claim we can't stand behind.
    if passed and event != BOOT_OK:
        raise ValueError(
            f"target {result.target.id}: passed=True but terminating_event={event!r}"
        )
    v = result.validate
    return {
        "target": result.target.id,
        "tier": result.target.validate,
        # fidelity is the TIER's, never config's — an ELF load is never a boot.
        "fidelity": presets.fidelity_of(result.target),
        "passed": passed,
        "terminating_event": event,
        "boot_ms": int(round((v.seconds if v else 0.0) * 1000)),
        "flash_bytes": (result.sizes or {}).get("flash"),
        # sha256 of the SAME console tail the --json report prints, so a consumer
        # can recompute it from either artifact and cross-check.
        "console_sha256": hashlib.sha256(
            report.output_tail(v).encode("utf-8") if v else b""
        ).hexdigest(),
    }


def build_statement(results, runner: str, issued: Optional[int] = None) -> dict:
    """Build the in-toto Statement over a matrix run. `subject` binds each located
    artifact to a sha256 of its real bytes; `predicate.results` records the
    per-target on-target outcome. Raises if no artifact bytes could be bound —
    an attestation with nothing signed-over is not one."""
    issued = int(time.time()) if issued is None else issued
    subjects = []
    for r in results:
        path = locate_artifact(r)
        if path:
            subjects.append(
                {"name": os.path.basename(path), "digest": {"sha256": sha256_file(path)}}
            )
    if not subjects:
        raise ValueError(
            "no artifact bytes to attest — run with --artifacts <dir> and declare "
            "`artifacts:` on the target(s) so the built binary is pulled back to hash"
        )
    return {
        "_type": STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "assurance": ASSURANCE_SELF_ATTESTED,
            "runner": runner,
            "issued": issued,
            "results": [_result_block(r) for r in results],
        },
    }


def make_attestation(results, privkey, runner: str, issued: Optional[int] = None) -> dict:
    """The whole act: statement over the real bytes → DSSE envelope, signed once."""
    return sign_statement(build_statement(results, runner, issued), privkey)
