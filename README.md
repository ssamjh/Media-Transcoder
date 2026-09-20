# transcoder

A self-hosted transcoder for a Sonarr/Radarr/Jellyfin library. It picks up a
file after the Arr import, does only the work that file actually needs, and
tells Jellyfin when the result is final.

```text
download client -> Sonarr/Radarr import -> Media-Transcoder
                -> Arr rescan + targeted rename -> AutoPulse -> Jellyfin
```

**New here? [SETUP.md](SETUP.md) walks the whole chain, step by step.**

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Libraries and modes](#libraries-and-modes)
- [What each stage does](#what-each-stage-does)
- [How the stereo track is made](#how-the-stereo-track-is-made)
- [The panel](#the-panel)
- [Sonarr and Radarr](#sonarr-and-radarr)
- [Notifications](#notifications)
- [Safety](#safety)
- [Backups](#backups)
- [Tuning throughput](#tuning-throughput)
- [State](#state)
- [API](#api)
- [Development](#development)

## What it does

- **Cleans up on import, encodes on a schedule.** New files get the cheap
  audio/subtitle/container work in seconds. The slow x265 pass happens later,
  off the import path.
- **Nothing is encoded until you ask.** New libraries start switched off. A
  scan only plans, and what it finds waits until you queue it.
- **Nothing is overwritten until it verifies.** See [Safety](#safety).
- **Zero Python dependencies.** Python 3.14 on Debian 13, ffmpeg 7.x. Nothing
  to `pip install`.
- **Idempotent.** A processed file plans out as "nothing to do" on every later
  scan, so re-running is free. Tests assert this for every combination of
  stage switches.
- **Durable imports.** A webhook is committed to SQLite before it is
  acknowledged, so a restart mid-encode resumes instead of losing the job.

## Quick start

**1. Point the volumes at your media** in `compose.yml`, and set `PUID`/`PGID`
to whoever owns it:

```bash
stat -c '%u:%g' /mnt/nfs/media     # the ids to put in PUID/PGID
docker compose up -d --build
```

**2. Open the panel** at <http://localhost:8080>. First start writes a fully
commented `config/config.toml` and generates an API key.

**3. Add a library** in the Libraries tab: give it a name and the path it
covers, pick the mode it should use.

**4. Scan, read the Files tab, then queue.** A new library arrives switched
off. Tick it on when its mode looks right.

> **The panel has no authentication.** The API key guards `/api/`, but the
> panel is served with that key embedded so the UI can use it. Anyone who can
> load the page can read it. Keep the panel on a trusted network.

### Where things are written

| Path | What | Notes |
| --- | --- | --- |
| `./config` | config.toml, state.db, backups | Small. |
| `/media` | your library | Files are replaced in place, once verified. |
| `./transcode_cache` | in-progress encodes | Real disk. Room for one encode per worker, roughly the size of a source file. Keep it off network shares. |

Startup checks the config and scratch directories and stops with the offending
path if either is unwritable. The media tree cannot be checked in advance, so
`PUID`/`PGID` want to match its owner.

Set a mode's `output.replace_original` to false to encode and then discard.
Good for checking settings against real files.

## Libraries and modes

**A library says where files are. A mode says what happens to them.**

- TV can use a mode that re-encodes to x265 and trims subtitles to English.
- Movies can use one that leaves the video alone and only cleans the audio.
- A 4K library can be scanned but never re-encoded.

Any number of libraries can share one mode, so two libraries that should
behave the same are configured in one place instead of two that drift apart.

Two modes ship by default:

| Mode | What it does |
| --- | --- |
| `standard` | Re-encode to x265, best audio plus a stereo downmix, subtitles trimmed, container normalised. |
| `cleanup` | Everything except re-encoding video. Seconds rather than hours. |

## What each stage does

Each stage has its own switch, per mode.

| Stage | On | Off |
| --- | --- | --- |
| **Video** | SD (<=576p) to x265 CRF 23, 720p CRF 23, 1080p CRF 22, each band configurable. Already-HEVC is copied. Above 1200p left alone. | Every video stream copied, and the height ceiling no longer applies, so a 4K file still gets its audio and subtitles cleaned. |
| **Audio** | Every file ends up with a safe 2.0 track, source title preserved. See [the rules below](#how-the-stereo-track-is-made). `keep_stereo_only` drops the rest; off by default. Commentary and described audio are never folded down or dropped. | Every audio track copied, dispositions untouched. |
| **Subtitles** | Keeps the configured languages. If none match and there is exactly one unlabelled track, that one is kept. A text track the container cannot carry (`mov_text` into mkv) is converted to SubRip. | Every subtitle track copied, still converted if the container demands it. |
| **Output** | Container normalised to MKV (or `keep`), cover art dropped, original replaced once verified and inside the size window. | Nothing renamed or replaced. |
| **Notifications** | Calls your URLs once the verified file is in place. | Nothing is called. |

Chapters and metadata are always preserved.

## How the stereo track is made

Picking the wrong source is the failure that matters here, a film whose
default track is a director talking over it, so the choice is explicit rather
than scored:

1. **An existing 2.0 track wins.** Folding surround down when a stereo mix
   exists is a second lossy generation for nothing. Non-AAC is converted only
   when a configured AAC bitrate is below its known source bitrate, so 192k
   AC-3 becomes 128k AAC, while a source with no safe lower rung or no
   reported bitrate is copied unchanged.
2. **Otherwise, fold down the widest surround track** in
   `audio.preferred_languages` with a channel count in
   `audio.downmix_channels`. Ties break on bitrate, then stream index.
3. **Commentary is excluded**, by disposition (`comment`, `visual_impaired`)
   or by `audio.commentary_pattern`.
4. **If exclusion empties the list, nothing is downmixed.** The file is
   flagged for review instead of guessed at.

The fold-down uses the decoder (`-downmix stereo`) for codecs carrying their
own Lo/Ro coefficients (AC-3, E-AC-3, DTS, TrueHD), so the mix engineer's own
settings are applied. Anything else uses the standard matrix, normalised with
`aresample=rematrix_maxval=1.0` so the sum cannot clip. No hand-written pan
matrix, no centre-channel boost. libfdk_aac when the build has it, native
`aac` otherwise, both at constant bitrate.

## The panel

| Tab | What is there |
| --- | --- |
| **Dashboard** | Live counters, what is encoding with speed and ETA, the queue, last scan results, recent history. Buttons: *Scan now*, *Queue pending*, *Cancel all*, and a schedule toggle. |
| **Libraries** | One card per library: paths, its mode, its counts and reclaimed bytes. *Configure* edits routing inline. *Scan* scans just that one. |
| **Modes** | One card per mode: what it does, which libraries use it, all settings behind *Configure*. Editing one changes every library on it, and the card names them. |
| **Integrations** | One card per Sonarr/Radarr profile: its webhook URL to copy, whether its credentials are complete, a Test button, and every setting behind *Configure*. The AutoPulse destination and the API key live here too. Below them, **Imports**: every import a webhook has accepted, the stage it is in, and *Retry* on the ones that failed. |
| **Files** | Every tracked file, with search and filters. Click a row for the full plan, its state, past runs, and per-file actions. |
| **History** | Every run: before, after, percentage saved, how long. |
| **Settings** | **Database snapshots** - the backups on disk, *Back up now*, and *Restore* - followed by every global setting with its documentation, validated on save. |

Jobs report encode progress and copy-back progress separately. Only one file
is copied back at a time however many workers are running, because the library
is usually one network link and parallel copies only halve each other, so a
job waiting its turn says *waiting to copy*.

Every change made in the panel is written straight to `config.toml`.

## There is no command line

The daemon is the only thing the image runs, and the panel and its API are
the only way to drive it. Scanning a library, planning or processing a single
file, editing libraries, modes and integrations, taking and restoring a
backup: all of it is a button, and all of it is an endpoint listed under
[API](#api). One way to do a thing beats two that can disagree.

The *Check a file...* button on the Dashboard is the one that replaces a
terminal habit: give it any path inside a library and it plans that file, or
processes it, without waiting for a scan to notice it.

## Sonarr and Radarr

**Full walkthrough: [SETUP.md](SETUP.md).** The short version:

1. **Integrations tab → Add Sonarr / Add Radarr.** Name it, then fill in that
   application's url and api_key so the transcoder can ask it to rescan and
   rename. Press **Test** to prove the credentials.
2. **Copy the webhook URL from the card** into that Arr under
   Settings → Connect → Webhook, with **On Import** and **On Upgrade** ticked
   and the API key as an `X-Api-Key` header.
3. **Turn on the AutoPulse card** to hand the final path to Jellyfin.
4. **Remove any existing Arr-to-AutoPulse hook** for import events.

**The chain starts after the Arr import, deliberately.** With copy imports (no
hard links) only the library copy changes, so the torrent payload keeps
seeding. Do not put the transcoder between the download client and the Arr.

Each import runs three independent, durable stages:

```text
processing -> arr_reconcile -> autopulse -> complete
```

- A restart resumes the current stage.
- Failures back off, stay visible, and retry with `POST /api/workflow/retry`,
  **never re-running ffmpeg**.
- Duplicate webhook deliveries are deduplicated.
- If a file cannot be reconciled with the Arr, AutoPulse is not called with a
  stale filename.
- Test, Rename, Health and Grab events answer `ignored: true` and queue
  nothing.

### One-shot modes

An integration profile, or a `POST /api/process` call, can name a **mode** for
that file only:

| `mode` | Result |
| --- | --- |
| *(omitted)* | Whatever the file's library uses, exactly as a scan would. |
| `cleanup` | Audio, subtitles and container only. Seconds. |
| `standard` | The full x265 encode. |

It is never stored against the file, so the next scheduled scan plans it under
its library's own mode again. An import using `cleanup` gets the cheap wins
now and still queues for x265 later. An unknown mode is a 400, not a silent
full re-encode.

For simple setups that do not need Arr reconciliation, `POST /api/process`
queues a single file and returns immediately:

```sh
#!/bin/sh
# Sonarr: Settings > Connect > Custom Script, on Import and Upgrade.
# Radarr: use $radarr_moviefile_path instead.
curl -fsS -X POST http://transcoder:8080/api/process \
  -H "X-Api-Key: $TRANSCODER_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"$sonarr_episodefile_path\", \"mode\": \"cleanup\"}"
```

### The API key

Generated on first start, written to `config.toml`, shown on the Settings tab.
Send it as `X-Api-Key`, `Authorization: Bearer`, or `?apikey=`.

The panel embeds it so the UI can call its own API, which means anyone who can
load the panel can read it. **It authenticates Sonarr and Radarr. It does not
make the panel safe to expose.**

## Notifications

AutoPulse is the preferred path for imports. Modes also have generic webhooks
for everything else. Turn **Notifications** on for a mode and list URLs:

```toml
[modes.notify]
enabled = true
urls = ["http://jellyfin:8096/Library/Media/Updated"]
method = "POST"
headers = ["X-Api-Key: KEY"]
timeout = 15.0
retries = 3
```

They fire **only** after a processed file is verified and copied back. Never
for a file that needed no work, never for one that failed.

`POST` and `PUT` send a JSON body:

```json
{"event": "processed", "status": "done", "path": "/media/TV/Show/ep.mkv",
 "name": "ep.mkv", "stem": "ep", "dir": "/media/TV/Show",
 "original_path": "/media/TV/Show/ep.avi", "library": "tv", "mode": "cleanup",
 "in_size": 8203471290, "out_size": 3011225533, "saved": 5192245757,
 "elapsed": 1794.2, "reasons": ["..."], "at": 1757635200.0}
```

`GET`, `HEAD` and `DELETE` send no body, so put what the far end needs in the
URL. `{path}` `{name}` `{stem}` `{dir}` `{library}` `{mode}` `{status}` are
substituted and URL-encoded, in URLs and header values alike.

Delivery runs on one background thread, so a bulk import never holds up an
encode. Timeouts and 5xx retry with a growing delay; 4xx does not.
**Generic webhooks are best effort**: one that never succeeds is logged and
dropped, because the file on disk is already correct. The native
Arr/AutoPulse outbox is different, and keeps its failures for an explicit
retry.

*Test hooks* on the Libraries tab fires a sample payload, so a typo shows up
while you are looking at the panel rather than at 3am.

## Safety

Nothing overwrites a source file until the encode has been verified.

- Encodes go to the temp directory, never over the original.
- The output is re-probed: it must have a video stream, the expected stream
  count, and at least 98% of the source duration. That catches truncated
  encodes that still exit 0.
- The size must land inside the mode's window, `output.min_size_ratio` to
  `output.max_size_ratio` (30% to 110% by default). The ceiling catches an
  encode that did not pay off; the floor catches one that came back
  implausibly small. Set the floor to 0 or the ceiling to 10 to turn either
  off.
- The verified file is staged next to the original and moved in with
  `os.replace`, so a crash mid-copy cannot leave a half-written file.
- Three failed attempts and a file is left alone until you retry it.
- `docker stop` cancels running encodes cleanly and leaves originals intact.
- Scratch left by a `kill -9` or a reboot is swept on the next start. Anything
  not written by an encode is left alone.
- Deleting a library forgets its tracked state. No media files are touched.

## Backups

The database is the one thing here that cannot be rebuilt by rescanning, so
the daemon snapshots it daily into `config/backups/` and keeps the last seven:

```
config/backups/state-2026-01-05.db
config/backups/state-2026-01-06.db
config/backups/state-2026-01-07.db
```

- Taken through SQLite's backup API, so it is safe while encodes are running.
- Each file is self-contained. No `-wal` sidecar to copy with it.
- Named for the day, so a restart or a shorter interval refreshes that day's
  file instead of spending a retention slot. `keep = 7` means seven days.
- Settings under `[backup]`, or Backups in the settings panel: `enabled`,
  `interval_hours` (24), `keep` (7), `dir`.

Restore from **Settings -> Database snapshots -> Restore**. The daemon does
not have to be stopped: it refuses while anything is queued or encoding, and
otherwise closes the database, checks the snapshot opens, moves the current
one aside as `state.db.replaced-<timestamp>` rather than deleting it, and
reopens on the restored file. Imports the snapshot still owes are pushed back
onto the queue, exactly as a restart would.

Nothing is re-encoded because of a restore. The next scan re-probes what the
restored database does not remember, and planning is idempotent, so a
processed file settles straight back to `done`.

## Tuning throughput

`workers.count` x `workers.pools` should land near your host's CPU thread
count. x265 scales poorly past about 12 threads, so throughput comes from
several concurrent encodes rather than one very wide one.

| Host | Try |
| --- | --- |
| 8 threads | 2 workers x 4 pools (the default) |
| 24 threads | 4 workers x 6 pools |

Workers are shared across all libraries.

## State

SQLite, at `config/state.db`. A file is keyed by `(path, size, mtime)`, so an
unchanged file that already settled as `done` or `skip` is never re-probed and
rescans of a large library cost almost nothing.

```bash
sqlite3 config/state.db "select name, library, status, error from files where status='failed'"
sqlite3 config/state.db \
  "select sum(in_size - out_size)/1073741824.0 as gb_saved from history where status='done'"
```

## API

The panel is a client of a plain JSON API.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | unauthenticated liveness check |
| GET | `/api/status` | counters, active jobs, queue, libraries, recent history |
| GET | `/api/files` | `?status=&library=&q=&order=&limit=&offset=` |
| GET | `/api/file` | `?path=`, state, plan and run history |
| GET | `/api/history` | `?limit=&offset=` |
| GET | `/api/libraries` | each library with its schema and stats |
| POST | `/api/libraries/add` | `{"name": "TV", "paths": ["/media/TV"]}` |
| POST | `/api/libraries/update` | `{"id": "tv", "updates": {"mode": "cleanup"}}` |
| POST | `/api/libraries/delete` | `{"id": "tv"}` |
| GET | `/api/config` | global schema, values, rendered TOML |
| POST | `/api/config` | `{"updates": {"workers.pools": 8}}` |
| POST | `/api/scan` | `{"library": "tv"}` / `{"paths": [...]}`, both optional |
| GET | `/api/modes` | each mode, its schema, and the libraries using it |
| POST | `/api/modes/add` | `{"name": "Subs only", "copy_from": "cleanup"}` |
| POST | `/api/modes/update` | `{"id": "cleanup", "updates": {...}}` |
| POST | `/api/modes/delete` | `{"id": "cleanup"}` |
| POST | `/api/check` | `{"path": "...", "mode": null}`, plan one file |
| POST | `/api/process` | `{"path": "...", "mode": null, "force": false}` |
| POST | `/api/webhook/sonarr/<id>` | native Arr import hook |
| GET | `/api/integrations` | Arr profiles, AutoPulse, webhook URLs, schemas |
| POST | `/api/integrations/add` | `{"provider": "sonarr", "name": "TV"}` |
| POST | `/api/integrations/update` | `{"provider": "sonarr", "id": "tv", "updates": {...}}` |
| POST | `/api/integrations/delete` | `{"provider": "sonarr", "id": "tv"}` |
| POST | `/api/integrations/autopulse` | `{"updates": {"enabled": true}}` |
| POST | `/api/integrations/test` | `{"provider": "sonarr", "id": "tv"}`, checks credentials |
| GET | `/api/workflow` | `?status=&limit=`, accepted imports and their stages |
| POST | `/api/workflow/retry` | `{}` or `{"job_id": 12}`, no re-encode |
| POST | `/api/cancel` / `/api/cancel-all` | stop work |
| POST | `/api/queue-pending` | queue everything that needs work |
| POST | `/api/retry` | reset failures, optionally `{"path": "..."}` |
| POST | `/api/schedule` | `{"enabled": true}` |
| POST | `/api/notify/test` | `{"library": "tv", "mode": null}` |
| POST | `/api/history/clear` | wipe history, keep file state |
| GET | `/api/backups` | daily database snapshots, newest first |
| POST | `/api/backups/run` | take a snapshot now |
| POST | `/api/backups/restore` | `{"name": "state-2026-01-06.db"}`, refused while busy |

## Development

```bash
python -m unittest discover -s tests -v
python -m app -c ./config/config.toml      # the daemon, panel on :8080
```

Planning rules are pure functions over ffprobe output, so most of the suite
needs no media files. The API tests run a real server on an ephemeral port.

The suite asserts that planning the output of a previous run reports no work
to do, for every combination of stage switches: a library that keeps all audio
or skips subtitles must be as stable as one that does everything.

## Licence

MIT. See [LICENSE](LICENSE).
