"""cilicon presets: validation tiers and boards as DATA, not code.

The whole promise of cilicon is that adding a new chip is *one yaml entry*, never
a cilicon code change. A preset is pure data: the shell command that proves an
artifact actually runs, plus what the sandbox needs to run it (a GPU, whether
it's a full-system boot we shouldn't expect a clean exit from).

Two escape hatches keep "any hardware" honest:
  * `validate: custom` + `run: <shell>` — a tier cilicon has never heard of, with
    no edit to this file.
  * a `board:` catalog — a one-word alias that expands to a known
    (base + apt + tier + machine) bundle so common silicon is zero-config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Preset:
    """A validation tier. `run` is a shell template; the {fields} below are
    substituted from the Target before it runs in the sandbox."""
    name: str
    run: str                       # shell template, or "" for the custom tier
    full_system: bool = False      # full-system boot: the expect string IS the
                                   # proof; we don't require a clean emulator exit
    gpu: Optional[str] = None      # request a real Modal GPU of this type (e.g. "T4")
    defaults: dict = field(default_factory=dict)   # field values to fill if unset
    blurb: str = ""                # one-line description for `cilicon presets`


# Template fields available to every preset's `run` string. qemu-user tiers
# line-buffer so partial output survives a crash (it proves how far it got).
_USER = "stdbuf -oL -eL {qemu_bin} ./{artifact}"


def _system(qemu: str, default_machine: str) -> str:
    # boot bare-metal firmware in a full-system emulator; semihosting/UART
    # output proves it reaches main. timeout guards the never-exits case.
    return (
        f"timeout {{boot_timeout}} {qemu} -M {{machine}} -nographic "
        f"-semihosting -kernel ./{{artifact}} 2>&1 || true"
    )


PRESETS: dict[str, Preset] = {
    # ---- run a host binary directly --------------------------------------
    "native": Preset(
        "native", "./{artifact}",
        blurb="run the artifact directly on the host",
    ),

    # ---- qemu-user: cross-built Linux ELF, one arch per tier -------------
    "qemu_user": Preset(
        "qemu_user", _USER, defaults={"qemu_bin": "qemu-arm"},
        blurb="ARM Linux ELF under qemu-arm (loads, libs resolve, reaches main)",
    ),
    "qemu_user_aarch64": Preset(
        "qemu_user_aarch64", _USER, defaults={"qemu_bin": "qemu-aarch64"},
        blurb="ARM64 Linux ELF under qemu-aarch64",
    ),
    "qemu_user_riscv64": Preset(
        "qemu_user_riscv64", _USER, defaults={"qemu_bin": "qemu-riscv64"},
        blurb="RISC-V 64 Linux ELF under qemu-riscv64",
    ),

    # ---- qemu-system: bare-metal / RTOS firmware -------------------------
    "qemu_system": Preset(
        "qemu_system", _system("qemu-system-arm", "lm3s6965evb"),
        full_system=True, defaults={"machine": "lm3s6965evb"},
        blurb="bare-metal ARM (Cortex-M) firmware in qemu-system-arm",
    ),
    "qemu_system_aarch64": Preset(
        "qemu_system_aarch64", _system("qemu-system-aarch64", "virt"),
        full_system=True, defaults={"machine": "virt"},
        blurb="bare-metal ARM64 firmware in qemu-system-aarch64",
    ),
    "qemu_system_riscv": Preset(
        "qemu_system_riscv",
        "timeout {boot_timeout} qemu-system-riscv64 -M {machine} -bios none "
        "-nographic -kernel ./{artifact} 2>&1 || true",
        full_system=True, defaults={"machine": "virt"},
        blurb="bare-metal RISC-V firmware in qemu-system-riscv64",
    ),

    # ---- ESP32 / FreeRTOS (Xtensa) via ESP-IDF ---------------------------
    "qemu_esp32": Preset(
        "qemu_esp32",
        "cd /work/{app_dir} && . $IDF_PATH/export.sh >/dev/null 2>&1 && "
        "timeout {boot_timeout} idf.py qemu 2>&1 || true",
        full_system=True,
        blurb="ESP-IDF image in qemu-system-xtensa (bootloader, app, FreeRTOS)",
    ),

    # ---- Renode: models real boards + their PERIPHERALS, unlike QEMU -----
    # Headless flow: the .resc loads a board platform, loads the ELF, runs for a
    # bounded time and `quit`s, teeing the target's UART to a file. We run renode
    # (its own log to stderr), then cat the captured UART so the `expect` string
    # — printed by the firmware over a modeled peripheral — lands in stdout.
    # Use the antmicro/renode image (ships renode + platform descriptions), and
    # point `renode_script` at a .resc (see examples/renode/).
    "renode": Preset(
        "renode",
        "timeout {boot_timeout} renode --disable-xwt --disable-gui --console "
        "-e \"include @{renode_script}\" 2>/tmp/cilicon_renode.log ; "
        "echo '--- renode log ---' >&2 ; tail -n 40 /tmp/cilicon_renode.log >&2 ; "
        "echo '--- UART ---' ; cat {renode_uart_log} 2>/dev/null || true",
        full_system=True,
        defaults={"renode_uart_log": "/tmp/cilicon_uart.log"},
        blurb="firmware in Renode (peripheral-accurate; antmicro/renode image + .resc)",
    ),

    # ---- vendor cycle-accurate sim / ARM Fast Models (FVP) --------------
    # Just a Linux binary you bring in your image (or via a Dockerfile). Far
    # higher fidelity than QEMU — models the actual core timing/peripherals.
    # Set `sim_bin` (e.g. FVP_MPS2_Cortex-M4) and optional `sim_args`.
    "sim": Preset(
        "sim",
        "timeout {boot_timeout} {sim_bin} {sim_args} ./{artifact} 2>&1 || true",
        full_system=True,
        blurb="cycle-accurate vendor sim / ARM FVP (set sim_bin [+ sim_args])",
    ),

    # ---- real silicon: run on an actual GPU in Modal ---------------------
    # Not emulation — runs on a physical GPU. `gpu:` accepts any Modal type and
    # an optional count, e.g. T4 / L4 / A100-80GB / "H100:2"  (see cilicon gpus).
    "real_gpu": Preset(
        "real_gpu", "./{artifact}", gpu="T4",
        blurb="run the artifact on a real GPU (not emulation) in Modal",
    ),

    # ---- escape hatch: bring your own run command ------------------------
    "custom": Preset(
        "custom", "",
        blurb="you supply `run:` — any tier cilicon has never heard of",
    ),
}


# A board is a one-word alias for a bundle of target fields, applied as defaults
# (anything the target sets explicitly wins). These are just a few STARTERS —
# define your own under a top-level `boards:` in cilicon.yml for whatever you
# ship. A board can set ANY target field, so it's fully yours to shape.
BOARDS: dict[str, dict] = {
    "arm-linux": dict(
        base="debian:bookworm-slim",
        apt=["gcc-arm-linux-gnueabihf", "qemu-user"],
        validate="qemu_user", qemu_bin="qemu-arm",
    ),
    "aarch64-linux": dict(
        base="debian:bookworm-slim",
        apt=["gcc-aarch64-linux-gnu", "qemu-user"],
        validate="qemu_user_aarch64", qemu_bin="qemu-aarch64",
    ),
    "cortex-m": dict(
        base="debian:bookworm-slim",
        apt=["gcc-arm-none-eabi", "qemu-system-arm"],
        validate="qemu_system", machine="lm3s6965evb",
    ),
    "esp32": dict(
        base="espressif/idf:release-v5.3",
        validate="qemu_esp32",
    ),
    "cuda": dict(
        base="nvidia/cuda:12.4.1-devel-ubuntu22.04",
        validate="real_gpu", gpu="T4",
    ),
}

# The friendly starters above stay; merge in the big grounded catalog (100+
# boards/chips/GPUs across QEMU machines, qemu-user arches, Renode platforms,
# and Modal GPUs). Define your own under `boards:` in cilicon.yml to extend it.
from . import catalog as _catalog  # noqa: E402
BOARDS = {**BOARDS, **_catalog.BOARDS}
SENSORS = _catalog.SENSORS


# Modal's GPU lineup, smallest → biggest. The `gpu:` field accepts any of these,
# optionally with a count ("A100-80GB:2"). Unknown names are passed through (so a
# newly-launched GPU still works) but won't be flagged as known by `cilicon gpus`.
GPUS: list[str] = [
    "T4", "L4", "A10G", "A100", "A100-40GB", "A100-80GB",
    "L40S", "H100", "H200", "B200",
]


def split_gpu(spec: str) -> tuple[str, int]:
    """'A100-80GB:2' -> ('A100-80GB', 2); 'T4' -> ('T4', 1)."""
    spec = (spec or "").strip()
    if ":" in spec:
        base, _, count = spec.rpartition(":")
        return base, (int(count) if count.isdigit() else 1)
    return spec, 1


def gpu_known(spec: str) -> bool:
    base, _ = split_gpu(spec)
    return base in GPUS


@dataclass
class Resolved:
    """The concrete validator cilicon will run for a target."""
    cmd: str
    full_system: bool
    gpu: Optional[str]


# fields a preset template may reference; populated from the Target
_TEMPLATE_FIELDS = (
    "artifact", "machine", "qemu_bin", "boot_timeout", "app_dir", "renode_script",
    "renode_uart_log", "sim_bin", "sim_args",
)


# Fault markers that mean "it ran but crashed" — failed even if `expect` matched
# (firmware can print BOOT OK and *then* HardFault). Kept specific to avoid
# false positives; a target can opt out with `crash_check: false` or add its own
# via `expect_not:`.
_CRASH_COMMON = ["stack smashing detected"]
CRASH_SIGNATURES: dict[str, list[str]] = {
    "qemu_system":          ["HardFault", "BusFault", "UsageFault", "MemManage fault", "CPU lockup"],
    "qemu_system_aarch64":  ["Synchronous Abort", "Kernel panic"],
    "qemu_system_riscv":    ["Kernel panic", "Unhandled trap"],
    "qemu_esp32":           ["Guru Meditation Error", "abort() was called", "rst:0x"],
    "qemu_user":            ["Segmentation fault", "Aborted", "Illegal instruction", "(core dumped)"],
    "qemu_user_aarch64":    ["Segmentation fault", "Aborted", "(core dumped)"],
    "qemu_user_riscv64":    ["Segmentation fault", "Aborted", "(core dumped)"],
    "renode":               ["HardFault", "CPU abort", "lockup"],
    "native":               ["Segmentation fault", "Aborted", "(core dumped)"],
    "real_gpu":             ["CUDA error", "an illegal memory access", "out of memory"],
}


def crash_signatures(tier: str) -> list[str]:
    """Auto fault markers for a tier (common + tier-specific)."""
    return _CRASH_COMMON + CRASH_SIGNATURES.get(tier, [])


def is_tier(name: str) -> bool:
    return name in PRESETS


def resolve(t) -> Resolved:
    """Render a Target's validation tier into a runnable shell command.

    Works on any object exposing the template fields + `validate`, `run`,
    `gpu`, `full_system` (i.e. a config.Target)."""
    if t.validate == "custom":
        if not (t.run or "").strip():
            raise ValueError(f"target {t.id}: validate: custom requires a `run:` command")
        full = t.full_system if t.full_system is not None else bool(t.boot_timeout)
        return Resolved(t.run, full, (t.gpu or None))

    p = PRESETS.get(t.validate)
    if p is None:
        raise ValueError(
            f"target {t.id}: unknown validate tier '{t.validate}' "
            f"(known: {sorted(PRESETS)} or 'custom')"
        )
    if t.validate == "sim" and not (getattr(t, "sim_bin", "") or "").strip():
        raise ValueError(f"target {t.id}: validate: sim requires a `sim_bin` (the simulator binary)")
    if t.validate == "renode" and not (getattr(t, "renode_script", "") or "").strip():
        raise ValueError(f"target {t.id}: validate: renode requires a `renode_script` (.resc path)")
    fields = {k: getattr(t, k, "") for k in _TEMPLATE_FIELDS}
    cmd = p.run.format(**fields)
    full = t.full_system if t.full_system is not None else p.full_system
    gpu = (t.gpu or None) or p.gpu
    return Resolved(cmd, full, gpu)
