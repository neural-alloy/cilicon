# Validation tiers, boards, and GPUs

A **validation tier** is how a target proves its artifact actually runs. Tiers are **data, not code** — each is a row in `cilicon/presets.py`, so adding a chip is one YAML entry, never a cilicon code change. A tier cilicon has never heard of is `validate: custom` + `run:`.

Run `cilicon presets` to list every tier, `cilicon boards` for the one-word bundles, and `cilicon gpus` for the GPU lineup.

> **Honesty about fidelity.** Every tier except `real_gpu` runs in an emulator or simulator. A green check proves the code **builds, fits, and runs far enough to print an expected string** — it is **not** silicon certification. QEMU and Renode *model* the chip; they are not the chip, and don't model every peripheral or timing detail. The `real_gpu` tier is the one exception: it runs the artifact on a physical Modal GPU. cilicon never pretends emulation is silicon.

## How a tier judges a run

A tier renders to a shell command in the sandbox. Then the assertions decide pass/fail: every string in `expect:` must appear, `expect_regex:` must match, and `expect_exit:` must equal the exit code (see [configuration.md](configuration.md#assertions--how-cilicon-judges-it-actually-ran)). A tier marked **full-system** boots firmware in a way that may never cleanly exit, so for those the **expect string is the proof** and a clean exit isn't required (`timeout ... || true` guards the never-exits case). `full_system:` on a target overrides this.

## The tiers

### `native`
Runs the artifact directly on the host (`./{artifact}`). For host-arch binaries.

### `qemu_user`
ARM Linux ELF under `qemu-arm`: the ELF loads, shared libs resolve, it reaches `main`, and runs to completion (crashes like SIGSEGV/SIGILL are caught). Defaults `qemu_bin: qemu-arm`. qemu-user tiers line-buffer output so partial output survives a crash — it proves how far the code got. **Fields:** `artifact`, `qemu_bin`.

### `qemu_user_aarch64`
ARM64 Linux ELF under `qemu-aarch64`. Defaults `qemu_bin: qemu-aarch64`.

### `qemu_user_riscv64`
RISC-V 64 Linux ELF under `qemu-riscv64`. Defaults `qemu_bin: qemu-riscv64`.

### `qemu_system`
Bare-metal ARM (Cortex-M) firmware in `qemu-system-arm`, `-nographic -semihosting -kernel ./{artifact}`. The firmware resets, reaches `main`, and drives the virtual UART (semihosting) to a known string. Full-system; defaults `machine: lm3s6965evb`. **Fields:** `artifact`, `machine`, `boot_timeout`.

### `qemu_system_aarch64`
Bare-metal ARM64 firmware in `qemu-system-aarch64`. Full-system; defaults `machine: virt`.

### `qemu_system_riscv`
Bare-metal RISC-V firmware in `qemu-system-riscv64 -bios none`. Full-system; defaults `machine: virt`. **Fields:** `artifact`, `machine`, `boot_timeout`.

### `qemu_esp32`
ESP-IDF image in `qemu-system-xtensa` via `idf.py qemu`: the bootloader runs, the app starts, FreeRTOS schedules, and the UART boot log hits a known string. Full-system. Uses `cd /work/{app_dir} && . $IDF_PATH/export.sh`, so set **`app_dir`** to the ESP-IDF project dir. Pair with `base: espressif/idf:release-v5.3`. **Fields:** `app_dir`, `boot_timeout`.

### `renode`
Firmware in [Renode](https://renode.io) — **peripheral-accurate** where QEMU isn't: it models the real board's peripherals, not just the CPU. Headless flow: the `.resc` loads a board platform, loads the ELF, runs for a bounded time, `quit`s, and tees the modeled UART to a file; cilicon then cats that file so the firmware's `expect:` string (printed over a modeled peripheral) lands in stdout. Full-system; defaults `renode_uart_log: /tmp/cilicon_uart.log`. **Requires `renode_script`** (a `.resc` path). Use the `antmicro/renode` image (it ships renode + platform descriptions). **Fields:** `renode_script`, `renode_uart_log`, `boot_timeout`.

```yaml
- id: sensor/stm32f4-renode
  base: antmicro/renode:latest
  apt: [gcc-arm-none-eabi]
  build: arm-none-eabi-gcc -mcpu=cortex-m4 ... src/firmware.c -o build/firmware.elf
  validate: renode
  renode_script: examples/renode/stm32f4_discovery.resc
  artifact: build/firmware.elf
  expect: "BOOT OK"
  boot_timeout: 90
```

See [`examples/renode/stm32f4_discovery.resc`](../examples/renode/stm32f4_discovery.resc) for a real, commented headless script that boots a modeled STM32F4 Discovery and tees USART2 to the log cilicon reads back.

### `sim`
Cycle-accurate vendor simulator / ARM Fast Models (FVP) — far higher fidelity than QEMU (models actual core timing + peripherals). The simulator is a Linux binary you bring in your image (via `base`/`apt` or a `dockerfile`). Full-system; runs `{sim_bin} {sim_args} ./{artifact}`. **Requires `sim_bin`.** **Fields:** `sim_bin`, `sim_args`, `artifact`, `boot_timeout`.

```yaml
- id: dsp/cortex-m4-fvp
  dockerfile: docker/fvp.Dockerfile     # an image with the FVP installed
  build: arm-none-eabi-gcc ... -o build/app.axf
  validate: sim
  sim_bin: FVP_MPS2_Cortex-M4
  sim_args: "-C mps2.platform_type=0 --stat"
  artifact: build/app.axf
  expect: "BOOT OK"
```

Higher fidelity than QEMU, but still a model — not the physical part.

### `real_gpu`
**Not emulation** — runs the artifact on a real GPU in Modal. Defaults `gpu: T4`. Set `gpu:` to any Modal type (see [GPU catalog](#gpu-catalog)), optionally with a count like `H100:2`. **Fields:** `artifact`, `gpu`.

```yaml
- id: infer/cuda
  base: nvidia/cuda:12.4.1-devel-ubuntu22.04
  build: nvcc -O2 src/gpu/infer.cu -o build/infer
  validate: real_gpu
  gpu: "H100"
  artifact: build/infer
  expect: "infer: gpu ok"
```

### `custom`
The escape hatch: you supply **`run:`** — any tier cilicon has never heard of, in pure YAML, with no cilicon code change. `validate: custom` without a non-empty `run:` is an error. A `gpu:` can be requested here too.

```yaml
- id: fpga/vivado
  dockerfile: docker/vivado.Dockerfile
  build: vivado -mode batch -source build.tcl
  validate: custom
  run: "grep -q 'timing met' vivado.log"
  expect_exit: 0
```

## Boards (define your own)

A board is a one-word alias for a bundle of target fields, applied as defaults
(anything you set explicitly on the target wins). **Define your own** under a
top-level `boards:` in `cilicon.yml` — a board can set *any* target field, so
shape it to whatever you ship:

```yaml
boards:
  my-mcu:                      # then use:  board: my-mcu
    base: my-registry/toolchain:latest
    apt: [gcc-arm-none-eabi, qemu-system-arm]
    validate: qemu_system
    machine: mps2-an385
```

Your boards extend — and can override by name — these built-in starters
(`cilicon boards` lists everything, including yours):

| Board | validate | base |
|---|---|---|
| `arm-linux` | `qemu_user` | `debian:bookworm-slim` |
| `aarch64-linux` | `qemu_user_aarch64` | `debian:bookworm-slim` |
| `cortex-m` | `qemu_system` (machine `lm3s6965evb`) | `debian:bookworm-slim` |
| `esp32` | `qemu_esp32` | `espressif/idf:release-v5.3` |
| `cuda` | `real_gpu` (gpu `T4`) | `nvidia/cuda:12.4.1-devel-ubuntu22.04` |

## GPU catalog

The `gpu:` field (used by `real_gpu` and `custom`) accepts any Modal GPU type. Run `cilicon gpus` to list them. Smallest → biggest:

```
T4  L4  A10G  A100  A100-40GB  A100-80GB  L40S  H100  H200  B200
```

- Add a **count** with `:N`, e.g. `A100-80GB:2` or `H100:2`. A spec like `T4` means one GPU.
- **Unknown names pass through** to Modal (so a newly-launched GPU works day one) but won't be flagged as "known" by `cilicon gpus`.

## See also

- [configuration.md](configuration.md) — every field these tiers consume.
- [getting-started.md](getting-started.md) — running tiers locally.
- The root [README.md](../README.md) for the tier overview and honest-limits section.
