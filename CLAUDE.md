# CLAUDE.md

This project's agent guidance lives in **AGENTS.md** — read it first:

@AGENTS.md

## Claude Code notes

- Prefer the fast offline commands (`cilicon doctor`, `cilicon targets`, `pytest`)
  for verifying changes. Do **not** run `cilicon run` (the full cloud matrix)
  unless the user explicitly asks — it spins up real Modal containers and bills them.
- Parse `cilicon run --json <path>` rather than the pretty terminal table.
- The deep architecture reference (module-by-module, with `file:line` anchors and
  the invariants you must preserve) is in `docs/architecture.md`.
