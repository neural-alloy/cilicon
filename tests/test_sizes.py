"""Size parser + budget judge — the 'does it fit the silicon' gate."""
from cilicon import sizes

BERKELEY = """   text\t   data\t    bss\t    dec\t    hex\tfilename
  12048\t    112\t    560\t  12720\t   31b0\tbuild/firmware.elf
"""

SYSV = """firmware.elf  :
section              size         addr
.isr_vector           388            0
.text                9000          388
.data                 112    536870912
.bss                  560    536871024
.stack               2048    536873072
Total               12108
"""


def test_parse_berkeley():
    s = sizes.parse(BERKELEY)
    assert (s.text, s.data, s.bss) == (12048, 112, 560)
    assert s.flash == 12160      # text + data
    assert s.ram == 672          # data + bss


def test_parse_sysv_buckets_sections():
    s = sizes.parse(SYSV)
    assert s.text == 388 + 9000  # isr_vector + text
    assert s.data == 112
    assert s.bss == 560 + 2048   # bss + stack
    assert s.flash == 9500


def test_parse_garbage_is_none():
    assert sizes.parse("no size info here") is None
    assert sizes.parse("") is None


def test_evaluate_within_budget():
    r = sizes.evaluate(BERKELEY, flash_max=64 * 1024, ram_max=8 * 1024)
    assert r.ok and not r.over
    assert "flash" in r.detail and "ram" in r.detail


def test_evaluate_over_flash():
    r = sizes.evaluate(BERKELEY, flash_max=8 * 1024, ram_max=8 * 1024)
    assert not r.ok
    assert any("flash" in o for o in r.over)
    assert "over budget" in r.detail


def test_evaluate_no_budget_is_report_only():
    r = sizes.evaluate(BERKELEY, flash_max=None, ram_max=None)
    assert r.ok                  # nothing to gate on
    assert r.sizes.flash == 12160


def test_evaluate_unparseable_does_not_gate():
    r = sizes.evaluate("???", flash_max=1, ram_max=1)
    assert r.ok and r.sizes is None
