# ⚡ cilicon

**CI for real hardware — build AND boot your firmware across every chip you ship
to, in parallel, owning zero hardware.**

Regular CI tells you your code *compiled*. cilicon tells you it **runs on the
chip** — it cross-builds each target **and boots it** in an emulator (or on a
real GPU), in parallel on [Modal](https://modal.com), and reports **one PR
check.** It's a step you add to your existing CI, not a new CI.

## Use it as a GitHub Action

```yaml
# .github/workflows/cilicon.yml
- uses: RyanRana/cilicon@v1
  env:
    MODAL_TOKEN_ID:     ${{ secrets.MODAL_TOKEN_ID }}
    MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
```

Add a `cilicon.yml`, add the two Modal secrets (`modal token new`), done. Full
setup → **[docs/github-actions.md](docs/github-actions.md)**.

## Or run it locally

```bash
pip install cilicon    # the cilicon CLI
modal token new        # once
cilicon run            # build + boot the whole matrix in parallel
```

## Prove it, don't just boot it

A green cilicon check says "it booted." Turn it into a check something *downstream*
can trust — signed, security-gated, and root-caused:

```yaml
- uses: RyanRana/cilicon@v1
  with:
    evidence: cilicon-evidence.json   # SBOM + signature + SLSA provenance per green target
  permissions:
    id-token: write                   # keyless cosign (Fulcio + Rekor)
```

- **Evidence bundle** — for every target that built *and* booted, cilicon emits a
  CycloneDX SBOM (syft), a keyless cosign signature over the artifact, and a SLSA
  provenance statement, bundled as one JSON. A red target is never signed.
- **Vuln gate** — set `vuln_gate: critical` (or `high` / `kev`) on a target and an
  unwaived Critical/KEV finding fails the check, right beside the size and boot
  gates. Time-boxed `waivers:` are reported but don't gate.
- **Triage** — a 40-board sweep that fails the same way is reported as *one* root
  cause, not forty; `--triage-history` marks a failure "known since <sha>" vs new.

Locally: `cilicon run --evidence out.json` (needs `syft`, `grype`, `cosign` on PATH;
each degrades gracefully if absent). Full reference in
**[Configuration](docs/configuration.md)**.

## Docs

- **[Getting started](docs/getting-started.md)** — install, auth, first run
- **[GitHub Actions](docs/github-actions.md)** — wire it into CI, gate PRs
- **[Configuration](docs/configuration.md)** — the `cilicon.yml` reference
- **[Tiers](docs/tiers.md)** — the validation tiers (QEMU, Renode, GPU, …)
- **[Architecture](docs/architecture.md)** — how it works internally; start here to contribute

MIT © Ryan Rana
