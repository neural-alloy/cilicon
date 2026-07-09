"""cilicon evidence: turn a green target into a *provable* one — an SBOM, a
signature over the artifact, and a SLSA provenance statement, bundled per target.

A cilicon check today says "it booted"; nothing downstream can verify that. This
adds the receipts: what's in the artifact (SBOM), that this exact artifact is the
one cilicon booted (digest + signature), and how it was built (provenance). The
bundle is the machine-readable thing a release system (or an auditor) consumes.

Pure functions over strings/dicts: no network, no syft/cosign here — those live
in `cilicon/tools.py` as lazy adapters, exactly the way `import modal` is lazy in
the runner. So the provenance/bundle shape is unit-testable with nothing installed.

The provenance is in-toto Statement v1 + SLSA provenance v1 — the same shape
carbonium's `ci/postbuild/postbuild.sh` emits, so a downstream `POST /v1/releases`
consumes a cilicon bundle unchanged.
"""
from __future__ import annotations

import base64
import hashlib

SCHEMA_VERSION = "cilicon-evidence/v1"
BUILD_TYPE = "cilicon/v1"


def sha256_hex(data: bytes) -> str:
    """Bare hex digest of some bytes (no `sha256:` prefix)."""
    return hashlib.sha256(data).hexdigest()


def b64(text: str) -> str:
    """Base64 an artifact (SBOM / provenance) for embedding in the bundle, matching
    how postbuild.sh inlines them so the bundle is one self-contained JSON."""
    return base64.b64encode(text.encode()).decode()


def ci_context(env: dict) -> dict:
    """The build context, read from a CI environment mapping (GitHub Actions here).
    Passed in rather than read from os.environ so this stays pure + testable."""
    return {
        "workflow": env.get("GITHUB_WORKFLOW", "local"),
        "run_id": env.get("GITHUB_RUN_ID", "0"),
        "repo": env.get("GITHUB_REPOSITORY", ""),
        "server": env.get("GITHUB_SERVER_URL", ""),
        "sha": env.get("GITHUB_SHA", ""),
    }


def provenance(subject_name: str, digest_hex: str, ci: dict) -> dict:
    """A SLSA-provenance-v1 in-toto Statement binding an artifact digest to how it
    was built. `digest_hex` is the bare sha256 (no prefix); `ci` is `ci_context`."""
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": subject_name, "digest": {"sha256": digest_hex}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildType": BUILD_TYPE,
            "builder": {"id": f"{ci.get('workflow', 'local')}/{ci.get('run_id', '0')}"},
            "invocation": {
                "configSource": {
                    "uri": f"{ci.get('server', '')}/{ci.get('repo', '')}".strip("/"),
                    "digest": {"sha1": ci.get("sha", "")},
                }
            },
        },
    }


def entry(
    target_id: str,
    artifact: str,
    digest_hex: str,
    *,
    sbom: str | None = None,
    signature: str | None = None,
    cert: str | None = None,
    prov: dict | None = None,
) -> dict:
    """One artifact's evidence: its digest, and whichever receipts were produced.
    A missing receipt is `None` (honest — we never fabricate one)."""
    return {
        "target": target_id,
        "artifact": artifact,
        "digest": f"sha256:{digest_hex}",
        "sbom": b64(sbom) if sbom is not None else None,
        "sbom_format": "cyclonedx-json" if sbom is not None else None,
        "signature": signature,
        "certificate": cert,
        "provenance": b64_json(prov) if prov is not None else None,
        "signed": bool(signature),
    }


def b64_json(obj: dict) -> str:
    import json

    return b64(json.dumps(obj, sort_keys=True))


def bundle(entries: list[dict], *, keyless: bool = True) -> dict:
    """The top-level evidence document over every green target's entry. `signed`
    counts how many artifacts actually carry a signature (never all, if a tool was
    missing) — the honest denominator, not a claim that everything is signed."""
    return {
        "tool": "cilicon",
        "schema_version": SCHEMA_VERSION,
        "signing": "keyless" if keyless else "keyed",
        "artifacts": len(entries),
        "signed": sum(1 for e in entries if e.get("signed")),
        "entries": entries,
    }
