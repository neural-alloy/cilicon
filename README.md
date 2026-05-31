# ⚡ cilicon

**CI for real hardware — build AND boot across every chip you ship to, in
parallel, owning zero hardware.**

Regular CI tells you your firmware *compiled*. cilicon tells you it **runs on the
chip** — it cross-builds each target **and boots it** in an emulator (or on a
real GPU), in parallel, and turns "compiles" + "runs on target" into **one green
PR check.**

cilicon is an **engine you drop into your existing CI as a GitHub Actions step** —
not a CI platform. GitHub decides *when* to run; cilicon answers *does this boot
on the silicon*. It's built on [Modal](https://modal.com): one spec fans every
target out to its own cloud container, each with its own toolchain, cross-builds
the artifact, and **proves it runs** by booting it co-located with the build.

```yaml
# .github/workflows/cilicon.yml — that's the whole integration
- uses: RyanRana/cilicon@v1
  env:
    MODAL_TOKEN_ID:     ${{ secrets.MODAL_TOKEN_ID }}
    MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
```

---

## How it fits

```
   GitHub push / PR                  ← GitHub orchestrates WHEN
        │
        ▼   uses: RyanRana/cilicon@v1
   ┌─────────────────────────────────────────────┐
   │  cilicon (a step in your CI)                  │
   │     reads cilicon.yml, fans out on Modal ─────┼──▶ ┌──────────────────────┐
   │     build + boot each target in parallel      │    │ Modal cloud sandboxes │
   │     judge + report one PR check  ◀────────────┼────│  each = its own image │
   └─────────────────────────────────────────────┘    └──────────────────────┘
        │
        ▼  cilicon answers DOES IT RUN ON THE CHIP
   ✅ / ❌  one status check, matrix table + sweep grid in the PR summary
```

You don't replace GitHub Actions — you add a check to it. (There's an optional
reference dashboard in [`cilicon/service/`](SERVICE.md), but it's **not** the
product and you don't need it.)

---

## Quickstart

### As a GitHub Action (the main way)

