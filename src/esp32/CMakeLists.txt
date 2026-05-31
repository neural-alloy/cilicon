# ESP-IDF project (Xtensa). Built with the Espressif toolchain, booted in
# qemu-system-xtensa — a completely different "world" from the ARM targets.
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cilicon_esp32)
