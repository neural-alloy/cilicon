"""cilicon size analysis: turn a toolchain `size` report into flash/RAM numbers
and judge them against a budget — the part of "does it actually fit" that you
otherwise only learn when the linker overflows on real silicon.

Pure functions over strings: no Modal, no I/O. The runner runs `{size_tool}
{artifact}` in the sandbox; everything here parses that output and decides
pass/fail against `flash_max` / `ram_max`. That keeps it unit-testable without a
toolchain installed.

We understand GNU `size` in its two formats:
  * Berkeley (default):   text  data  bss  dec  hex filename
  * SysV    (`-A`):       a section table, one `.name  size  addr` row each

For embedded targets the convention we use:
    flash (ROM) used = text + data      (code + initialized data, lives in flash)
    ram  used        = data + bss       (initialized + zeroed data, lives in RAM)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Sizes:
    text: int = 0          # code (flash)
    data: int = 0          # initialized data (flash at rest, RAM at run)
    bss: int = 0           # zero-initialized data (RAM)

    @property
    def flash(self) -> int:
        return self.text + self.data

    @property
    def ram(self) -> int:
        return self.data + self.bss

    def to_dict(self) -> dict:
        return {
            "text": self.text, "data": self.data, "bss": self.bss,
            "flash": self.flash, "ram": self.ram,
        }


@dataclass
class SizeReport:
    sizes: Optional[Sizes]
    flash_max: Optional[int] = None
    ram_max: Optional[int] = None
    ok: bool = True
    detail: str = ""
    over: list[str] = field(default_factory=list)   # which budgets blew

    def to_dict(self) -> dict:
        d = {"ok": self.ok, "detail": self.detail, "over": self.over,
             "flash_max": self.flash_max, "ram_max": self.ram_max}
        if self.sizes:
            d.update(self.sizes.to_dict())
        return d


# Berkeley header line, e.g. "   text    data     bss     dec     hex filename"
_BERKELEY_HDR = re.compile(r"^\s*text\s+data\s+bss\s+dec\s+hex", re.MULTILINE)
# A Berkeley data row: five+ leading integers then a filename.
_BERKELEY_ROW = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([0-9a-fA-Fx]+)\s+\S", re.MULTILINE
)
# SysV section row: ".name  size  addr"  (size + addr are decimal).
_SYSV_ROW = re.compile(r"^(\.\S+)\s+(\d+)\s+(\d+)\s*$", re.MULTILINE)

# How GNU sections map onto flash vs RAM, by name prefix.
_TEXT_SECTIONS = (".text", ".isr_vector", ".vectors", ".rodata", ".init", ".fini",
                  ".ARM", ".preinit_array", ".init_array", ".fini_array")
_DATA_SECTIONS = (".data", ".ramfunc", ".tdata")
_BSS_SECTIONS = (".bss", ".tbss", ".noinit", "COMMON", ".stack", ".heap")


def parse(out: str) -> Optional[Sizes]:
    """Parse `size` output (Berkeley or SysV). Returns None if nothing parsed."""
    if not out:
        return None

    if _BERKELEY_HDR.search(out):
        m = _BERKELEY_ROW.search(out)
        if m:
            return Sizes(text=int(m.group(1)), data=int(m.group(2)), bss=int(m.group(3)))

    rows = _SYSV_ROW.findall(out)
    if rows:
        s = Sizes()
        for name, size, _addr in rows:
            n = int(size)
            if name.startswith(_DATA_SECTIONS):
                s.data += n
            elif name.startswith(_BSS_SECTIONS):
                s.bss += n
            elif name.startswith(_TEXT_SECTIONS):
                s.text += n
            # unknown sections are ignored rather than mis-bucketed
        if s.text or s.data or s.bss:
            return s

    # last resort: a lone Berkeley row with no header (some `size` builds)
    m = _BERKELEY_ROW.search(out)
    if m:
        return Sizes(text=int(m.group(1)), data=int(m.group(2)), bss=int(m.group(3)))
    return None


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024*1024):.1f}M"
    if n >= 1024:
        return f"{n / 1024:.1f}K"
    return f"{n}B"


def _pct(used: int, cap: Optional[int]) -> str:
    if not cap:
        return _human(used)
    return f"{_human(used)}/{_human(cap)} ({100*used/cap:.0f}%)"


def evaluate(out: str, flash_max: Optional[int], ram_max: Optional[int]) -> SizeReport:
    """Parse + judge against a budget. A budget of None means 'report, don't gate'."""
    s = parse(out)
    if s is None:
        return SizeReport(sizes=None, flash_max=flash_max, ram_max=ram_max,
                          ok=True, detail="size output not understood")

    over: list[str] = []
    if flash_max is not None and s.flash > flash_max:
        over.append(f"flash {_pct(s.flash, flash_max)}")
    if ram_max is not None and s.ram > ram_max:
        over.append(f"ram {_pct(s.ram, ram_max)}")

    if over:
        return SizeReport(s, flash_max, ram_max, ok=False,
                          detail="over budget: " + ", ".join(over), over=over)

    detail = f"flash {_pct(s.flash, flash_max)} · ram {_pct(s.ram, ram_max)}"
    return SizeReport(s, flash_max, ram_max, ok=True, detail=detail)
