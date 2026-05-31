# `cilicon.yml` reference

A `cilicon.yml` is a list of **targets**. A target is intentionally small and uniform across wildly different silicon: it carries its own toolchain (a `base` image + `apt`, or a `dockerfile`) and its own way of proving the artifact actually runs (a validation tier — see [tiers.md](tiers.md)).

```yaml
targets:
  - id: stm32h7/cortex-m
    base: debian:bookworm-slim
    apt: [gcc-arm-none-eabi, qemu-system-arm]
    build: arm-none-eabi-gcc ... -o build/firmware.elf
    validate: qemu_system
    machine: lm3s6965evb
    artifact: build/firmware.elf
    expect: "BOOT OK"
```

Three layers of dynamism turn "the tiers cilicon ships" into "any code, any hardware": [`board:`](#board-aliases) aliases, the [`matrix:`](#the-matrix-block) block, and `validate: custom` + `run:`. The whole project directory is mounted into every sandbox, so any path builds — not just files under `src/`.

Every target needs at least an `id` and a `build`. Unknown fields are rejected, and so is an unknown `validate` tier (unless it's `custom`).

## Field reference

### Identity

| Field | Type | Meaning |
|---|---|---|
| `id` | string (required) | Unique target id. Its `slug` (lowercased, non-alphanumerics → `-`) is used for matching and reports. |
| `build` | string (required) | Shell command that produces the artifact, run in `/work` inside the sandbox. |
| `validate` | string | A preset tier name (default `native`) or `custom`. See [tiers.md](tiers.md). |

### Toolchain (its own world)

| Field | Type | Meaning |
|---|---|---|
| `base` | string | Base Docker image (default `debian:bookworm-slim`). |
| `apt` | list of strings | apt packages to install on top of `base`. |
| `dockerfile` | string | Path to a custom Dockerfile; overrides `base` + `apt`. |
| `board` | string | One-word alias from the [board catalog](tiers.md#board-catalog) that fills `base`/`apt`/`validate`/`machine`/etc. as defaults. |

### Proof-it-runs

| Field | Type | Meaning |
|---|---|---|
| `run` | string | Custom validate command — **required** when `validate: custom`. |
| `artifact` | string | Path (in `/work`) to the built binary the tier runs. |
| `machine` | string | `qemu-system` machine (default `lm3s6965evb`; system tiers default `virt`). |
| `qemu_bin` | string | `qemu-user` launcher for this arch (default `qemu-arm`; set per tier). Old name `qemu_user_bin` is accepted as an alias. |
| `app_dir` | string | ESP-IDF project dir (rel to `/work`) for the `qemu_esp32` tier. |
| `renode_script` | string | `.resc` path (in `/work`) — **required** for the `renode` tier. |
| `renode_uart_log` | string | Where the `.resc` tees the modeled UART (default `/tmp/cilicon_uart.log`). |
| `sim_bin` | string | The FVP / vendor simulator binary — **required** for the `sim` tier. |
| `sim_args` | string | Extra flags passed before the artifact, `sim` tier. |
| `gpu` | string | Modal GPU type for `real_gpu` / `custom`, e.g. `T4` or `H100:2`. |
| `full_system` | bool | Override the tier's "don't require a clean emulator exit" behaviour. |

### Assertions — how cilicon judges "it actually ran"

| Field | Type | Meaning |
|---|---|---|
| `expect` | list of strings | **All** substrings must appear in the output. A scalar is accepted and wrapped into a one-element list. |
| `expect_regex` | string | A regex that must match the output. |
| `expect_exit` | int | Require this exact exit code. |

### Size budget — does the artifact fit the silicon?

| Field | Type | Meaning |
|---|---|---|
| `size_tool` | string | e.g. `arm-none-eabi-size`; cilicon runs it on the artifact. |
| `flash_max` | int or human size | `text + data` must fit. Accepts bytes or `256K` / `64k` / `1M` / `2g` / `"12 KB"`. |
| `ram_max` | int or human size | `data + bss` must fit. Same human-size parsing. |

Size checks parse GNU `size` in both Berkeley and SysV formats. A budget of "unset" means *report only, don't gate*. Output that can't be parsed is treated leniently (passes with "size output not understood"). See [tiers.md](tiers.md) and the source `cilicon/sizes.py` for the section-to-flash/RAM mapping.

### Test phase — run a suite ON the target after it boots

| Field | Type | Meaning |
|---|---|---|
| `test` | string | Command (in `/work`) whose exit `0` == pass — a second on-target check beyond the boot smoke-proof. |
| `test_expect` | list of strings | Strings the test output must contain (scalar accepted, wrapped into a list). |

### Plumbing

| Field | Type | Meaning |
|---|---|---|
| `env` | mapping | Environment variables exported in the sandbox. |
| `secrets` | list of strings | Modal secret names to mount into the sandbox (e.g. a vendor license). |
| `artifacts` | list of strings | Globs to pull back out (with `--artifacts` / the Action's `artifacts` input). |
| `boot_timeout` | int | Seconds to let an emulator boot (default `60`). |
| `timeout` | int | Seconds for the whole target (default `900`). |

> Note: `artifacts` (a list, on a target) controls *which built files to pull back*, while the `--artifacts <dir>` CLI flag / Action input controls *where they land*. Different things.

## Human sizes

`flash_max` and `ram_max` accept a raw integer (bytes) or a human string. Suffixes are binary (`K = 1024`, `M = 1024²`, `G = 1024³`), an optional trailing `b`/`B` is ignored, and case is flexible:

```yaml
flash_max: 256K        # 262144 bytes
ram_max: 64k           # 65536 bytes
flash_max: 1M
flash_max: "12 KB"
```

A YAML boolean is explicitly rejected as a size (guards against `true` being an int subclass).

## `board:` aliases

A board is a one-word alias for a `(base + apt + validate + machine/qemu_bin/gpu)` bundle. Its values are applied **as defaults** — anything you set explicitly on the target wins. An unknown board name is an error.

```yaml
- id: gateway/arm-linux
  board: arm-linux        # = debian + gcc-arm-linux-gnueabihf + qemu-user + qemu-arm
  build: arm-linux-gnueabihf-gcc -static -O2 src/app.c -o build/app
  artifact: build/app
  expect: "engine ok"
```

Run `cilicon boards` to list them, or see the [board catalog in tiers.md](tiers.md#board-catalog).

## The `matrix:` block

A `matrix:` expands **one** target entry into the cartesian product of its values — each combination becomes its own independent target and its own cloud container.

```yaml
- id: node-{arch}
  matrix:
    arch: [arm, aarch64, riscv64]
  base: debian:bookworm-slim
  apt: ["gcc-{arch}-linux-gnu", qemu-user]
  build: "{arch}-linux-gnu-gcc -static -O2 src/perception.c -o build/app-{arch}"
  validate: custom
  run: "stdbuf -oL qemu-{arch} ./build/app-{arch}"
  artifact: build/app-{arch}
  expect: "perception: engine ok"
```

This produces `node-arm`, `node-aarch64`, `node-riscv64`.

### Substitution rules

- Only literal `{var}` tokens for matrix variables are replaced — substitution is applied to **every** field (strings, list items, and dict values), not just `id`.
- It is a plain token replace, **not** `str.format`, so shell braces survive untouched: `${...}` parameter expansion and `$(( ... ))` arithmetic in your `build`/`run` commands are left alone.
- Each expanded target remembers its sweep cell, which is what lets the PR summary draw a ✅/❌ [sweep grid](github-actions.md#what-shows-up-on-the-pr). A 1-axis matrix renders a row, a 2-axis matrix a table; 3+ axes fall back to the flat target table.
- Duplicate target ids (after expansion) are an error, so keep `{var}` in the `id`.

A matrix value can also be a scalar (treated as a one-element list).

## A fuller example

See [`examples/cilicon-advanced.yml`](../examples/cilicon-advanced.yml) for boards, a matrix sweep, a `custom` tier, a `real_gpu` target, a size budget, a `test:` phase, Renode, and commented-out `sim` / `env` + `secrets` examples in one file. The canonical 4-target demo is [`cilicon.yml`](../cilicon.yml).

## See also

- [tiers.md](tiers.md) — what each `validate` tier does and which fields it uses.
- [getting-started.md](getting-started.md) — running locally; reading the output table.
- [github-actions.md](github-actions.md) — using cilicon as a PR check.
