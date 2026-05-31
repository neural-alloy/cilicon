# Contributing to cilicon

Thanks for your interest! cilicon is a small, focused engine — the bar is
"clear, tested, and honest about what a green check means."

## Dev setup

```bash
pip install -e ".[dev]"      # engine + test deps
python -m pytest -q          # the whole suite (all Modal-free)
```

The test suite never touches Modal: config parsing, matrix expansion, tier
resolution, the pass/fail judge, size analysis, reports, and telemetry are all
pure functions, so `pytest` runs in well under a second with nothing to auth.

## Where things live

| Area | File |
|------|------|
| Validation tiers + boards (as data) | `cilicon/presets.py` |
| `cilicon.yml` parsing, matrix, boards | `cilicon/config.py` |
| Modal orchestration (build/boot/judge) | `cilicon/runner.py` |
| Size budgets | `cilicon/sizes.py` |
| JSON / JUnit / Markdown reports | `cilicon/report.py` |
| Telemetry events + sinks | `cilicon/telemetry.py` |
| CLI | `cilicon/cli.py` |
| Optional reference dashboard | `cilicon/service/` (not the product) |

## Adding a validation tier

The whole point is that you usually **shouldn't need to**. A new chip is one
`cilicon.yml` entry; a tier cilicon has never heard of is `validate: custom` +
`run:`. Only add a `Preset` to `cilicon/presets.py` when a tier is broadly
reusable — and keep it honest about what it actually proves.

## Pull requests

- Add or update a test for any behavior change (`tests/`).
- Run `python -m pytest -q` before pushing.
- Be honest in docs and `--detail` strings about what's verified vs. modeled.
  cilicon's credibility is that it never overclaims what a green check means.
