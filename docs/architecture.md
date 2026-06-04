# Architecture & technical breakdown

This is the doc to read before you contribute. It explains how cilicon actually
works end to end — every module, the data that flows between them, the one clever
trick that makes the whole thing tick (the in-sandbox marker protocol), and the
seams where new behaviour is meant to be added.

The other docs ([getting-started](getting-started.md), [configuration](configuration.md),
[tiers](tiers.md)) tell you how to *use* cilicon. This one tells you how to *change* it.

---

## 1. The one-sentence mental model

> cilicon reads a list of **targets** from `cilicon.yml`, and for each one spins
> up a throwaway cloud container with that target's toolchain, runs a single
> generated shell script inside it that **builds → sizes → boots → tests** the
> artifact, parses the script's stdout back into structured results, and judges
> whether "it actually ran."

Everything else — the matrix expansion, the boards, the reports, the dashboard —
is sugar around that loop.

Two design rules explain almost every decision in the codebase:

1. **Adding a chip is a YAML entry, never a code change.** Validation tiers and
   boards are *data* (`presets.py`, `catalog.py`), not branches in the engine.
   The escape hatch (`validate: custom` + `run:`) means even an unheard-of tier
   needs no edit.
2. **The pure logic must be importable and testable without Modal.** `modal` is
   imported *lazily*, inside functions, never at module top level. So config
   parsing, the script generator, the output parser, the pass/fail judge, sizes,
   telemetry and reports are all plain string-in/string-out functions you can
   unit-test with no cloud, no toolchain, no clock. The test suite relies on this.

If a change you're making breaks either rule, stop and reconsider.

---

## 2. Repository layout

```
cilicon/                  ← the Python package (the actual product)
  __init__.py             version string
  cli.py                  argparse CLI, the live table, report writing  (entrypoint)
  config.py               cilicon.yml → list[Target]   (parse, matrix, boards)
  presets.py              validation tiers + starter boards, as DATA
  catalog.py              the big 100+ board/sensor/GPU catalog (data)
  runner.py               the engine: fan targets across Modal, parse, judge
  sizes.py                parse `size` output → flash/RAM, judge vs budget
  testparse.py            parse Unity / TAP test output → per-case rows
  baseline.py             snapshot a run, diff a later run for regressions
  telemetry.py            structured lifecycle events → sinks (jsonl/stdout/http)
  report.py               results → JSON / JUnit XML / Markdown summary
  service/                ← OPTIONAL hosted dashboard (not the product)
    app.py                FastAPI: webhook + dashboard routes
    orchestrator.py       webhook → clone → run_matrix → persist → check-run
    github.py             GitHub App auth, webhook verify, check-runs
    db.py                 Supabase (PostgREST + Storage) client
    auth.py               signed session cookies
    settings.py           env-driven config
    templates/ static/    Jinja2 + CSS for the dashboard

src/                      ← SAMPLE FIRMWARE, not engine code. These are the C/CUDA
                            fixtures the demo cilicon.yml builds (firmware.c,
                            esp32/, gpu/infer.cu, perception.c, loadtest.c, …).
examples/                 a fuller cilicon.yml + a Renode .resc
tests/                    pytest suite (all Modal-free)
supabase/migrations/      SQL schema for the optional service
action.yml                the composite GitHub Action wrapper
cilicon.yml               the canonical 4-target demo config
```

The single most common point of confusion: **`src/` is not the program.** It's
example embedded code used to demo the engine. The program is the `cilicon/`
package. If you're fixing engine behaviour you almost never touch `src/`.

---

## 3. End-to-end data flow

Here is one `cilicon run` from keystroke to exit code. File:line references point
at the real code so you can follow along.

