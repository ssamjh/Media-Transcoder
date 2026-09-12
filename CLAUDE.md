# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m unittest discover -s tests -v         # full suite
python -m unittest tests.test_plan -v           # one module
python -m unittest tests.test_plan.TestIdempotency.test_output_of_a_full_run_needs_no_further_work
python -m unittest tests.test_modes.TestIdempotency

python -m app -c ./config/config.toml scan      # run the CLI against a local config
python -m app -c ./config/config.toml daemon    # scheduler + workers + web panel on :8080

docker compose up -d --build                    # the real deployment
docker compose run --rm transcoder <subcommand> # one-shot CLI in the container
```

Tests need no media files and no ffmpeg for `test_plan` / `test_config` (planning is pure
functions over ffprobe-shaped dicts). `test_api` starts a real `http.server` on an ephemeral
port but never starts worker threads, so it touches nothing.

## Hard constraints

- **Zero third-party dependencies, and it must stay that way.** Config is stdlib `tomllib`
  in, hand-rolled emitter out; the panel is stdlib `http.server`; there is no requirements
  file, no venv, nothing to `pip install`. Adding a dependency breaks the premise of the
  Dockerfile and the README.
- **Python 3.14 / ffmpeg 7.x** (Debian 13 base image).
- **The image starts as root and steps down.** `docker-entrypoint.sh` moves the
  `transcoder` user onto `PUID`/`PGID`, chowns `/config` and the configured scratch
  directory, and `exec setpriv`s into it — the linuxserver.io arrangement, so a bind
  mount's host ownership is matched instead of fought. Started unprivileged (compose
  `user:`) it skips all of that and runs as whoever it is. `cli._preflight` then checks
  those directories are writable before the first encode, because finding out per file
  burns a retry attempt each time.
- **Planning must be idempotent.** `plan_file()` applied to a file this tool already produced
  must return `needs_work == False`, *for every combination of stage switches, and under
  every mode* — otherwise every scan re-processes the whole library forever, or a repeated
  import hook encodes twice. `tests/test_plan.py::TestIdempotency` and
  `tests/test_modes.py::TestIdempotency` assert this; any change to planning rules needs a
  matching case there.
- **The scratch space is swept on `Engine.start()`.** `ffmpeg.sweep_scratch` deletes
  `WORK_PREFIX`-named leftovers from a hard kill — startup is the one moment no encode
  of ours is running, which is the same reasoning behind `db` resetting `running` rows.
- **Nothing overwrites a source until it verifies** (`ffmpeg._verify` → `replace_original`):
  encode to temp dir, re-probe, check stream count and ≥`min_duration_ratio` of source
  duration, discard unless the size lands inside the library's
  `min_size_ratio`..`max_size_ratio` window (`ffmpeg.size_verdict`, which also writes
  the rejection note), stage next to the original, `os.replace` into place.

## Architecture

Pipeline: `probe → plan → ffmpeg → verify → replace`, orchestrated by `engine`, persisted by
`db`, driven by either `cli` or `web`.

- `probe.py` — thin ffprobe wrapper plus stream accessors (`lang_of`, `codec_of`, …). The
  `Probe` dataclass is just `path + streams + fmt`, which is why tests can fabricate one.
- `plan.py` — **pure**: `plan_file(probe, lib, cfg) -> FilePlan`. A stream copy is not
  always free: `CONTAINER_SUBTITLES` says what the target container can mux, and a text
  track it cannot (`mov_text` into mkv) is converted to `srt` rather than copied — the
  muxer would otherwise refuse the header and fail the entire encode. Applies even with
  the subtitle stage off, because that is a muxing fact, not a policy. No I/O, no side effects.
  A `FilePlan` is a list of `StreamPlan`s (source index → output codec + extra args) plus
  `reasons` (human-readable work items) and `dropped`. `needs_work` is `skip_reason is None
  and bool(reasons)`, so an empty `reasons` list is the idempotency signal.
- `ffmpeg.py` — `build_args(plan, dest)` turns a plan into an invocation. Every output stream
  gets an explicit `-c:` — omitting one makes ffmpeg silently fall back to the container
  default encoder (H.264 for mkv). `"{i}"` in `StreamPlan.extra` is substituted with the
  *output* stream index at build time.
- `config.py` — the config file is **generated, not patched**. Dataclasses hold defaults;
  `META` / `LIB_META` hold per-field description, min/max, choices, `restart`, `readonly`.
  That one table drives the comments in the emitted TOML, the settings panel's schema, and
  validation of updates. Adding a setting means adding the dataclass field *and* its META
  entry. `dump_toml` → `save` rewrites the whole file, which is why the panel can safely
  write on every change.
- `engine.py` — the daemon. **A scan never starts work by itself**: the scheduler
  calls `enqueue_pending()` only when `schedule.process_after_scan` is on (default off),
  and `add_library` creates libraries disabled, so no file is ever encoded that nobody
  asked for. Worker threads pull paths off a `queue.Queue` (not a one-shot
  batch), so work can be queued and cancelled while live. `_process_one` **re-plans
  immediately before encoding** — a scan may be hours stale. Three failed attempts
  (`MAX_ATTEMPTS`) and a file is parked until an explicit retry.
- `db.py` — SQLite at `config/state.db`. `files` keyed by path, carrying `(size, mtime)` so
  an unchanged file that settled as `done`/`skip` is never re-probed; `history` records runs.
  Statuses: `pending|queued|running|skip|done|failed`.
- `notify.py` — outbound webhooks. `Notifier` owns a `queue.Queue` and one delivery
  thread, so a bulk import that finishes twenty files at once never blocks a worker on
  someone else's HTTP server. Delivery is **best effort**: 5xx/timeouts retry with a
  growing backoff, 4xx does not, and a hook that never succeeds is logged and dropped —
  the file on disk is already correct, so no encode state may depend on a third party.
  `notify.depth` counts in-flight retries as well as queued calls, or `join()` would
  report an empty backlog while a call was mid-backoff.
- `web.py` + `static/` — dispatch-dict routing to `api_*` methods, dependency-free SPA. No
  authentication anywhere, by design; the panel is trusted-network only.
- `cli.py` — argparse subcommands sharing the same `Engine`. `scan`/`check`/`libraries`
  support `--json`.

### Per-library profiles

Settings that decide *what happens to a file* live on the library (`LibraryCfg`), not
globally. Nothing is inherited — a library's full behaviour is readable in one place. Each of
video / audio / subtitles / output has its own `enabled` switch, and a disabled stage copies
its streams through untouched. `Config.library_for(path)` routes a file by longest matching
root; overlapping library paths are rejected at config-validation time precisely because a
file under two libraries would have an ambiguous profile.

There is no default library and no minimum. A fresh `Config()` has none, every library can
be deleted, and anything that needs a library to render — mode schemas, say — falls back to
a throwaway `LibraryCfg()` instead of assuming `libraries[0]` exists.

Notifications are part of the library profile (`LibraryCfg.notify`) for the same reason
everything else is: whether Jellyfin should be told about a file is a property of the
library, and being an ordinary dotted key means a **mode can override it** — an import
hook adds a callback that scheduled scans do not make. `engine._process_one` fires them
from `profile.notify` (the mode-resolved copy), and only on the success path after
`replace_original`.

Global config (`workers`, `schedule`, `output.temp_dir`, `web`) is about *how the daemon
runs*, not about what a file becomes.

### Modes

A `ModeCfg` is a named set of dotted overrides (`{"video.enabled": false}`) applied on top of
a library's profile for **one** `/api/process` call — the endpoint Sonarr and Radarr hit on
import. `resolve_library(cfg, lib, mode_id)` returns a **deepcopy** with the overrides
applied; never mutate the live `LibraryCfg`, or one file's request would change every other
file's profile.

Two invariants make the one-shot semantics real, both in `engine._process_one`:

1. The mode decides the **encode**, but the state written to the database is always the
   library's own verdict. After a mode run the result is re-planned under `lib` (not
   `profile`), so a `cleanup` file lands as `pending` with its x265 work still owed. Storing
   the mode's verdict would settle it as `done`/`skip` and `db.is_cached` would stop the next
   scan re-probing it — the file would silently never get encoded.
2. Modes live in `engine._modes`, keyed by path, popped when the file is processed or
   cancelled. Deliberately not persisted: a restart drops them, which is the same outcome as
   the scan that follows.

Overrides are validated against a throwaway `LibraryCfg` when the mode is *saved*, so a typo
is caught in the panel rather than at 3am when a hook fires. `MODE_FORBIDDEN` blocks
overriding identity and routing (`id`, `name`, `enabled`, `paths`).

### API key

`web.api_key` is generated in `cli.main` on first start and written to `config.toml`.
`Handler._authorise` enforces it on `/api/` only when it is non-empty — so tests that build a
bare `Config()` need no key. `index.html` is served with `__API_KEY__` substituted, which is
what lets the panel call its own API; it is therefore not a boundary against anyone who can
load the panel, only a credential for Sonarr and Radarr. Don't document it as more than that.
