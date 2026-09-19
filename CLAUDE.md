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
- **The size *ceiling* only judges a run that re-encoded the video**
  (`size_verdict(..., video_encoded)`, set from `FilePlan.encodes_video`). It is really
  asking whether the x265 pass paid off; a run that copied the video has no way to
  shrink the file and every reason to grow it, so applying it would reject every
  cleanup pass. The floor applies either way — an output at a tenth of the source
  means something was lost however it got there. A ceiling rejection is not the end of
  the run: `engine._rebuild_without_encode` re-runs it from `plan.without_video_encode()`
  so the audio and subtitle work still lands, and the result is stored as `skip`, not
  `done`/`pending` — the rejected encode would be attempted and rejected again on every
  scan otherwise.

## Architecture

Pipeline: `probe → plan → ffmpeg → verify → replace`, orchestrated by `engine`, persisted by
`db`, driven by either `cli` or `web`.

- `probe.py` — thin ffprobe wrapper plus stream accessors (`lang_of`, `codec_of`, …). The
  `Probe` dataclass is just `path + streams + fmt`, which is why tests can fabricate one.
- `plan.py` — **pure**: `plan_file(probe, lib, cfg) -> FilePlan`. Audio is the part with
  the sharp edge: the stereo track is chosen by an explicit rule, not a score. A 2.0
  track already in the file wins (re-encoded to AAC only at a configured rate below
  its known source rate, otherwise copied; never kept beside its own copy); failing
  that, the widest `downmix_channels` track in a
  `preferred_languages` language is folded down, ties broken by bitrate then stream
  index. Anything with the `comment` or `visual_impaired` disposition, or a title
  matching `commentary_pattern`, is excluded — and if exclusion empties the candidate
  list, **nothing is downmixed**: `FilePlan.manual_review` is set instead. That is
  deliberately *not* a `reason`, because a file nobody ever looks at would otherwise be
  re-planned and re-queued forever. A stream copy is not
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
  *output* stream index at build time; `"{s}"` in `StreamPlan.input_extra` with the
  *source* index, and those args are emitted **before `-i`** because they configure the
  decoder (`-downmix stereo`, which is what `-request_channel_layout stereo` became in
  ffmpeg 7). A plan names `AAC_AUTO` rather than a real encoder: which AAC encoder exists
  is a property of the binary, not of the library's settings, and planning has to stay
  pure, so `encoder_for`/`aac_encoder` resolve it here instead.
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

### Libraries and modes

The split is the whole design: a **library** says where files are and which of them count
(`paths`, `extensions`, `exclude`, `min_size_mb`, `enabled`); a **mode** (`ModeCfg`) says
what happens to them — video / audio / subtitles / output / notify, each stage with its own
`enabled` switch, and a disabled stage copies its streams through untouched. A library names
one mode; two libraries that should behave the same point at the same mode rather than
carrying two copies of the settings that drift apart.

`resolve(cfg, lib, mode_id=None)` returns a `Profile`: the library's `id` and `name` with a
**deepcopy** of a mode's settings. `plan_file` takes that, never a `LibraryCfg`, so nothing
downstream knows whether the settings came from the library's own mode or a one-shot
override — and nothing a file's run does can leak into the live config or another file's
profile. `mode_id` is the override an import call passes; without one, the library's own
mode is used.

`Config.library_for(path)` routes a file by longest matching root; overlapping library paths
are rejected at config-validation time precisely because a file under two libraries would
have an ambiguous profile. There is no default library and no minimum — a fresh `Config()`
has none and every library can be deleted — but there are always modes, and
`_validate_global` rejects a library naming one that does not exist.

Notifications are part of the mode (`ModeCfg.notify`) for the same reason everything else
is: whether Jellyfin should be told about a file is part of what happens to it, so an import
mode can add a callback that scheduled scans do not make. `engine._process_one` fires them
from `profile.notify` (the resolved copy), and only on the success path after
`replace_original`.

Global config (`workers`, `schedule`, `output.temp_dir`, `web`) is about *how the daemon
runs*, not about what a file becomes.

### One-shot modes

`/api/process` — the endpoint Sonarr and Radarr hit on import — takes an optional `mode`,
applied to that one file instead of its library's. Two invariants make the one-shot
semantics real, both in `engine._process_one`:

1. The named mode decides the **encode**, but the state written to the database is always
   the library's own verdict. After a mode run the result is re-planned under
   `resolve(cfg, lib)`, so a `cleanup` file lands as `pending` with its x265 work still
   owed. Storing the mode's verdict would settle it as `done`/`skip` and `db.is_cached`
   would stop the next scan re-probing it — the file would silently never get encoded.
2. The requested mode lives in `engine._modes`, keyed by path, popped when the file is
   processed or cancelled. Deliberately not persisted: a restart drops it, which is the same
   outcome as the scan that follows.

A mode is validated when it is *saved*, so a typo is caught in the panel rather than at 3am
when a hook fires, and `apply_mode_updates` is transactional (`_transactional` restores the
mode if validation refuses) because a half-applied profile would be live for every library
pointing at it.

Config files from before this split still load: `_from_dict` lifts a library's own
`[libraries.video]`-style settings into a mode (sharing one mode between libraries
configured identically), and turns an old override-style mode into a full one by applying
its `overrides` to the defaults.

### API key

`web.api_key` is generated in `cli.main` on first start and written to `config.toml`.
`Handler._authorise` enforces it on `/api/` only when it is non-empty — so tests that build a
bare `Config()` need no key. `index.html` is served with `__API_KEY__` substituted, which is
what lets the panel call its own API; it is therefore not a boundary against anyone who can
load the panel, only a credential for Sonarr and Radarr. Don't document it as more than that.
