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


# fidelity — WHAT ACTUALLY RAN, orthogonal to who signed it (WEDGE_SPEC §0.2).
# It is a property of the TIER, never of user config: an ELF that reaches main is
# not a boot no matter what the yaml calls the board. "boot_tested" is reserved
# for FULL_SYSTEM_BOOT and up.
ELF_LOAD = "ELF_LOAD"                   # loaded a binary, reached main. No kernel.
FULL_SYSTEM_BOOT = "FULL_SYSTEM_BOOT"   # a real kernel/RTOS booted the artifact.
REAL_HARDWARE = "REAL_HARDWARE"         # ran on physical silicon (a Modal GPU).


@dataclass(frozen=True)
class Preset:
    """A validation tier. `run` is a shell template; the {fields} below are
    substituted from the Target before it runs in the sandbox."""
    name: str
    run: str                       # shell template, or "" for the custom tier
    full_system: bool = False      # full-system boot: the expect string IS the
                                   # proof; we don't require a clean emulator exit
    fidelity: str = ELF_LOAD       # what ran (ELF_LOAD/FULL_SYSTEM_BOOT/REAL_HARDWARE)
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


# Full-system Linux boot for aarch64 userspace targets (Jetson/i.MX/Graviton-class
# SoCs). Where qemu_user just loads the ELF, this boots a REAL ARM64 Linux kernel
# under qemu-system-aarch64, hands it an initramfs whose /init runs the artifact,
# and asserts on the actual console — kernel boot log AND the artifact's output.
# So `expect` is proven from inside a booted Linux, not a bare loader.
#
# The kernel (Debian's linux-image-arm64) + an arm64 static busybox are pulled via
# apt into the sandbox at run time; the artifact must be a STATICALLY linked
# aarch64 ELF (nothing else is in the initramfs). Deliberately brace-free shell so
# presets.resolve()'s str.format only touches the {artifact}/{machine}/{boot_timeout}
# fields — see docs/results-schema.md. → makes `boot` mean boot for FL-051's fleet.
_LINUX_AARCH64_BOOT = r"""
set -e
cp {artifact} /tmp/cil_app
echo '::cilicon-boot:: provisioning arm64 kernel + busybox'
dpkg --add-architecture arm64
apt-get update -qq
KPKG=$(apt-cache depends linux-image-arm64:arm64 | sed -n 's/.*Depends: \(linux-image-[^ ]*\).*/\1/p' | head -1)
KPKG=$(echo "$KPKG" | cut -d: -f1)
cd /tmp
apt-get download "$KPKG:arm64" busybox-static:arm64
dpkg-deb -x linux-image-*_arm64.deb /tmp/cil_kx
dpkg-deb -x busybox-static_*_arm64.deb /tmp/cil_bx
KZ=$(ls /tmp/cil_kx/boot/vmlinuz-*-arm64 | head -1)
MAGIC=$(od -An -tx1 -N2 "$KZ" | tr -d ' ')
if [ "$MAGIC" = "1f8b" ]; then gzip -dc < "$KZ" > /tmp/cil_Image; else cp "$KZ" /tmp/cil_Image; fi
BB=$(ls /tmp/cil_bx/bin/busybox /tmp/cil_bx/usr/bin/busybox 2>/dev/null | head -1)
R=/tmp/cil_initrd
rm -rf "$R"; mkdir -p "$R/bin" "$R/proc" "$R/sys" "$R/dev"
cp "$BB" "$R/bin/busybox"
cp /tmp/cil_app "$R/app"
printf '%s\n' '#!/bin/busybox sh' '/bin/busybox mount -t proc proc /proc 2>/dev/null' '/bin/busybox mount -t sysfs sys /sys 2>/dev/null' 'echo ::cilicon-boot:: kernel booted to PID1 --' '/bin/busybox uname -a' '/bin/busybox head -1 /proc/version' 'echo ::cilicon-boot:: exec artifact as init --' '/app' 'echo ::cilicon-boot:: cilicon-init-done' '/bin/busybox poweroff -f' > "$R/init"
chmod +x "$R/init" "$R/bin/busybox" "$R/app"
# Raw (uncompressed) cpio, root-owned. The kernel accepts a bare newc cpio as an
# initramfs; the gzip step was the ONLY thing that could yield "junk at the end of
# compressed archive" -> unpack fail -> mount_root panic. -R 0:0 so files aren't
# owned by the sandbox's build uid. cpio stderr is kept (a truncated archive was
# the failure we were hiding with 2>/dev/null).
( cd "$R" && find . | cpio -o -H newc -R 0:0 ) > /tmp/cil_initramfs.cpio
echo '::cilicon-boot:: initramfs' $(wc -c < /tmp/cil_initramfs.cpio) 'bytes'
echo '::cilicon-boot:: booting linux under qemu-system-aarch64'
timeout {boot_timeout} qemu-system-aarch64 -M {machine} -cpu cortex-a57 -smp 1 -m 512 -nographic -kernel /tmp/cil_Image -initrd /tmp/cil_initramfs.cpio -append "console=ttyAMA0 loglevel=4 panic=-1 rdinit=/init" 2>&1 || true
"""


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
        full_system=True, fidelity=FULL_SYSTEM_BOOT, defaults={"machine": "lm3s6965evb"},
        blurb="bare-metal ARM (Cortex-M) firmware in qemu-system-arm",
    ),
    "qemu_system_aarch64": Preset(
        "qemu_system_aarch64", _system("qemu-system-aarch64", "virt"),
        full_system=True, fidelity=FULL_SYSTEM_BOOT, defaults={"machine": "virt"},
        blurb="bare-metal ARM64 firmware in qemu-system-aarch64",
    ),
    "qemu_system_riscv": Preset(
        "qemu_system_riscv",
        "timeout {boot_timeout} qemu-system-riscv64 -M {machine} -bios none "
        "-nographic -kernel ./{artifact} 2>&1 || true",
        full_system=True, fidelity=FULL_SYSTEM_BOOT, defaults={"machine": "virt"},
        blurb="bare-metal RISC-V firmware in qemu-system-riscv64",
    ),

    # ---- full-system Linux boot: real ARM64 kernel boots the artifact ----
    # The Linux-userspace analogue of qemu_system. Where qemu_user_aarch64 just
    # loads an ELF and reaches main, this boots a REAL Debian arm64 kernel under
    # qemu-system-aarch64 and runs the (static) artifact as PID 1 — kernel, init,
    # userspace. So `boot` finally means boot for Jetson/i.MX/Graviton-class SoCs.
    # → docs/results-schema.md, WEDGE_SPEC §0.2.
    "qemu_system_linux_aarch64": Preset(
        "qemu_system_linux_aarch64", _LINUX_AARCH64_BOOT,
        full_system=True, fidelity=FULL_SYSTEM_BOOT, defaults={"machine": "virt"},
        blurb="full-system ARM64 Linux boot: real kernel boots a static ELF as init "
              "(qemu-system-aarch64)",
    ),

    # ---- ESP32 / FreeRTOS (Xtensa) via ESP-IDF ---------------------------
    "qemu_esp32": Preset(
        "qemu_esp32",
        "cd /work/{app_dir} && . $IDF_PATH/export.sh >/dev/null 2>&1 && "
        "timeout {boot_timeout} idf.py qemu 2>&1 || true",
        full_system=True, fidelity=FULL_SYSTEM_BOOT,
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
        full_system=True, fidelity=FULL_SYSTEM_BOOT,
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
        full_system=True, fidelity=FULL_SYSTEM_BOOT,
        blurb="cycle-accurate vendor sim / ARM FVP (set sim_bin [+ sim_args])",
    ),

    # ---- real silicon: run on an actual GPU in Modal ---------------------
    # Not emulation — runs on a physical GPU. `gpu:` accepts any Modal type and
    # an optional count, e.g. T4 / L4 / A100-80GB / "H100:2"  (see cilicon gpus).
    "real_gpu": Preset(
        "real_gpu", "./{artifact}", gpu="T4", fidelity=REAL_HARDWARE,
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
    fidelity: str = ELF_LOAD   # WHAT RAN — from the tier, never user config (§0.2)


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
    "qemu_system_linux_aarch64": ["Kernel panic", "Unable to handle kernel", "Internal error",
                                  "Synchronous Exception", "Attempted to kill init"],
    "qemu_system_riscv":    ["Kernel panic", "Unhandled trap"],
    "qemu_esp32":           ["Guru Meditation Error", "abort() was called"],
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
        # a tier cilicon has never heard of can't be proven a boot — stay honest.
        return Resolved(t.run, full, (t.gpu or None), ELF_LOAD)

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
    # fidelity is the tier's, never the target's: `full_system: true` in yaml can
    # skip the clean-exit check, but it cannot promote an ELF load to a boot.
    return Resolved(cmd, full, gpu, p.fidelity)


def fidelity_of(t) -> str:
    """A target's fidelity — WHAT RAN — derived from its tier alone (§0.2). Pure
    and total: an unknown/custom tier is ELF_LOAD, so we never claim an
    unprovable boot. This is the value the JSON contract records per target."""
    p = PRESETS.get(getattr(t, "validate", ""))
    return p.fidelity if p else ELF_LOAD
