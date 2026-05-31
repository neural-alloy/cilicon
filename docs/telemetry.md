# Telemetry

A cilicon run fans many builds and boots across the cloud; without observability you only see the final table. Telemetry emits **one JSON object per lifecycle event** — `run.started`, `target.started`, `target.completed`, `run.completed` — each with timings, per-phase status, tier, GPU, and size numbers. Point it at a JSONL file, stdout, or an HTTP collector and you get a queryable record: flaky targets, slow phases, which tier fails most, build-vs-boot time over weeks.

> **Sinks never raise into a run.** A telemetry hiccup (unwritable file, dead collector) is swallowed — it must never fail your CI.

## Turning it on

### CLI flags

| Flag | Effect |
|---|---|
| `--telemetry <path>` | Append JSONL run/target/phase events to this path (parent dirs are created; flushed per event). |
| `--telemetry-stdout` | Also print each event to stdout, prefixed `cilicon.telemetry `. |

```bash
cilicon run --telemetry runs.jsonl
cilicon run --telemetry runs.jsonl --telemetry-stdout
```

In GitHub Actions, set the Action's `telemetry` input to a path and it's appended and uploaded with the results bundle (see [github-actions.md](github-actions.md)).

### Environment variables

A sink can also be configured purely from the environment (flags take precedence, and any combination of sinks runs together):

| Env var | Effect |
|---|---|
| `CILICON_TELEMETRY` | JSONL file path (same as `--telemetry`). |
| `CILICON_TELEMETRY_STDOUT` | If set (non-empty), print events to stdout (same as `--telemetry-stdout`). |
| `CILICON_TELEMETRY_URL` | Best-effort POST each event as JSON to this collector URL (OTLP-style ingestion; failures swallowed, ~2s timeout). |

If nothing is configured, telemetry is a no-op.

## The four event types

Every event has `ts` (epoch seconds, rounded to ms) and `run_id` (a per-run id like `run_18f...`). The `event` field names the type.

### `run.started`

```json
{
  "ts": 1717200000.123,
  "run_id": "run_18f2a3b1c4d5e6f7",
  "event": "run.started",
  "version": "0.0.0",
  "targets": ["jetson-perception/linux-arm", "stm32h7/cortex-m"],
  "count": 2
}
```

`version` is the installed cilicon package version; `targets` lists the target ids in this run; `count` is their number.

### `target.started`

```json
{
  "ts": 1717200001.456,
  "run_id": "run_18f2a3b1c4d5e6f7",
  "event": "target.started",
  "target": "stm32h7/cortex-m",
  "tier": "qemu_system",
  "gpu": null
}
```

`tier` is the target's `validate` value; `gpu` is the requested Modal GPU or `null`.

### `target.completed`

```json
{
  "ts": 1717200120.789,
  "run_id": "run_18f2a3b1c4d5e6f7",
  "event": "target.completed",
  "target": "stm32h7/cortex-m",
  "tier": "qemu_system",
  "gpu": null,
  "ok": true,
  "seconds": 12.084,
  "phases": {
    "build":    { "ok": true, "seconds": 8.21, "detail": "" },
    "size":     { "ok": true, "seconds": 0.10, "detail": "flash 4.0K/1.0M (0%) · ram 1.0K/256.0K (0%)" },
    "validate": { "ok": true, "seconds": 3.77, "detail": "boots, reaches main ('BOOT OK')" },
    "test":     null
  },
  "sizes": { "text": 4096, "data": 12, "bss": 1024, "flash": 4108, "ram": 1036 },
  "error": null
}
```

- `ok` — did the target pass overall.
- `seconds` — total target time (sum of phase times).
- `phases` — one entry per phase (`build`, `size`, `validate`, `test`), each `{ok, seconds, detail}` or `null` if the phase didn't run.
- `sizes` — the parsed `text`/`data`/`bss`/`flash`/`ram` byte counts, or `null` if no size analysis ran.
- `gpu` — the resolved GPU for the tier (e.g. `T4` for a `real_gpu` target), falling back to the target's own `gpu` field.
- `error` — an orchestration error string, or `null`.

### `run.completed`

```json
{
  "ts": 1717200121.000,
  "run_id": "run_18f2a3b1c4d5e6f7",
  "event": "run.completed",
  "passed": 3,
  "failed": 1,
  "total": 4,
  "wall_seconds": 170.5,
  "phase_seconds": { "build": 145.2, "size": 0.3, "validate": 24.1, "test": 0.0 },
  "by_tier": { "qemu_user": 2, "qemu_system": 1, "qemu_esp32": 1 }
}
```

- `passed` / `failed` / `total` — target counts.
- `wall_seconds` — wall-clock time for the whole parallel run.
- `phase_seconds` — total seconds spent in each phase summed across targets (useful against `wall_seconds` to see parallelism).
- `by_tier` — how many targets ran per validation tier.

## Querying the JSONL with `jq`

Since each line is a standalone JSON object, `jq` works directly over the file.

```bash
# every target that failed, across all runs
jq -r 'select(.event=="target.completed" and .ok==false) | .target' runs.jsonl

# build seconds per target (slowest first)
jq -r 'select(.event=="target.completed")
       | "\(.phases.build.seconds)\t\(.target)"' runs.jsonl | sort -rn

# tier distribution from the last run.completed
jq 'select(.event=="run.completed") | .by_tier' runs.jsonl | tail -1

# pass/fail summary line per run
jq -r 'select(.event=="run.completed")
       | "\(.run_id): \(.passed)/\(.total) in \(.wall_seconds)s"' runs.jsonl

# flash bytes for every target that reported a size
jq -r 'select(.event=="target.completed" and .sizes!=null)
       | "\(.target)\t\(.sizes.flash)"' runs.jsonl
```

## See also

- [getting-started.md](getting-started.md) — the `--telemetry` flags in context.
- [github-actions.md](github-actions.md) — the Action's `telemetry` input.
- [configuration.md](configuration.md) — the fields (`size_tool`, `flash_max`, `gpu`, …) that produce these numbers.
