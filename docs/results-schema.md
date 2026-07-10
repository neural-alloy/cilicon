# `cilicon run --json` — the results contract (`cilicon.results/v1`)

`cilicon run --json <path>` writes the one machine-readable artifact downstream
tooling consumes (the monorepo CI step, dashboards, the control-plane gate). This
is that contract. It is produced by `cilicon/report.py:to_json`; the tests in
`tests/test_report.py` pin the shape. **Parse this, never the pretty table.**

## Top level

```jsonc
{
  "schema": "cilicon.results/v1",   // bump on any breaking change below
  "tool": "cilicon",
  "passed": 1,                      // count of targets with ok == true
  "total": 2,
  "wall_seconds": 16.527,           // real time for the whole matrix (parallel)
  "targets": [ /* one object per target, in config order */ ]
}
```

`schema` is the only field a consumer must check first — a mismatch means the
rest may have moved. Everything else (`tool`/`passed`/`total`/`wall_seconds`/
`targets`) is stable across `v1`.

## Per-target object

```jsonc
{
  "id": "jetson-thor/linux-boot",
  "validate": "qemu_system_linux_aarch64",  // the tier that ran (presets.py)
  "fidelity": "FULL_SYSTEM_BOOT",            // WHAT RAN — see below
  "ok": true,                               // build AND validate (AND test/size) passed
  "seconds": 9.2,                           // summed phase time for this target
  "error": "",                              // non-empty = orchestration/infra failure
  "artifacts": [],                          // pulled-back file paths (best-effort)
  "build":         { /* step */ },
  "validate_step": { /* step */ },          // the proof-it-runs; console log lives here
  "test":          null,                    // step or null (no test phase)
  "test_cases":    null,                    // parsed Unity/TAP cases or null
  "size":          { /* step */ } | null,
  "sizes":         { "flash": 187, ... } | null
}
```

### `fidelity` — WHAT RAN (the honesty axis)

Derived **from the tier alone, never from user config** (`WEDGE_SPEC §0.2`). A
target cannot promote its own fidelity by setting `full_system: true` — that only
relaxes the clean-exit check, it does not conjure a kernel.

| value | meaning | tiers |
|---|---|---|
| `ELF_LOAD` | loaded a binary, reached `main`. No kernel. | `native`, `qemu_user`, `qemu_user_aarch64`, `qemu_user_riscv64`, `custom` |
| `FULL_SYSTEM_BOOT` | a real kernel/RTOS booted the artifact | `qemu_system`, `qemu_system_aarch64`, `qemu_system_riscv`, `qemu_system_linux_aarch64`, `qemu_esp32`, `renode`, `sim` |
| `REAL_HARDWARE` | ran on physical silicon | `real_gpu` |

`FULL_SYSTEM_BOOT` is the floor for the word **boot_tested**. An `ELF_LOAD` result
is never a boot; a `FULL_SYSTEM_BOOT` result is never silicon. Consumers gate on
`fidelity`, not on the tier name — e.g. the boot-test gate accepts only
`fidelity >= FULL_SYSTEM_BOOT`.

> This file is only the **fidelity** axis (what ran). The orthogonal **assurance**
> axis (who signed the result) is a control-plane concern and does not appear in
> a standalone cilicon run — cilicon signs nothing here.

### Step object (`build` / `validate_step` / `test` / `size`)

```jsonc
{
  "ok": true,
  "seconds": 8.079,
  "detail": "boots, reaches main ('perception: engine ok')",  // the judge's verdict
  "output_tail": "…last 12 lines of this phase's console, VERBATIM…"
}
```

`validate_step.output_tail` is the on-target console, printed **verbatim and
uninterpreted** — the last window before the terminating event (process exit,
timeout, or a panic marker). On a boot failure this is the whole product: the
unexplained panic. cilicon never names a cause here (`WEDGE_SPEC §5.1`).

## Example — a full-system Linux boot (`qemu_system_linux_aarch64`)

`validate_step.output_tail` from a real `cilicon run` of `board: jetson-thor`:

```
::cilicon-boot:: booting linux under qemu-system-aarch64
::cilicon-boot:: kernel booted to PID1 --
Linux (none) 6.1.0-50-arm64 #1 SMP Debian 6.1.176-1 (2026-07-02) aarch64 GNU/Linux
::cilicon-boot:: exec artifact as init --
perception: engine ok, 1 infer -> 2.000
::cilicon-boot:: cilicon-init-done
[    1.446717] reboot: Power down
```

The `uname`/`/proc/version` lines are authored by the booted kernel, and the
artifact printed its proof running as PID 1 — a genuine `FULL_SYSTEM_BOOT`, not an
ELF load. Contrast `qemu_user_aarch64`, where the same binary would only reach
`main` under `qemu-aarch64` with no kernel (`fidelity: ELF_LOAD`).

## Exit codes

`0` all passed · `1` a target failed · `2` `--target` matched nothing.
