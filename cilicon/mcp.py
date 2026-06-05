"""cilicon MCP server: expose cilicon to any MCP-capable coding agent.

An agent that can speak the Model Context Protocol (Claude Code, Cursor, …) can
add this server and then *call* cilicon directly — validate a config, list the
matrix, or run it — instead of shelling out and scraping the table.

Tools:
  doctor        validate cilicon.yml + resolve every tier   (offline, free, SAFE)
  list_targets  the matrix, fully expanded                   (offline, free)
  list_presets  built-in validation tiers                    (offline, free)
  list_boards   board aliases                                (offline, free)
  run           build + boot the matrix in Modal             (COSTS MONEY, needs auth)

Run it:
    cilicon-mcp                 # stdio transport (what agents launch)

Register it with an agent by pointing its MCP config at the command `cilicon-mcp`.
Install the dependency with:  pip install 'cilicon[mcp]'
"""
from __future__ import annotations

import json
from typing import Optional

from . import config as cfgmod
from . import presets as presetmod
from . import report as reportmod
from .cli import _doctor_report, _targets_report

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    raise SystemExit(
        "cilicon's MCP server needs the MCP SDK: pip install 'cilicon[mcp]'"
    )

mcp = FastMCP("cilicon")


@mcp.tool()
def doctor(config: str = "cilicon.yml") -> dict:
    """Validate a cilicon.yml WITHOUT running anything: parse it, resolve every
    validation tier, and flag weak checks. Offline, free, ~50ms — the safe way to
    verify config edits. Prefer this over `run` when checking changes.

    Returns {ok, errors, targets:[{id, validate, ok, errors[], warnings[]}]}."""
    return _doctor_report(cfgmod.load(config))


@mcp.tool()
def list_targets(config: str = "cilicon.yml") -> dict:
    """List the targets in a cilicon.yml, fully matrix-expanded, with what each
    builds, validates, and proves. Offline and free."""
    return _targets_report(cfgmod.load(config))


@mcp.tool()
def list_presets() -> dict:
    """List cilicon's built-in validation tiers (qemu_user, qemu_system, renode,
    sim, real_gpu, custom, …). Offline and free."""
    return {
        "tool": "cilicon",
        "presets": [
            {"name": n, "full_system": p.full_system, "gpu": p.gpu, "blurb": p.blurb}
            for n, p in presetmod.PRESETS.items()
        ],
    }


@mcp.tool()
def list_boards() -> dict:
    """List cilicon's built-in board aliases — one-word toolchain+tier bundles
    you can reference with `board: <name>`. Offline and free."""
    return {"tool": "cilicon", "boards": sorted(presetmod.BOARDS)}


@mcp.tool()
def run(config: str = "cilicon.yml", target: Optional[str] = None) -> dict:
    """Build AND boot the matrix in real Modal cloud containers, then return a
    machine-readable report (every phase, timing, sizes, pass/fail, output tails).

    ⚠ COSTS MONEY and requires Modal auth (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET).
    Only call this when the user has explicitly asked to run the matrix — use
    `doctor` to validate config changes. Pass `target` to run a single target.

    Returns the same shape as `cilicon run --json` (see report.to_json)."""
    import time
    from .runner import run_matrix

    cfg = cfgmod.load(config)
    t0 = time.time()
    results = run_matrix(cfg, only=target, on_update=lambda *a: None)
    wall = time.time() - t0
    return json.loads(reportmod.to_json(results, wall))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
