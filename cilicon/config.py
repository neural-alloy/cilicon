"""cilicon config: parse cilicon.yml into Target specs.

A target is intentionally small and uniform across wildly different silicon. It
carries its own toolchain (base image + apt, or a Dockerfile) and its own way of
proving the artifact actually runs (a validation tier — see cilicon/presets.py).

Three layers of dynamism turn "the four tiers cilicon ships" into "any code, any
hardware":
  * `matrix:` — expand one entry into many ({var} substituted into every field)
  * `board:`  — a one-word alias for a known toolchain+tier bundle
  * `validate: custom` + `run:` — a tier cilicon has never heard of, in pure yaml
"""
from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from typing import Optional

from . import presets

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("cilicon needs pyyaml: pip install pyyaml")


@dataclass
class Target:
    id: str
    build: str = ""
    validate: str = "native"           # a preset name (presets.PRESETS) or "custom"

    # --- toolchain (its own world) ----------------------------------------
    base: str = "debian:bookworm-slim"
    apt: list[str] = field(default_factory=list)
    dockerfile: Optional[str] = None   # path to a custom Dockerfile (overrides base+apt)
    board: str = ""                    # one-word alias from presets.BOARDS

    # --- the proof-it-runs command ----------------------------------------
    run: str = ""                      # custom validate command (validate: custom)
    artifact: str = ""                 # path (in /work) to the built binary
    machine: str = "lm3s6965evb"       # qemu-system machine
    qemu_bin: str = "qemu-arm"         # qemu-user launcher for this arch
    app_dir: str = ""                  # ESP-IDF project dir (rel to /work), qemu_esp32
    renode_script: str = ""            # .resc path (in /work), renode tier
    renode_uart_log: str = "/tmp/cilicon_uart.log"  # where the .resc tees UART
    sim_bin: str = ""                  # sim tier: the FVP / vendor simulator binary
    sim_args: str = ""                 # sim tier: extra flags before the artifact
    gpu: str = ""                      # real_gpu / custom: Modal GPU type, e.g. "T4" or "H100:2"
    full_system: Optional[bool] = None # override the tier's "don't require clean exit"

    # --- assertions: how we judge "it actually ran" -----------------------
    expect: list[str] = field(default_factory=list)  # ALL substrings must appear
    expect_regex: str = ""             # a regex that must match the output
    expect_exit: Optional[int] = None  # require this exact exit code

    # --- size budget: does the artifact actually fit the silicon? ---------
    size_tool: str = ""                # e.g. "arm-none-eabi-size"; runs on artifact
    flash_max: Optional[int] = None    # bytes; text+data must fit (accepts "256K")
    ram_max: Optional[int] = None      # bytes; data+bss must fit  (accepts "64K")

    # --- optional test phase: run a suite ON the target after it boots ----
    test: str = ""                     # command (in /work) whose exit 0 == pass
    test_expect: list[str] = field(default_factory=list)

    # --- plumbing ---------------------------------------------------------
    env: dict = field(default_factory=dict)     # exported in the sandbox
    secrets: list[str] = field(default_factory=list)   # Modal secret names
    artifacts: list[str] = field(default_factory=list) # globs to pull back out
    boot_timeout: int = 60             # seconds to let an emulator boot
    timeout: int = 900                 # seconds, whole target
    matrix_values: dict = field(default_factory=dict)  # set by matrix expansion (sweep cell)

    @property
    def slug(self) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "-", self.id.lower()).strip("-")

    @property
    def has_test(self) -> bool:
        return bool(self.test.strip())

    @property
    def has_size(self) -> bool:
        return bool(self.size_tool.strip()) or self.flash_max is not None or self.ram_max is not None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Config:
    targets: list[Target]
    project_dir: str

    def get(self, target_id: str) -> Optional[Target]:
        for t in self.targets:
            if t.id == target_id or t.slug == target_id:
                return t
        for t in self.targets:  # fall back to a substring match
            if target_id in t.id:
                return t
        return None


# ---- helpers ---------------------------------------------------------------

def _substitute(value, subs: dict):
    """Replace literal {var} tokens for matrix vars ONLY — never str.format,
    so shell braces / $(( )) in build commands survive untouched."""
    if isinstance(value, str):
        for k, v in subs.items():
            value = value.replace("{" + k + "}", str(v))
        return value
    if isinstance(value, list):
        return [_substitute(x, subs) for x in value]
    if isinstance(value, dict):
        return {k: _substitute(v, subs) for k, v in value.items()}
    return value


def _expand_matrix(raw: dict) -> list[dict]:
    matrix = raw.get("matrix")
    if not matrix:
        return [raw]
    if not isinstance(matrix, dict) or not matrix:
        raise SystemExit(f"cilicon: target {raw.get('id','?')} has a malformed 'matrix'")
    base = {k: v for k, v in raw.items() if k != "matrix"}
    keys = list(matrix)
    out = []
    for combo in itertools.product(*[_aslist(matrix[k]) for k in keys]):
        subs = dict(zip(keys, combo))
        concrete = _substitute(dict(base), subs)
        # remember which sweep cell this is, so the report can draw a grid
        concrete["matrix_values"] = {k: str(v) for k, v in subs.items()}
        out.append(concrete)
    return out