```
cli.main()  cli.py:375
  └─ cmd_run()  cli.py:53
       ├─ config.load("cilicon.yml")  config.py:235
       │     parse YAML → expand matrix → apply boards+presets → validate
       │     → list[Target]
       ├─ _filter_changed()  cli.py:153      (optional --changed-files/-since)
       ├─ telemetry.Recorder + make_sink()   cli.py:77
       ├─ run_matrix(cfg, ...)  runner.py:361
       │     ├─ modal.App.lookup("cilicon")  runner.py:372
       │     └─ ThreadPoolExecutor → run_target() per target  runner.py:378
       │           run_target()  runner.py:262
       │             ├─ presets.resolve(t)        validate tier → shell cmd
       │             ├─ _image_for(t)             build the Modal Image (toolchain)
       │             ├─ _script(t)                generate the in-sandbox script
       │             ├─ modal.Sandbox.create(...) run it; sb.wait(); read stdout
       │             ├─ _phase_result() ×N        slice stdout by markers
       │             ├─ sizes.evaluate()          judge size budget
       │             ├─ _judge()                  judge "did it actually run?"
       │             ├─ testparse.parse()         parse Unity/TAP if test: set
       │             └─ _fetch_artifacts()        pull globs back (best-effort)
       │           → TargetResult
       │     (on_update callback streams each target's status to the live table)
       ├─ rec.run_completed() / close()     emit telemetry
       ├─ _handle_baseline()  cli.py:169    compare/update baseline
       └─ _write_reports()    cli.py:194    --json / --junit / --summary
  → exit 0 if all passed, 1 if any failed, 2 if --target matched nothing
```

The hosted service swaps the front of this pipeline (a GitHub webhook instead of
the CLI) but calls the **exact same `run_matrix`** in the middle — see §9.

---

## 4. Config: `cilicon.yml` → `Target` objects

**File: `config.py`.** Pure, no network. The output is a `Config` holding a
`list[Target]` plus the project directory.

The `Target` dataclass (`config.py:28`) is the central data structure of the
whole project — every other module consumes it. It's deliberately flat: a target
carries its toolchain (`base`/`apt`/`dockerfile`/`board`), its proof-it-runs
config (`validate`/`run`/`artifact`/`machine`/…), its assertions
(`expect`/`expect_regex`/`expect_exit`/`expect_not`/`crash_check`), an optional
size budget, an optional test phase, and plumbing (`env`/`secrets`/`timeout`).

`load()` (`config.py:235`) is the pipeline, and the *order* matters:

1. `yaml.safe_load` the file.
2. `_merge_boards()` (`config.py:264`): built-in `BOARDS` + the user's top-level
   `boards:`, user wins.
3. For each raw target, `_expand_matrix()` (`config.py:129`) produces one or more
   concrete dicts (the cartesian product of the `matrix:` values).
4. Each concrete dict goes through `_normalize()` (`config.py:167`): apply the
   `board:` bundle as defaults, then the tier's `preset.defaults`, coerce
   scalar→list fields, parse human sizes. **Explicit user values always win**
   over board/preset defaults — that's why `_normalize` uses `setdefault`.
5. `_build_target()` (`config.py:209`) validates required fields, rejects unknown
   fields and unknown tiers, and constructs the `Target`.
6. Duplicate ids (after expansion) are a hard error.

### Two subtleties worth internalizing before you touch this file

- **Matrix substitution is *not* `str.format`.** `_substitute()` (`config.py:115`)
  does a literal `{var}` token replace, recursing into lists and dicts. This is
  on purpose: a `build:` command full of shell `${...}` and `$(( ))` must survive
  untouched. If you "improve" this to use `.format()`, you will break every
  config with shell arithmetic. Don't.
- **Size parsing rejects booleans on purpose** (`config.py:155`). YAML `true` is
  an `int` subclass in Python, so `_parse_size` guards against it to catch
  `flash_max: true` typos instead of silently treating it as 1 byte.

Errors here are surfaced as `SystemExit("cilicon: …")` — a deliberate choice so
config mistakes print a clean one-liner and exit, rather than a traceback.

---

## 5. Presets & catalog: tiers and boards as data

