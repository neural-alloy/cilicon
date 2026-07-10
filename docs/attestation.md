# `cilicon run --attestation` — the signed boot-test claim

The free tier can **sign a statement about what it just built and booted**. It is
the one cryptographic act in cilicon: a DSSE envelope over an in-toto Statement
that binds a sha256 of the **real artifact bytes** to the on-target outcome, under
the user's own Ed25519 key.

cilicon **signs**; it does not verify across a fleet and it names **no root cause**
— that reasoning is the paid control plane's (see the moat boundary in the wedge
spec). On a boot failure the console is still printed verbatim and uninterpreted;
the attestation records only *syntactic* facts (did the validator run? did it
pass? a fixed panic marker? the boot timeout?).

## Two honesty axes (WEDGE_SPEC §0.2)

| axis | question | who sets it |
|---|---|---|
| **fidelity** | WHAT RAN — `ELF_LOAD` / `FULL_SYSTEM_BOOT` / `REAL_HARDWARE` | the **tier**, never config |
| **assurance** | WHO SIGNED — `SELF_ATTESTED` / … | fixed at `SELF_ATTESTED` here |

A standalone cilicon run is always `SELF_ATTESTED`: the user's own runner, the
user's own key. cilicon cannot mint a higher assurance — that is the control
plane's to grant. An `ELF_LOAD` result can never be described as a boot.

## Use

```bash
# key: an Ed25519 PKCS8 (PEM/DER) or a 32-byte raw seed file; or CILICON_SIGNING_KEY
cilicon run --artifacts out/ --attestation boot.att.json --signing-key ed25519.seed

# anyone can verify independently against the public key
cilicon verify-attestation boot.att.json --key ed25519.pub
```

`--attestation` requires `--artifacts <dir>` and an `artifacts:` glob on the
target(s): the built binary is pulled back so its **real bytes** are hashed here,
at the runner — the control plane never sees the artifact, so this binding must
happen at the one place that has the bytes. **No key ⇒ no envelope** (never a
silent unsigned one) and the run aborts before any cloud spend.

## The envelope (DSSE)

```jsonc
{
  "payloadType": "application/vnd.in-toto+json",
  "payload": "<base64 of the in-toto Statement>",
  "signatures": [ { "keyid": "<sha256(pubkey) hex>", "sig": "<base64>" } ]  // EXACTLY one
}
```

The signature is over the **DSSE PAE**
`DSSEv1 SP len(payloadType) SP payloadType SP len(payload) SP payload` (lengths in
bytes) of the **raw** payload. A verifier re-derives the PAE from the base64
payload — the JSON is never re-serialized, so formatting can't drift.

## The Statement (in-toto)

```jsonc
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [ { "name": "<artifact basename>", "digest": { "sha256": "<hex of REAL bytes>" } } ],
  "predicateType": "https://neuralalloy.dev/boot-test/v1",
  "predicate": {
    "assurance": "SELF_ATTESTED",
    "runner": "cilicon/<ver> qemu@modal",
    "issued": 1752160000,
    "results": [ {
      "target": "jetson-thor/linux-boot",
      "tier": "qemu_system_linux_aarch64",
      "fidelity": "FULL_SYSTEM_BOOT",           // from the tier, not config
      "passed": true,
      "terminating_event": "boot_ok",           // boot_ok | panic | timeout | did_not_run
      "boot_ms": 14700,
      "flash_bytes": 187,
      "console_sha256": "<sha256 of the same output_tail the --json report prints>"
    } ]
  }
}
```

Invariants enforced when signing (each has a test in `tests/test_attest.py`):

- **Ed25519** only; the digest is over the real built bytes (`shasum -a 256` of the
  pulled artifact equals `subject[].digest.sha256`).
- `passed: true` with `terminating_event != boot_ok` is a contradiction — refused.
- `fidelity` comes from the tier; an `ELF_LOAD` result is never a boot.
- `terminating_event` is a coarse **syntactic** bucket, not a diagnosis: a fixed
  panic-marker set, the process exit, or the boot timeout — never a ranked cause.
