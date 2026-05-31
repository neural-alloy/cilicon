"""Config: matrix expansion, board/preset defaults, size-budget parsing."""
import textwrap

import pytest

from cilicon import config as cfgmod


def _load(tmp_path, yaml_text):
    p = tmp_path / "cilicon.yml"
    p.write_text(textwrap.dedent(yaml_text))
    return cfgmod.load(str(p))


def test_board_alias_fills_toolchain(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: node/arm
            board: arm-linux
            build: echo hi
            artifact: build/x
    """)
    t = cfg.targets[0]
    assert t.validate == "qemu_user"
    assert "qemu-user" in t.apt
    assert t.qemu_bin == "qemu-arm"


def test_matrix_expands_and_substitutes(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: app/{arch}
            board: "{board}"
            build: gcc-{arch} src.c
            matrix:
              arch: [armv7, aarch64]
              board: [arm-linux]
    """)
    ids = sorted(t.id for t in cfg.targets)
    assert ids == ["app/aarch64", "app/armv7"]
    armv7 = next(t for t in cfg.targets if t.id == "app/armv7")
    assert armv7.build == "gcc-armv7 src.c"   # {arch} substituted into build


def test_size_budget_human_strings(tmp_path):
    cfg = _load(tmp_path, """
        targets:
          - id: fw/m
            board: cortex-m
            build: make
            artifact: build/fw.elf
            size_tool: arm-none-eabi-size
            flash_max: 256K
            ram_max: 64K
    """)
    t = cfg.targets[0]
    assert t.flash_max == 256 * 1024
    assert t.ram_max == 64 * 1024
    assert t.has_size


def test_unknown_field_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        _load(tmp_path, """
            targets:
              - id: x
                build: make
                notafield: 1
        """)


def test_custom_tier_requires_run(tmp_path):
    with pytest.raises(SystemExit):
        _load(tmp_path, """
            targets:
              - id: x
                build: make
                validate: custom
        """)


def test_duplicate_ids_rejected(tmp_path):
    with pytest.raises(SystemExit):
        _load(tmp_path, """
            targets:
              - id: dup
                build: make
              - id: dup
                build: make
        """)