**Files: `presets.py`, `catalog.py`.** This is the heart of rule #1.

A `Preset` (`presets.py:20`) is a frozen dataclass describing a validation tier:
a `run` shell *template*, whether it's a `full_system` boot, an optional `gpu`
request, and `defaults` to fill into the target. `PRESETS` (`presets.py:47`) is
just a dict of them — `native`, `qemu_user[_aarch64/_riscv64]`,
`qemu_system[_aarch64/_riscv]`, `qemu_esp32`, `renode`, `sim`, `real_gpu`, and
the `custom` escape hatch.

`resolve(t)` (`presets.py:245`) turns a `Target` into a concrete `Resolved`
(the actual shell command, `full_system` flag, and gpu). It:

- handles `custom` (requires `run:`),
- guards tiers that need extra fields (`sim` needs `sim_bin`, `renode` needs
  `renode_script`),
- `.format()`s the template with the target's `_TEMPLATE_FIELDS`
  (`presets.py:211`) — note that here `.format()` *is* correct, because preset
  templates are authored by us with known `{field}` placeholders.

`crash_signatures(tier)` (`presets.py:236`) returns the fault strings that mean
"ran but crashed" for a tier (e.g. `HardFault` for `qemu_system`, `Segmentation
fault` for `qemu_user`, `Guru Meditation Error` for ESP32). The judge uses these.

A **board** (`presets.BOARDS` + the much larger `catalog.BOARDS`, merged at
`presets.py:175`) is just a one-word alias for a bundle of target fields applied
as defaults. `catalog.py` also holds `SENSORS` (modeled Renode peripherals) and
`presets.GPUS` is Modal's GPU lineup. Unknown GPU names are *passed through* to
Modal on purpose (`presets.py:181`) so a newly launched GPU works day one.

**To add a tier:** add one `Preset` to `PRESETS` and (if it has fault markers) one
entry to `CRASH_SIGNATURES`. No engine code changes. **To add a board:** one dict
entry in `catalog.py` — or the user adds it under `boards:` in their own config.

---

## 6. The runner: where it all happens

**File: `runner.py`.** This is the only module that talks to Modal, and even here
`import modal` is lazy, inside functions.

### 6.1 The image (the target's toolchain world)

`_image_for(t, project_dir)` (`runner.py:74`) builds a Modal `Image`: either
`from_dockerfile` or `from_registry(base).apt_install(*apt)`. Then — and this is
important — it mounts the **whole project dir** at `/work` with `copy=False`
(`runner.py:85`). `copy=False` means the source is added at *runtime*, so editing
your code doesn't invalidate the cached toolchain layers. `_MOUNT_IGNORE`
(`runner.py:27`) keeps `.git`, caches, venvs, etc. out of the mount.

### 6.2 The marker protocol (read this twice)

This is the cleverest part of the codebase and the thing most likely to confuse a
new contributor.

We want to run build + size + validate + test as phases, with per-phase timing
and pass/fail, **but** we want to do it in a *single* container command. Why not
just call `sb.exec()` four times? Because each `exec` opens a separate TLS channel
and round-trip; one script is dramatically faster and simpler.

So `_script(t)` (`runner.py:124`) generates **one bash script** that wraps each
phase in sentinel markers. The marker string is `::cilicon::` (`_MK`,
`runner.py:97`). Each phase (`_phase()`, `runner.py:106`) emits:

```
::cilicon::BUILD_BEGIN
<the build command's combined stdout+stderr>
::cilicon::BUILD_END rc=0 ms=1234
```

…and, by default, bails out early on a nonzero rc so later phases don't run
against a broken artifact. `ms()` is a tiny shell helper that reads epoch
milliseconds for timing.

On the Python side, the output parser reverses this:

- `_slice(out, begin, end)` (`runner.py:151`) extracts the body between two
  markers.
- `_marker(out, name)` (`runner.py:164`) parses the `key=value` tokens off a
  marker line (so we recover `rc` and `ms`).
