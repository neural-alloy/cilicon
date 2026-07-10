"""cilicon board + sensor catalog — starter templates grounded in real emulator
support. Each board is just a bundle of Target fields (base/apt/validate/machine/
qemu_bin/gpu/renode_script) — pick one with `board: <name>`, override any field,
or define your own under `boards:` in cilicon.yml.

Honest about fidelity (so a green check means what it says):
  • Cortex-M / bare-metal entries map to a QEMU `-M <machine>` that boots directly.
  • ARM64 *Linux SoC* entries (Jetson/i.MX8/Graviton/…) resolve to
    qemu_system_linux_aarch64 — a REAL arm64 kernel boots and runs your static
    ELF as init (full-system boot). It's the generic `virt` machine + a stock
    kernel, so the SoC's specific peripherals still aren't modeled — but it is a
    boot, not a bare loader. Want only the ELF-load check? set
    `validate: qemu_user_aarch64` on the target.
  • ARM 32-bit / RISC-V *Linux SoC* entries resolve to qemu-user — you cross-build
    a Linux ELF and it runs under qemu-arm/riscv64 (loads, reaches main; no kernel).
  • `renode-*` entries use Renode (peripheral-accurate) and need a `.resc` —
    they point `renode_script` at renode/<name>.resc (bring your own, see
    examples/renode/). They run on the antmicro/renode image.
  • `gpu-*` entries run on a REAL Modal GPU (not emulation).
  • SENSORS are peripherals you attach inside a Renode .resc, NOT boot targets.

None of these have been run end-to-end against Modal; they're starting points,
not certified configs. Exact bootability depends on the emulator supporting that
machine and your firmware matching the chip.
"""
from __future__ import annotations


# ---- toolchain bundles (a board is one of these + a name) ------------------

def _cm(machine: str) -> dict:
    """bare-metal Cortex-M that qemu-system-arm boots directly."""
    return dict(base="debian:bookworm-slim",
                apt=["gcc-arm-none-eabi", "qemu-system-arm"],
                validate="qemu_system", machine=machine)


def _armhf() -> dict:
    return dict(base="debian:bookworm-slim",
                apt=["gcc-arm-linux-gnueabihf", "qemu-user"],
                validate="qemu_user", qemu_bin="qemu-arm")


def _arm64_boot() -> dict:
    """Full-system ARM64 Linux boot: cross-build a STATIC aarch64 ELF, then boot
    a real Debian arm64 kernel under qemu-system-aarch64 with the ELF as init.
    The `qemu-system-arm` package ships qemu-system-aarch64; cpio/gzip build the
    initramfs. This is the Linux-userspace analogue of the Cortex-M boot — where
    qemu_user_aarch64 only loads the ELF, this proves it survives a kernel boot.
    (`board: <soc>` → `validate: qemu_user_aarch64` remains an explicit ELF-only
    opt-out.)"""
    return dict(base="debian:bookworm-slim",
                apt=["gcc-aarch64-linux-gnu", "qemu-system-arm", "cpio", "gzip"],
                validate="qemu_system_linux_aarch64", machine="virt")


def _rv64() -> dict:
    return dict(base="debian:bookworm-slim",
                apt=["gcc-riscv64-linux-gnu", "qemu-user"],
                validate="qemu_user_riscv64", qemu_bin="qemu-riscv64")


def _rvsys(machine: str) -> dict:
    return dict(base="debian:bookworm-slim",
                apt=["gcc-riscv64-unknown-elf", "qemu-system-misc"],
                validate="qemu_system_riscv", machine=machine)


def _esp() -> dict:
    return dict(base="espressif/idf:release-v5.3", validate="qemu_esp32")


def _renode(name: str, arm: bool = True) -> dict:
    apt = ["gcc-arm-none-eabi"] if arm else ["gcc-riscv64-unknown-elf"]
    return dict(base="antmicro/renode:latest", apt=apt,
                validate="renode", renode_script=f"renode/{name}.resc")


def _gpu(g: str) -> dict:
    return dict(base="nvidia/cuda:12.4.1-devel-ubuntu22.04",
                validate="real_gpu", gpu=g)


# ---- the catalog -----------------------------------------------------------

BOARDS: dict[str, dict] = {}

# Cortex-M / bare-metal — real QEMU machines (boot via -kernel + semihosting)
for _name, _m in {
    "ti-lm3s6965": "lm3s6965evb", "ti-lm3s811": "lm3s811evb",
    "arm-mps2-an385": "mps2-an385", "arm-mps2-an386": "mps2-an386",
    "arm-mps2-an500": "mps2-an500", "arm-mps2-an505": "mps2-an505",
    "arm-mps2-an521": "mps2-an521", "arm-mps3-an524": "mps3-an524",
    "arm-mps3-an547": "mps3-an547", "arm-musca-a": "musca-a",
    "arm-musca-b1": "musca-b1", "bbc-microbit": "microbit",
    "netduino2": "netduino2", "netduino-plus2": "netduinoplus2",
    "stm32-vldiscovery": "stm32vldiscovery", "canon-a1100": "canon-a1100",
}.items():
    BOARDS[_name] = _cm(_m)