def _aslist(v) -> list:
    return v if isinstance(v, list) else [v]


def _parse_size(v) -> Optional[int]:
    """Bytes from an int or a human string: 256K, 64k, 1M, 2g, 1024, "12 KB"."""
    if v is None:
        return None
    if isinstance(v, bool):  # guard: yaml `true` is an int subclass
        raise SystemExit(f"cilicon: bad size value {v!r}")
    if isinstance(v, int):
        return v
    import re as _re
    m = _re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)[bB]?\s*", str(v))
    if not m:
        raise SystemExit(f"cilicon: cannot parse size {v!r} (try 256K, 64k, 1M, or bytes)")
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def _normalize(raw: dict, boards: dict) -> dict:
    """Apply board + preset defaults and field aliases to a concrete (already
    matrix-expanded) raw target dict. Explicit user values always win.
    `boards` is the merged catalog: built-in starters + the user's own
    `boards:` from cilicon.yml (user definitions win)."""
    raw = dict(raw)

    # alias: qemu_user_bin (old name) -> qemu_bin
    if "qemu_user_bin" in raw:
        raw.setdefault("qemu_bin", raw.pop("qemu_user_bin"))

    # board: fill the bundle as defaults
    board = raw.get("board")
    if board:
        if board not in boards:
            raise SystemExit(
                f"cilicon: target {raw.get('id','?')} unknown board '{board}' "
                f"(known: {sorted(boards)} — or define it under top-level `boards:`)"
            )
        for k, v in boards[board].items():
            raw.setdefault(k, v)

    # preset defaults (e.g. qemu_bin for a riscv tier, machine for a system tier)
    tier = raw.get("validate", "native")
    p = presets.PRESETS.get(tier)
    if p:
        for k, v in p.defaults.items():
            raw.setdefault(k, v)

    # expect / test_expect: accept a scalar or a list, store as a list
    for key in ("expect", "test_expect"):
        if key in raw and not isinstance(raw[key], list):
            raw[key] = [raw[key]]

    # size budgets: accept human strings like "256K" / "2M" -> bytes
    for key in ("flash_max", "ram_max"):
        if key in raw:
            raw[key] = _parse_size(raw[key])

    return raw


def _build_target(raw: dict, index: int) -> Target:
    if "id" not in raw:
        raise SystemExit(f"cilicon: target #{index} missing 'id'")
    tid = raw["id"]
    if "build" not in raw:
        raise SystemExit(f"cilicon: target {tid} missing 'build'")

    tier = raw.get("validate", "native")
    if tier != "custom" and not presets.is_tier(tier):
        raise SystemExit(
            f"cilicon: target {tid} has unknown validate tier '{tier}' "
            f"(known: {sorted(presets.PRESETS)} or 'custom')"
        )
    if tier == "custom" and not (raw.get("run") or "").strip():
        raise SystemExit(f"cilicon: target {tid} uses 'validate: custom' but has no 'run:'")

    known = set(Target.__dataclass_fields__)
    unknown = set(raw) - known - {"matrix"}
    if unknown:
        raise SystemExit(
            f"cilicon: target {tid} has unknown field(s): {sorted(unknown)}"
        )
    kwargs = {k: v for k, v in raw.items() if k in known}
    return Target(**kwargs)


def load(path: str = "cilicon.yml") -> Config:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise SystemExit(f"cilicon: no config found at {path}")
    project_dir = os.path.dirname(path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    targets_raw = raw.get("targets") or []
    if not targets_raw:
        raise SystemExit("cilicon: config has no targets")

    boards = _merge_boards(raw.get("boards") or {})

    targets: list[Target] = []
    seen: set[str] = set()
    for i, t in enumerate(targets_raw):
        if not isinstance(t, dict):
            raise SystemExit(f"cilicon: target #{i} is not a mapping")
        for concrete in _expand_matrix(t):
            tgt = _build_target(_normalize(concrete, boards), i)
            if tgt.id in seen:
                raise SystemExit(f"cilicon: duplicate target id '{tgt.id}'")
            seen.add(tgt.id)
            targets.append(tgt)

    return Config(targets=targets, project_dir=project_dir)


def _merge_boards(user_boards: dict) -> dict:
    """Built-in starter boards + the user's own `boards:` (user wins). A board
    is just a bundle of target fields applied as defaults — define whatever your
    hardware needs, reference it by name with `board: <name>`."""
    if not isinstance(user_boards, dict):
        raise SystemExit("cilicon: top-level `boards:` must be a mapping of name -> fields")
    for name, spec in user_boards.items():
        if not isinstance(spec, dict):
            raise SystemExit(f"cilicon: board '{name}' must be a mapping of fields")
    return {**presets.BOARDS, **user_boards}
