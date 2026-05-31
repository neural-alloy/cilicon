# cilicon as a GitHub Action

This is the primary way to use cilicon: as **a step inside your existing CI** that builds and boots your code across every chip in `cilicon.yml`, in parallel, on [Modal](https://modal.com), and reports the result as a normal PR check.

> cilicon is **not** a CI platform and does not replace GitHub Actions. It runs *inside* your GitHub Actions workflow and publishes a status check like any other step. Each target is its own world — its own toolchain image and its own proof-it-runs — fanned out in parallel.

> **What the check proves.** A green `cilicon` check means the code **built, fit, and ran far enough in an emulator/simulator to print an expected string**. QEMU and Renode model the chip; they are not the chip. The `real_gpu` tier is the exception — it runs on an actual Modal GPU. cilicon never overclaims; see [tiers.md](tiers.md).

## Setup in three steps

### 1. Add a `cilicon.yml` to your repo

Define one target per chip you ship to. See [configuration.md](configuration.md) for the full field reference and [getting-started.md](getting-started.md) to try it locally first. Minimal example:

```yaml
# cilicon.yml
targets:
  - id: firmware/cortex-m
    board: cortex-m                 # one word = debian + arm-none-eabi + qemu-system-arm
    build: >
      arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostartfiles -nostdlib
      -ffreestanding -T src/cortex-m.ld src/firmware.c -o build/firmware.elf
    artifact: build/firmware.elf
    expect: "BOOT OK"               # the on-target proof string
```

### 2. Add the workflow

Create `.github/workflows/cilicon.yml` in your repo:

```yaml
name: cilicon

on:
  push:
    branches: [main]
  pull_request:

jobs:
  build-and-boot:
    name: build + boot          # → check shows up as "cilicon / build + boot"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: cilicon
        uses: RyanRana/cilicon@v1
        with:
          config: cilicon.yml
        env:
          MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
          MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
```

The Action installs cilicon from its own checkout (not your repo), then runs `cilicon run` with `--junit`, `--summary "$GITHUB_STEP_SUMMARY"`, and `--artifacts`. It uploads the report, artifacts, and any telemetry file via `actions/upload-artifact`.

### 3. Set the two Modal secrets

cilicon needs Modal credentials to fan targets out to the cloud. Run `modal token new` locally (it prints a token id and secret), then add them as **repository secrets** in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `MODAL_TOKEN_ID` | the token id from `modal token new` |
| `MODAL_TOKEN_SECRET` | the token secret from `modal token new` |

## Gate merges on the check

To require a green build-and-boot before any merge:

1. Go to **Settings → Branches → Branch protection rules** for your default branch.
2. Enable **Require status checks to pass before merging**.
3. Add **`cilicon / build + boot`** as a required check (the name comes from the workflow's `job.name`).

Now a PR can't merge unless every target builds, fits, and boots.

## Action inputs

From [`action.yml`](../action.yml):

| Input | Default | Meaning |
|---|---|---|
| `config` | `cilicon.yml` | Path to `cilicon.yml`, relative to your repo. |
| `target` | `""` | Run a single target by id; empty runs the whole matrix. |
| `report` | `cilicon-results.xml` | Path the JUnit report is written to. |
| `artifacts` | `cilicon-artifacts` | Directory built artifacts are pulled into. |
| `telemetry` | `""` | Optional path to append JSONL run/target/phase events ([telemetry.md](telemetry.md)). |

The two Modal tokens are passed as `env:`, not inputs.

## What shows up on the PR

- **A status check** — the JUnit report (one `<testcase>` per target) is published as a check run. With the dogfood workflow this repo uses [`mikepenz/action-junit-report`](https://github.com/mikepenz/action-junit-report); you can also point your own JUnit consumer at the `report` file.
- **A Markdown summary** — written to `$GITHUB_STEP_SUMMARY`, so the job summary shows a table: each target's pass/fail, build time, on-target check detail, and flash size.
- **A fan-out sweep grid** — when a target uses a `matrix:`, the summary renders a ✅/❌ heatmap of the sweep (a row for one axis, a table for two axes), under a "Fan-out sweep" heading. Each cell is its own cloud container.
- **A failure-logs section** — a collapsible `<details>` block with the tail of any failing step's output, so a reviewer sees exactly how far the code got before it failed.
- **Uploaded artifacts** — the `cilicon-results` artifact bundle contains the JUnit report, the pulled binaries (your `artifacts:` globs), and the telemetry file if configured.

## This repo dogfoods its own action

[`.github/workflows/cilicon.yml`](../.github/workflows/cilicon.yml) in this repo uses `uses: ./` (the action's local checkout) instead of `uses: RyanRana/cilicon@v1`. In your repo, use the pinned `RyanRana/cilicon@v1` form shown above.

## See also

- [getting-started.md](getting-started.md) — run the same engine locally.
- [configuration.md](configuration.md) — every `cilicon.yml` field.
- [tiers.md](tiers.md) — validation tiers, boards, and GPUs.
- [telemetry.md](telemetry.md) — structured run observability.
