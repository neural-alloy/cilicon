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

## Docs

- **[Getting started](docs/getting-started.md)** — install, auth, first run
- **[GitHub Actions](docs/github-actions.md)** — wire it into CI, gate PRs
- **[Configuration](docs/configuration.md)** — the `cilicon.yml` reference
- **[Tiers](docs/tiers.md)** — the validation tiers (QEMU, Renode, GPU, …)
- **[Architecture](docs/architecture.md)** — how it works internally; start here to contribute

MIT © Ryan Rana