1. Add a `cilicon.yml` to your repo (see [docs/configuration.md](docs/configuration.md)).
2. Get a free [Modal](https://modal.com) account, run `modal token new`, and add
   `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` as repo secrets.
3. Add the workflow and make **`cilicon / build + boot`** a required check.

Full setup in **[docs/github-actions.md](docs/github-actions.md)**.

### Locally (the CLI)

```bash
pip install -e .          # the `cilicon` CLI + modal + pyyaml
modal token new           # (once) authenticate to Modal
cilicon targets           # list configured targets (matrix expanded)
cilicon presets           # list validation tiers
cilicon boards            # one-word toolchain bundles
cilicon gpus              # Modal GPU types for the real_gpu tier
cilicon run               # build + boot the whole matrix in parallel
cilicon run -t stm32      # just one target
cilicon run --junit out.xml --summary $GITHUB_STEP_SUMMARY --artifacts ./out
```

New here? Start with **[docs/getting-started.md](docs/getting-started.md)**.

---

## Example run

```
  cilicon · 4 target(s) · fanned across Modal cloud containers

  ┌──────────────────────────────┬────────────────┬────────────────────────────────────┐
  │ TARGET                       │ BUILD          │ ON-TARGET CHECK                    │
  ├──────────────────────────────┼────────────────┼────────────────────────────────────┤
  │ jetson-perception/linux-arm  │ ✓    0s        │ ✓ loads + runs ('perception: engi… │
  │ stm32h7/cortex-m             │ ✓    0s        │ ✓ boots, reaches main ('BOOT OK')  │
  │ esp32/freertos               │ ✓ 1m57s        │ ✓ boots, reaches main ('Hello fro… │
  │ pi5-loadtest/linux-arm       │ ✓    0s        │ ✗ crash (SIGSEGV), caught pre-fla… │
  └──────────────────────────────┴────────────────┴────────────────────────────────────┘
  3 / 4 passed · wall-clock ~2m50s
```

The `pi5-loadtest` target ships a real bug (a null-pointer write). On hardware
you'd flash it, watch it crash, and start over. cilicon catches it in emulation —
**before any metal is involved.** On a PR, that whole table (plus a fan-out sweep
grid and failing-target logs) lands in the check summary.

---

## Validation tiers

Tiers are **data, not code** (`cilicon/presets.py`) — adding a chip is one
`cilicon.yml` entry, and a tier cilicon has never heard of is `validate: custom`
+ `run:`. Full reference in [docs/tiers.md](docs/tiers.md).

| Tier | Proves it runs by… |
|------|--------------------|
| `qemu_user` / `_aarch64` / `_riscv64` | running the cross-built Linux ELF under qemu-user (loads, libs resolve, reaches `main`, no crash) |
| `qemu_system` / `_aarch64` / `_riscv` | booting bare-metal firmware in a full-system emulator, driving the virtual UART to a known string |
| `qemu_esp32` | booting an ESP-IDF/FreeRTOS image in qemu-system-xtensa |
| `renode` | booting in [Renode](https://renode.io) — models the board's **peripherals**, not just the CPU |
| `sim` | a cycle-accurate vendor sim / **ARM Fast Model (FVP)** you bring |
| `real_gpu` | **running on an actual Modal GPU** (real silicon, not emulation) — any type, e.g. `H100`, `A100-80GB:2` |
| `native` / `custom` | running on the host / a command you supply |

Plus: a `board:` catalog (one-word toolchain bundles), `matrix:` fan-out (sweep a
config grid), size budgets (`flash_max`/`ram_max`), an on-target `test:` phase,
and telemetry — see the docs.

---

## What a green check actually means

cilicon is built to tell the truth about its own checkmark. A green check proves
your code, in order of strength:

1. **builds** — cross-compiles and links cleanly (catches missing symbols, bad
   linker scripts, wrong ABI);
2. **fits** — `text+data` ≤ flash, `data+bss` ≤ RAM (if you set a size budget);
3. **runs far enough** — loads / reaches `main` / boots in an emulator without
   crashing, and prints the string you told it to `expect`.

It is **not silicon certification.** QEMU and Renode *model* your chip — they are
not your chip. Peripheral quirks, analog behavior, and real-time timing aren't
covered, and cilicon never pretends otherwise. It catches the cheap, common
failures that would otherwise cost you a flash-boot-fail cycle. The one tier
that's real silicon is `real_gpu`, which runs on an actual Modal GPU.

---

## Docs

| | |
|---|---|
| [Getting started](docs/getting-started.md) | install, auth Modal, first run |
| [GitHub Actions](docs/github-actions.md) | **the primary usage** — wire it into CI, gate PRs |
| [Configuration](docs/configuration.md) | the complete `cilicon.yml` reference |
| [Tiers](docs/tiers.md) | every validation tier, board, and GPU |
| [Telemetry](docs/telemetry.md) | structured run/phase observability |

---

## Layout

```
cilicon/
  cilicon/config.py    parse cilicon.yml → Target specs (matrix, boards, aliases)
  cilicon/presets.py   validation tiers + board + GPU catalogs, as DATA
  cilicon/runner.py    Modal orchestration: per-target image, build, size, boot, judge
  cilicon/sizes.py     parse `size` → flash/RAM, judge against a budget
  cilicon/report.py    JSON / JUnit / Markdown reports + the fan-out sweep grid
  cilicon/telemetry.py structured run/target/phase events → JSONL / stdout / HTTP
  cilicon/cli.py       cilicon run / targets / presets / boards / gpus
  cilicon/service/     OPTIONAL reference dashboard (not the product — see SERVICE.md)
  action.yml           the GitHub Action; .github/workflows/cilicon.yml uses it
  examples/            cilicon-advanced.yml — matrix, custom, real_gpu, renode, sim
  tests/               pytest over every Modal-free path (config, judge, size, report, telemetry)
  cilicon.yml          the demo target matrix
```

---

## License

MIT © Ryan Rana — see [LICENSE](LICENSE). Contributions welcome:
[CONTRIBUTING.md](CONTRIBUTING.md).
