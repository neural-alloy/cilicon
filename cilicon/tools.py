"""cilicon evidence tools: the thin, impure adapters that shell out to syft (SBOM),
grype (vuln scan), and cosign (sign) on a pulled artifact.

Everything here touches the filesystem and external binaries, so — like `modal` in
the runner — it stays OUT of the pure logic in `evidence.py` / `vuln.py`. Each
adapter degrades gracefully: a missing tool returns None with a warning, never a
crash, so `cilicon run` without cosign installed still runs (just unsigned).

Signing defaults to cosign **keyless** (Fulcio/Rekor, OIDC identity) — the right
default for OSS/GitHub CI; pass a key for the air-gapped/HSM path.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def digest_file(path: str) -> str:
    """Bare hex sha256 of a file (streamed, so a large image doesn't sit in RAM)."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sbom(artifact: str, warn=print) -> str | None:
    """CycloneDX SBOM for an artifact via syft. Returns the JSON string, or None."""
    if not have("syft"):
        warn("syft not installed — no SBOM (https://github.com/anchore/syft)")
        return None
    r = _run(["syft", "scan", f"file:{artifact}", "-o", "cyclonedx-json", "-q"])
    if r.returncode != 0:
        warn(f"syft failed on {artifact}: {r.stderr.strip()[:200]}")
        return None
    return r.stdout


def scan(artifact: str, warn=print) -> dict | None:
    """grype vulnerability scan of an artifact. Returns the parsed JSON, or None."""
    if not have("grype"):
        warn("grype not installed — no vuln scan (https://github.com/anchore/grype)")
        return None
    r = _run(["grype", f"file:{artifact}", "-o", "json", "-q"])
    if r.returncode != 0:
        warn(f"grype failed on {artifact}: {r.stderr.strip()[:200]}")
        return None
    import json

    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        warn(f"grype output for {artifact} was not JSON")
        return None


def sign_blob(artifact: str, key: str | None = None, warn=print) -> tuple[str, str] | None:
    """cosign sign-blob over an artifact. Keyless (Fulcio/Rekor) unless `key` is
    given. Returns (signature, certificate) — cert is "" for a keyed signature."""
    if not have("cosign"):
        warn("cosign not installed — artifact left unsigned (https://github.com/sigstore/cosign)")
        return None
    env = dict(os.environ)
    if key:
        cmd = ["cosign", "sign-blob", "--yes", "--key", key, "--output-signature", "-", artifact]
        cert_flag = None
    else:
        env["COSIGN_EXPERIMENTAL"] = "1"
        cert_flag = "/tmp/cilicon_cosign.cert"
        cmd = ["cosign", "sign-blob", "--yes", "--output-signature", "-",
               "--output-certificate", cert_flag, artifact]
    r = _run(cmd, env=env)
    if r.returncode != 0:
        warn(f"cosign failed on {artifact}: {r.stderr.strip()[:200]}")
        return None
    sig = r.stdout.strip()
    cert = ""
    if cert_flag and os.path.exists(cert_flag):
        with open(cert_flag) as f:
            cert = f.read().strip()
        try:
            os.remove(cert_flag)
        except OSError:
            pass
    return sig, cert


def kev_ids(path: str | None = None, warn=print) -> set | None:
    """The CISA Known-Exploited-Vulnerabilities id set, from a local catalog JSON
    (the KEV feed, downloaded by CI). None when unavailable — the vuln gate then
    honestly degrades a 'kev' policy to report-only rather than passing silently."""
    if not path or not os.path.exists(path):
        return None
    import json

    try:
        data = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        warn(f"could not read KEV catalog {path}")
        return None
    vulns = data.get("vulnerabilities", data) if isinstance(data, dict) else data
    ids = {v.get("cveID") for v in vulns if isinstance(v, dict) and v.get("cveID")}
    return ids or None