# ARM 32-bit Linux SoCs — all run cross-built Linux userspace under qemu-arm
for _name in [
    "rpi-zero", "rpi-1", "rpi-2", "beaglebone-black", "beagleboard",
    "imx6ull", "imx6q", "imx7d", "allwinner-h3", "allwinner-h2plus",
    "rockchip-rk3288", "exynos4412", "zynq-7000", "stm32mp157",
    "ti-am335x", "ti-am437x", "nxp-ls1021a", "marvell-kirkwood",
    "broadcom-bcm2836", "atmel-sama5d3",
]:
    BOARDS[_name] = _armhf()

# ARM64 Linux SoCs — a REAL arm64 kernel boots the static artifact as init
# (full-system boot, not a bare ELF load). `boot` means boot for these SoCs.
for _name in [
    "rpi-3", "rpi-4", "rpi-5", "jetson-nano", "jetson-tx2", "jetson-xavier",
    "jetson-orin", "jetson-thor", "jetson-agx-thor",
    "rockchip-rk3399", "rockchip-rk3588", "imx8mq", "imx8mp",
    "imx8mm", "snapdragon-845", "snapdragon-8cx", "mediatek-mt8183",
    "amlogic-s905", "allwinner-a64", "marvell-armada-8040", "ampere-altra",
    "aws-graviton2", "aws-graviton3", "broadcom-bcm2712", "ti-am62", "nxp-ls1046a",
]:
    BOARDS[_name] = _arm64_boot()

# RISC-V — bare-metal (qemu-system) and Linux userspace (qemu-user)
for _name, _m in {
    "sifive-e": "sifive_e", "sifive-u": "sifive_u", "riscv-spike": "spike",
    "shakti-c": "shakti_c", "microchip-icicle": "virt", "riscv-virt": "virt",
}.items():
    BOARDS[_name] = _rvsys(_m)
for _name in [
    "starfive-visionfive2", "allwinner-d1", "sifive-unmatched",
    "kendryte-k230", "thead-c906",
]:
    BOARDS[_name] = _rv64()

# Xtensa / ESP (and RISC-V ESP parts boot through the same ESP-IDF flow)
for _name in ["esp32", "esp32-s2", "esp32-s3", "esp32-c3", "esp32-c6"]:
    BOARDS[_name] = _esp()

# Renode — peripheral-accurate; needs a renode/<name>.resc (bring your own)
for _name in [
    "renode-stm32f4-discovery", "renode-stm32f072", "renode-stm32f103",
    "renode-stm32f746", "renode-stm32l072", "renode-stm32h743",
    "renode-nrf52840", "renode-nrf52832", "renode-cc2538", "renode-cc1352",
    "renode-efr32mg", "renode-efm32gg", "renode-max32652", "renode-samd20",
    "renode-samd21", "renode-tms570", "renode-quark-c1000", "renode-renesas-ra",
    "renode-stm32f405", "renode-nrf5340",
]:
    BOARDS[_name] = _renode(_name)
for _name in ["renode-hifive1", "renode-hifive-unleashed", "renode-kendryte-k210",
              "renode-leon3", "renode-polarfire-soc", "renode-miv"]:
    BOARDS[_name] = _renode(_name, arm=False)

# GPUs — run on REAL Modal silicon (not emulation)
for _g in ["T4", "L4", "A10G", "A100", "A100-40GB", "A100-80GB",
           "L40S", "H100", "H200", "B200"]:
    BOARDS["gpu-" + _g.lower()] = _gpu(_g)


# ---- sensors: modeled peripherals you ATTACH in a Renode .resc -------------
# NOT boot targets. Listed so you know what you can wire onto a renode-* board.
SENSORS: dict[str, str] = {
    "bme280": "Bosch temp/humidity/pressure (I2C/SPI)",
    "bmp280": "Bosch temp/pressure (I2C/SPI)",
    "bme680": "Bosch temp/humidity/pressure/gas (I2C)",
    "mpu6050": "InvenSense 6-axis IMU (I2C)",
    "mpu9250": "InvenSense 9-axis IMU (I2C/SPI)",
    "lsm9ds1": "ST 9-axis IMU (I2C/SPI)",
    "lsm6dsl": "ST 6-axis IMU (I2C/SPI)",
    "lis3dh": "ST 3-axis accelerometer (I2C/SPI)",
    "adxl345": "Analog Devices 3-axis accelerometer (I2C/SPI)",
    "sht3x": "Sensirion temp/humidity (I2C)",
    "sht21": "Sensirion temp/humidity (I2C)",
    "si7021": "SiLabs temp/humidity (I2C)",
    "htu21d": "TE temp/humidity (I2C)",
    "tmp102": "TI temperature (I2C)",
    "tmp117": "TI high-accuracy temperature (I2C)",
    "mcp9808": "Microchip temperature (I2C)",
    "ina219": "TI current/power monitor (I2C)",
    "ina226": "TI current/power monitor (I2C)",
    "max30102": "Maxim pulse-ox / heart-rate (I2C)",
    "vl53l0x": "ST time-of-flight distance (I2C)",
    "bh1750": "Rohm ambient light (I2C)",
    "apds9960": "Broadcom gesture/proximity/light (I2C)",
    "ccs811": "AMS eCO2 / VOC gas (I2C)",
    "scd30": "Sensirion CO2/temp/humidity (I2C)",
    "ds3231": "Maxim real-time clock (I2C)",
}
