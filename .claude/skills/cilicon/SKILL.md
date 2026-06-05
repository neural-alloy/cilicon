---
name: cilicon
description: Validate, inspect, and (when explicitly asked) run cilicon firmware-CI configs. Use when editing cilicon.yml, adding or changing targets/boards/validation tiers, or when asked to check or run the cilicon matrix. Always prefer the free offline commands (`cilicon doctor`, `cilicon targets`); only use `cilicon run` (paid cloud) when the user explicitly asks.
---

# cilicon

cilicon is CI for real hardware: it builds *and* boots firmware across many chip
targets in parallel, in Modal cloud containers, owning zero hardware. Config
lives in `cilicon.yml`; the engine is the Python package in `cilicon/`.

## The golden rule

`cilicon run` spins up **real Modal cloud containers and bills the user**.
Everything else is free and offline. So:

- To verify a config edit → `cilicon doctor` (≈50ms, no cloud, no network).
- To run the actual build+boot matrix → **only when the user explicitly asks.**

Never run `cilicon run` just to "check" a change. Use `doctor`.

## Commands (prefer `--json` when you need to parse results)

| Command | Cost | Use it to |
|---|---|---|
| `cilicon doctor --json` | free, offline | Validate `cilicon.yml`, resolve every tier, flag weak checks. Exit 1 if any target has errors. |
| `cilicon targets --json` | free, offline | List targets, fully matrix-expanded, with what each builds/validates/proves. |
| `cilicon presets` | free, offline | List built-in validation tiers. |
| `cilicon boards` | free, offline | List one-word board aliases. |
| `cilicon gpus` / `cilicon sensors` | free, offline | List Modal GPU types / modeled Renode peripherals. |
| `cilicon run --json out.json` | **paid, needs Modal** | Build + boot the matrix; emits a machine-readable report. Only on explicit request. |

`-c/--config <path>` selects the config and goes **before** the subcommand.
`cilicon run` exit codes: `0` all passed · `1` a target failed · `2` `--target` matched nothing.

## Typical workflow for editing a config

1. `cilicon targets --json` — see the current matrix.
2. Edit `cilicon.yml` (field reference: `docs/configuration.md`).
3. `cilicon doctor --json` — confirm it parses, every tier resolves, and no new
   warnings appeared (e.g. a target with no `expect` only proves "didn't crash").
4. Report the doctor result. Suggest `cilicon run` but don't run it unasked.

## Editing the engine itself (Python)

- Read `AGENTS.md` and `docs/architecture.md` first — they map every module and
  list 8 invariants you must not break (lazy `import modal`, the literal-token
  matrix substitution, the `::cilicon::` marker protocol, etc.).
- Run `pytest` (fast, fully offline) after any change; add tests for new behaviour.
- Validation tiers and boards are **data** (`cilicon/presets.py`, `catalog.py`) —
  a new chip should be a YAML entry, not engine code.

## MCP

cilicon also ships an MCP server (`cilicon-mcp`, `pip install 'cilicon[mcp]'`)
exposing `doctor`, `list_targets`, `list_presets`, `list_boards`, and `run` as
tools. If it's registered, prefer those tool calls over shelling out.