- `_phase_result()` (`runner.py:176`) combines them into a `StepResult` (name,
  ok, seconds, output, detail).

**Invariant to preserve:** marker lines must be unique and start-of-line. If you
add a phase, give it BEGIN/END markers and parse it the same way. Never print a
line that could start with `::cilicon::` from inside a user command.

### 6.3 The judge: "did it actually run?"

`_judge(t, code, out, seconds)` (`runner.py:214`) is the brain. Building and
exiting cleanly is *not* enough — cilicon's whole pitch is proving the thing ran.
The logic, in order:

1. A **fault marker** (`_forbidden_hit`, `runner.py:205` = user `expect_not` +
   the tier's crash signatures) fails the target *even if `expect` was printed
   first* — because firmware can print `BOOT OK` and *then* `HardFault`.
2. If `expect_exit` is set, the exit code must match exactly, plus expectations.
3. If the tier is `full_system` (a bare-metal boot), the `expect` string **is**
   the proof — we don't require a clean exit, because the emulator is killed by
   the boot timeout. If `expect` isn't met and we ran out the clock, it's
   reported as "hung"; otherwise "exited before reaching…".
4. Otherwise (`qemu_user`/`native`/`real_gpu`), require a clean exit (code 0) AND
   the expected proof. Nonzero codes are mapped to signal names
   (`139→SIGSEGV`, etc.) for a readable failure.

`_expectations_met()` (`runner.py:195`) checks all `expect` substrings and the
`expect_regex`. The whole judge is a pure function — fully unit-tested in
`tests/`.

### 6.4 run_target & run_matrix

`run_target()` (`runner.py:262`) is the per-target orchestration: resolve, build
image, create the sandbox, wait, read stdout, then walk the phases
(build → size → validate → test) building up a `TargetResult`. Note the early
exits: no build marker = "sandbox died early"; build failed = skip validate.
Artifacts are pulled back **best-effort** (`_fetch_artifacts`, `runner.py:342`) —
a failed pull never fails the target. The `finally` always terminates the sandbox.

`TargetResult.ok` (`runner.py:56`) is the authoritative pass/fail: build must
pass, validate must pass, and *if present* test and size must pass.

`run_matrix()` (`runner.py:361`) fans targets across a `ThreadPoolExecutor`,
streaming status via the `on_update(target_id, phase, result)` callback (this is
what drives the CLI's live table and the service's progress). `max_workers` caps
concurrent sandboxes — the hosted service passes its per-run ceiling so one
tenant can't fan a 200-target matrix across the platform at once.

---

## 7. The analysis modules (all pure)

These four take a `TargetResult` (or raw strings) and produce structured data.
None touch Modal, I/O (beyond what they return), or the clock. That's what keeps
them trivially testable.

- **`sizes.py`** — `parse()` understands GNU `size` in both Berkeley and SysV
  formats; `evaluate()` (`sizes.py:125`) judges `text+data` (flash) and
  `data+bss` (ram) against `flash_max`/`ram_max`. A budget of `None` means
  *report, don't gate*. Unparseable output passes leniently. The section→bucket
  mapping is the `_TEXT/_DATA/_BSS_SECTIONS` tuples (`sizes.py:73`).
- **`testparse.py`** — `parse()` turns a runner's stdout into per-case
  `{name, ok}` rows. Supports Unity (`file.c:42:test_name:PASS`) and TAP
  (`ok 1 - desc`), with `detect()` (`testparse.py:48`) auto-sniffing when
  `test_format` is blank. Turns "exit 0" into "23/24 passed, here's the one that
  didn't."
- **`baseline.py`** — `build()` snapshots the regress-able facts of a green run
  (flash, ram, boot seconds, normalized boot log); `compare()` (`baseline.py:94`)
  diffs a later run. Size growth past a threshold is *severe* (can fail the run);
  boot-time and log drift only *warn*, because emulation timing is noisy.
  `normalize_log()` (`baseline.py:34`) scrubs addresses/timestamps so log diffs
  are signal, not noise.
- **`telemetry.py`** — pure event *builders* (`target_event`, `run_summary`) plus
  a set of **sinks** that *never raise into the run* (every `emit` is wrapped in
  `try/except: pass`). `make_sink()` (`telemetry.py:167`) assembles sinks from
  flags + env (`CILICON_TELEMETRY`, `CILICON_TELEMETRY_STDOUT`,
  `CILICON_TELEMETRY_URL`); default is `NullSink`. The `HttpSink` URL is **only
  ever the one you configure** — there is no hardcoded collection endpoint. The
  `Recorder` (`telemetry.py:187`) glues the run lifecycle to a sink and injects
  the clock (so tests pass a fake clock).

**`report.py`** is the output formatter: `to_json` (cilicon's own shape),
`to_junit` (the lingua franca every CI dashboard reads), and `to_markdown` (the
GitHub check-run body — including the `_sweep_grids` ✅/❌ heatmap built from each
target's `matrix_values`). Also pure.

---

## 8. The CLI

**File: `cli.py`.** `argparse` with subcommands: `run`, `targets`, `presets`,
`boards`, `gpus`, `sensors`, `doctor`. A bare `cilicon` is `cilicon run`
(`cli.py:415`). The global `-c/--config` goes *before* the subcommand.

Most of `cli.py` is presentation: `_cell`/`_row_cells` draw a fixed-width table
where padding happens *before* ANSI colorizing so the columns stay aligned
(`cli.py:40`). `on_update()` (`cli.py:85`) is the streaming callback passed into
`run_matrix` — it prints a "→ building…" line when a target starts and the result
row when it finishes, and drives telemetry.

`cmd_doctor()` (`cli.py:313`) is worth knowing about: it validates `cilicon.yml`
without running anything — parses, resolves every tier, and warns about weak
checks (e.g. a target with no `expect`, where a green check only means "didn't
crash"). It's the 50ms feedback loop instead of a cloud round-trip.

---

## 9. The optional service (hosted dashboard)

**Directory: `cilicon/service/`.** This is explicitly *not the product* — it's a
reference implementation of a multi-tenant dashboard, behind the `[service]`
optional dependency. The key insight: it reuses the engine unchanged.

```
GitHub webhook (push/PR)
   → app.webhook()              app.py:39   verify HMAC sig, parse event
   → orchestrator.on_webhook()  orchestrator.py:33   write a "queued" run row (fast)
   → background thread:
       orchestrator.execute_run()  orchestrator.py:86
         ├─ quota check (per-org monthly seconds)
         ├─ create an in-progress GitHub check-run
         ├─ git clone @ the exact sha into a tempdir   _clone()  orchestrator.py:153
         ├─ config.load() + run_matrix(...)   ← THE SAME ENGINE
         ├─ _persist_results()   logs+artifacts → Supabase Storage, rows → DB
         └─ complete the check-run with the Markdown matrix table
```

The pieces:

- **`app.py`** — FastAPI routes: the webhook, GitHub OAuth (`/login`,
  `/auth/callback`), and the dashboard (`/`, `/projects/{id}`, `/runs/{id}`, plus
  an htmx `/runs/{id}/status` partial for live updates, and signed-URL
  `/dl/{kind}/{id}` downloads). Heavy work is pushed to a background thread so
  GitHub's webhook delivery doesn't time out (`app.py:57`) — a real deployment
  would swap that thread for a proper queue.
- **`github.py`** — GitHub App plumbing: constant-time webhook signature verify,
  JWT app auth, OAuth code exchange, check-run create/complete.
- **`db.py`** — a thin Supabase client over PostgREST + Storage using `httpx`
  (no heavy SDK). Uses the service-role key server-side only.
- **`auth.py`** — signed session cookies via `itsdangerous`; access is scoped to
  GitHub org membership (`_authz`, `app.py:213`).
- **`settings.py`** — all secrets/config from env; `require()` makes routes fail
  cleanly if the service isn't configured.
- **`supabase/migrations/0001_init.sql`** — the schema (orgs, users, memberships,
  projects, runs, target_results) with row-level security enabled.

If you only care about the engine, you can ignore this whole directory — it
imports *from* the engine, never the other way around.

---

## 10. Where to plug in new behaviour

The codebase is built around a handful of extension seams. In rough order of how
often they're used:

| You want to… | Do this | Touch |
|---|---|---|
| Support a new chip/board | Add a `board:` to your `cilicon.yml`, or a dict to `catalog.BOARDS` | none / `catalog.py` |
| Support a new validation tier | Add a `Preset` to `PRESETS` (+ `CRASH_SIGNATURES` if it has fault markers) | `presets.py` |
| Run something cilicon's never heard of | `validate: custom` + `run:` in YAML | none |
| Add a new on-target test format | Add a parser + a branch in `testparse.parse()`/`detect()` | `testparse.py` |
| Send telemetry somewhere new | Add a sink class + wire it in `make_sink()` | `telemetry.py` |
| Emit a new report format | Add a `to_<fmt>()` + a `--<fmt>` flag in `cli._write_reports` | `report.py`, `cli.py` |
| Add a new assertion kind | Extend `Target`, then `_judge()`/`_expectations_met()` | `config.py`, `runner.py` |
| Add a `cilicon` subcommand | Add a `cmd_*` + a subparser in `main()` | `cli.py` |

The further down this table you go, the more you should add tests alongside.

---

## 11. Testing

The whole suite is **Modal-free** — it exercises the pure layer:

```bash
pip install -e ".[dev]"
pytest
```

- `test_config.py` — matrix expansion, board/preset merging, human sizes, error cases
- `test_runner_script.py` — the generated script + the marker parser + `_judge`
- `test_sizes.py` — Berkeley/SysV `size` parsing and budget judging
- `test_report.py` — JSON/JUnit/Markdown output (incl. sweep grids)
- `test_github.py` — webhook signature verification, event parsing
- `test_features.py`, `test_cilicon.py`, `test_quality.py` — broader behaviour

Because `import modal` is lazy, none of these need Modal installed or
authenticated. **Keep it that way:** if you find yourself wanting to import
`modal` at the top of a module to make something testable, that's a signal the
testable logic should be pulled out into a pure function instead.

---

## 12. Invariants a contributor must not break

1. **`import modal` stays lazy.** Top-level imports of `modal` make the pure layer
   un-importable and break the test suite.
2. **Matrix substitution stays a literal token replace** (`config._substitute`),
   never `str.format` — shell braces in user commands must survive.
3. **The marker protocol stays line-oriented and unique.** New phases get
   BEGIN/END markers parsed the same way.
4. **Telemetry sinks never raise into a run.** A telemetry hiccup must not fail CI.
5. **Artifact pull-back is best-effort.** It never changes a target's pass/fail.
6. **Unknown fields/tiers are rejected at parse time** with a clean
   `SystemExit`, not a traceback deep in the run.
7. **The service imports from the engine, never vice-versa.** The engine has no
   knowledge of Supabase, GitHub, or the dashboard.
8. **A green check never overclaims.** Emulators model the chip; only `real_gpu`
   is real silicon. Keep `cilicon doctor`'s honesty warnings and the docs honest.

---

## See also

- [getting-started.md](getting-started.md) — install, auth, first run
- [configuration.md](configuration.md) — the complete `cilicon.yml` field reference
- [tiers.md](tiers.md) — every validation tier, board, and GPU, with fidelity honesty
- [telemetry.md](telemetry.md) — the event schema and sinks
- [github-actions.md](github-actions.md) — using cilicon as a PR check
- [SERVICE.md](../SERVICE.md) — running the optional hosted dashboard
- [CONTRIBUTING.md](../CONTRIBUTING.md) — dev setup and PR conventions
