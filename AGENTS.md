# AGENTS.md

Operational guide for AI coding agents working in this repository. This is the
cross-tool standard file (Cursor, Aider, Copilot agents, etc.); Claude Code reads
it via `CLAUDE.md`. For the deep design dive, read **[docs/architecture.md](docs/architecture.md)**.

## What this project is

**cilicon** is CI for real hardware: it builds *and* boots firmware across many
chip targets in parallel, in [Modal](https://modal.com) cloud containers, owning
zero hardware. The unit of work is a **target** in `cilicon.yml` (a toolchain +
a build command + a way to prove the artifact actually runs). The Python package
in `cilicon/` is the product; `src/` is *sample firmware fixtures*, not engine code.

## Setup & commands

```bash
pip install -e ".[dev]"     # engine + pytest
pytest                      # full suite — runs WITHOUT Modal, no cloud, no network
```

### Fast, free feedback loops — prefer these (no Modal, no cost)

```bash
cilicon doctor              # validate cilicon.yml + resolve every tier — ~50ms
cilicon targets             # list targets, fully matrix-expanded
cilicon presets             # list validation tiers
cilicon boards              # list board aliases
```

### Cloud runs — these cost money and need Modal auth

```bash
cilicon run                 # builds + boots the whole matrix in real containers
cilicon run --json out.json # machine-readable results (parse THIS, not stdout)
cilicon run -t <id>         # one target only
```

> An agent should NOT call `cilicon run` casually — it spins up real cloud
> containers and bills the user. Use `cilicon doctor` to verify config changes.
> Only run the full matrix when the user explicitly asks, or in CI.

## Machine-readable outputs (use these instead of parsing the table)

- `cilicon run --json <path>` — cilicon's own shape: every phase, timing, output
  tail, sizes, artifacts. Schema lives in `cilicon/report.py:to_json`.
- `cilicon run --junit <path>` — JUnit XML; drops into any CI dashboard.
- `cilicon run --summary <path>` — GitHub-flavoured Markdown (point at `$GITHUB_STEP_SUMMARY`).
- Exit codes: `0` all passed · `1` a target failed · `2` `--target` matched nothing.

## Code map

| Path | Role |
|---|---|
| `cilicon/cli.py` | CLI entrypoint, the live table, report writing |
| `cilicon/config.py` | `cilicon.yml` → `Target` objects (parse, matrix, boards) |
| `cilicon/presets.py`, `catalog.py` | validation tiers + boards, as **data** |
| `cilicon/runner.py` | the engine: fan across Modal, the marker protocol, the judge |
| `cilicon/sizes.py` `testparse.py` `baseline.py` `telemetry.py` `report.py` | pure analysis/output |
| `cilicon/service/` | OPTIONAL hosted dashboard (FastAPI + Supabase) — imports the engine, never vice-versa |
| `src/` | sample firmware the demo config builds — **not** engine code |
| `tests/` | pytest, all Modal-free |

Full module-by-module breakdown with `file:line` anchors: **[docs/architecture.md](docs/architecture.md)**.

## Conventions

- **Pure logic stays pure.** Config parsing, the script generator, the output
  parser, the judge, sizes, telemetry, and reports are string-in/string-out — no
  Modal, no I/O, no clock. That is what makes them testable.
- **`import modal` is always lazy** (inside functions), never at module top level.
- Config errors surface as `SystemExit("cilicon: …")` one-liners, not tracebacks.
- Match the surrounding style; modules carry a docstring explaining their "why."

## Invariants — do NOT break these (see architecture.md §12)

1. Keep `import modal` lazy — top-level imports break the test suite.
2. Matrix substitution is a literal `{var}` token replace (`config._substitute`),
   **never** `str.format` — shell braces in user commands must survive.
3. The `::cilicon::` marker protocol stays line-oriented and unique; new phases
   get BEGIN/END markers parsed the same way.
4. Telemetry sinks never raise into a run — a telemetry hiccup must not fail CI.
5. Artifact pull-back is best-effort; it never changes pass/fail.
6. Unknown YAML fields/tiers are rejected at parse time with a clean `SystemExit`.
7. The service imports from the engine, never the reverse.
8. A green check never overclaims — emulators model the chip; only `real_gpu` is
   real silicon. Keep `cilicon doctor`'s honesty warnings and the docs honest.

## When you change things

- Edited `presets.py`/`catalog.py`/parsing? Run `pytest` (it's fast and offline).
- Added a tier/board/format? Add tests in `tests/` alongside.
- Touched the engine? Verify with `cilicon doctor`, not a paid cloud run.
- Extension seams (where new behaviour plugs in) are tabulated in architecture.md §10.
