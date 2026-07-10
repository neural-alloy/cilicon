# Getting started

**cilicon is CI for real hardware** — it builds *and* boots your code across every chip you ship to, in parallel, owning zero hardware. Each target gets its own toolchain image and its own way of proving the artifact actually runs, fanned out across [Modal](https://modal.com) cloud containers.

cilicon is an engine you normally run as a GitHub Actions step (see [github-actions.md](github-actions.md)). This page covers the local CLI, which is the same engine you can run from your laptop.

> **What a green check means.** A pass proves the code **builds, fits, and runs far enough in an emulator/simulator to print an expected string**. It is not silicon certification — QEMU and Renode *model* the chip, they are not the chip. The one exception is the `real_gpu` tier, which runs on an actual Modal GPU (real silicon). See [tiers.md](tiers.md) for the honesty matrix.

## Install

```bash
pip install -e .          # installs the `cilicon` CLI (+ modal + pyyaml)
```

## Authenticate Modal

cilicon runs every build and boot in a Modal sandbox, so you need a (free) Modal account and a local token.

```bash
modal token new           # opens a browser, writes a local token
```

This is a one-time step. In CI you instead set `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` as secrets (see [github-actions.md](github-actions.md)).

## The CLI commands

| Command | What it does |
|---|---|
| `cilicon targets` | List the targets in `cilicon.yml`, fully matrix-expanded, with what each builds, validates, and proves. |
| `cilicon presets` | List every built-in validation tier (see [tiers.md](tiers.md)). |
| `cilicon boards` | List the one-word board bundles (`base + apt + tier + machine`). |
| `cilicon gpus` | List the Modal GPU types usable by the `real_gpu` tier. |
| `cilicon run` | Build + validate the whole matrix in parallel. |

The global flag `-c` / `--config` sets the config path (default `cilicon.yml`) and goes **before** the subcommand:

```bash
cilicon -c examples/cilicon-advanced.yml targets
```

A bare `cilicon` with no subcommand is equivalent to `cilicon run`.

## A first run

This repo ships a canonical 4-target demo in [`cilicon.yml`](../cilicon.yml): an ARM-Linux userspace binary, a bare-metal Cortex-M firmware (with a flash/RAM size budget), an ESP32/FreeRTOS image, and a second ARM-Linux target that ships a real bug.

```bash
cilicon run
```

You'll see a live table as each target builds and boots in its own container:

```
  cilicon · 4 target(s) · fanned across Modal cloud containers

  ┌──────────────────────────────┬────────────────┬────────────────────────────────────┐
  │ TARGET                       │ BUILD          │ ON-TARGET CHECK                    │
  ├──────────────────────────────┼────────────────┼────────────────────────────────────┤
  │ jetson-perception/linux-arm  │ ✓    0s        │ ✓ loads + runs ('perception: engi… │
  │ stm32h7/cortex-m             │ ✓    0s        │ ✓ boots, reaches main ('BOOT OK… │
  │ esp32/freertos               │ ✓ 1m57s        │ ✓ boots, reaches main ('Hello fro… │
  │ pi5-loadtest/linux-arm       │ ✓    0s        │ ✗ crash (SIGSEGV), caught pre-fla… │
  └──────────────────────────────┴────────────────┴────────────────────────────────────┘
  3 / 4 passed · wall-clock 2m50s · vs ~4m20s sequential
```

### Reading the table

- **TARGET** — the target `id` from `cilicon.yml`.
- **BUILD** — `✓` plus the build time, or `✗` plus the build failure detail.
- **ON-TARGET CHECK** — the proof-it-runs result. On success it shows the validate detail and appends `· tests ✓` if a `test:` phase ran and `· <N> flash` if a size was measured. On failure it shows which phase failed (build / size / validate / test) and why.
- The footer line shows `passed / total`, the **wall-clock** time, and — when the parallel run beat running each target back-to-back — `vs ~<N> sequential`.
- Failing targets print an expanded block below the table with the failing step's detail and the last few lines of its output (e.g. `loadtest: starting up` before a segfault).

The process exits `0` only if every target passed, `1` if any failed, and `2` if `--target` matched nothing.

## `cilicon run` flags

| Flag | Meaning |
|---|---|
| `--target` / `-t <id>` | Run a single target by id (matches exact id, slug, then substring). |
| `--json <path>` | Write a JSON report (cilicon's own shape: every phase, timing, output tail, artifacts). |
| `--junit <path>` | Write a JUnit XML report — the format every CI dashboard already understands. |
| `--summary <path>` | Write a GitHub-flavoured Markdown summary (point this at `$GITHUB_STEP_SUMMARY` in CI). |
| `--artifacts <dir>` | Pull built artifacts (per-target `artifacts:` globs) back into this directory. |
| `--telemetry <path>` | Append JSONL run/target/phase events to this path (see [telemetry.md](telemetry.md)). |
| `--telemetry-stdout` | Also print telemetry events to stdout. |

Example — produce a CI report and pull binaries back:

```bash
cilicon run --junit out.xml --artifacts ./out
cilicon run -t stm32                       # just one target
```

## Publishing (maintainers)

cilicon ships to PyPI as `cilicon` via **Trusted Publishing** — GitHub Actions
authenticates to PyPI over OIDC, so no API token is ever stored. One-time setup:

1. On [PyPI](https://pypi.org), add a **pending trusted publisher** for the project
   `cilicon`: owner `neural-alloy`, repo `cilicon`, workflow `publish.yml`,
   environment `pypi`.
2. In the GitHub repo, create an Environment named `pypi`.
3. Publish a **GitHub Release** (tag `v0.1.0`) — `.github/workflows/publish.yml`
   builds the sdist + wheel and publishes them. Done.

Manual fallback (token-based): `python -m build && python -m twine upload dist/*`.

## Next steps

- [github-actions.md](github-actions.md) — the primary way to use cilicon: a step in your existing CI that gates merges.
- [configuration.md](configuration.md) — the complete `cilicon.yml` field reference.
- [tiers.md](tiers.md) — every validation tier, board, and GPU, with honesty about fidelity.
- [telemetry.md](telemetry.md) — structured observability for every run.
- [architecture.md](architecture.md) — how cilicon works internally, module by module — read this before contributing.
- The root [README.md](../README.md) for the project overview.
