# transcoder

A self-hosted media library transcoder. It scans your libraries on a schedule,
works out what each file actually needs, and only then touches it — driven
from a web panel or the command line.

**Each library has its own processing profile.** TV can be re-encoded to x265
with subtitles trimmed to English, while Movies is left at its original codec
and has only its audio cleaned, and Home Video is scanned but never touched.
Every stage — video, audio, subtitles, container, replacement — has its own
switch.

- **Python 3.14 on Debian 13**, ffmpeg 7.x, **zero Python dependencies** —
  config is read with the stdlib `tomllib` and the panel is served by
  `http.server`, so there is nothing to `pip install` and nothing to drift.
- **Idempotent by design.** A file already processed plans out as "nothing to
  do" on every subsequent scan, so re-running is free and the library never
  churns. There are tests that assert exactly this, for every combination of
  stage switches.
- **Nothing is overwritten until it verifies.**

## Processing stages

Each is independently switchable per library.

| Stage | When enabled | When disabled |
| --- | --- | --- |
| **Video** | 720p → x265 CRF 23, 1080p → CRF 22. Already-HEVC is copied, never re-encoded. SD (≤576p) cleaned but not re-encoded. Above 1200p left alone entirely. | Every video stream copied untouched, and the height ceiling no longer applies — a 4K file still gets its audio and subtitles cleaned. |
| **Audio** | Keeps the best track — preferred language, not commentary, sane channel layout, widely supported codec — plus an AAC 2.0 downmix as default, titled `Stereo` (`audio.stereo_title`) whether it was encoded or adopted. An existing stereo track is re-used, not rebuilt. `keep_best_only` and `add_stereo_downmix` are separate switches. | Every audio track copied untouched, dispositions left alone. |
| **Subtitles** | Keeps configured languages only. If none match and there is exactly one *unlabelled* track, that one is kept. | Every subtitle track copied untouched. |
| **Output** | Container normalised to MKV (or `keep` to leave the extension alone), cover art dropped, originals replaced once verified and inside the size window (30%–110% of the source by default). | — |
| **Notifications** | Once the verified file is back in place, calls the URLs you list — Jellyfin, Plex, anything with an HTTP endpoint. Queued and delivered in the background. | Nothing is called. |

Chapters and metadata are always preserved.

## Quick start

Point the volumes in `compose.yml` at your media, set `PUID`/`PGID` to
whoever owns it, and start:

```bash
stat -c '%u:%g' /mnt/nfs/media     # the ids to put in PUID/PGID
docker compose up -d --build
```

The container starts as root, moves its own user onto `PUID`/`PGID`, takes
ownership of what it writes to, and drops to that user before running
anything — the same arrangement as the linuxserver.io images, so no host
directory needs chowning by hand.

Encodes are written to `/tmp/transcoder`, mounted from `./transcode_cache`
next to the compose file. It needs room for one in-progress encode per worker
— about the size of the source file, times `workers.count` — which is why it
is a real directory and not the container's own filesystem. Point it at
another disk if the one you deploy from is short of space, and keep it off
network shares, which an encode reads and writes constantly.

Startup checks the config and scratch directories and stops with the offending
path if either is unwritable. The media tree is the one thing that cannot be
checked in advance — it is found to be read-only only when a finished encode
is moved into place, so `PUID`/`PGID` want to match its owner.

The panel is on <http://localhost:8080>. On first start it writes a fully
commented `config/config.toml` and generates an API key for
[Sonarr and Radarr](#sonarr-and-radarr). No libraries are configured, so
nothing is scanned until you add one — *Add library* in the Libraries tab, or
`transcoder libraries --add`. A new library arrives switched off; tick it on
when its profile looks right.

> **The panel itself has no authentication.** The API key guards `/api/`, but
> the panel is served with that key embedded so the UI can use it — anyone who
> can load the page can read it. The panel can start encodes and rewrite the
> config, so keep it on a trusted network — behind a reverse proxy with auth,
> or bound to localhost. Do not expose it to the internet.

**Nothing is encoded until you ask for it.** A new library is created switched
off, a scan only plans and reports, and what it finds waits in `pending` until
you press *Queue pending* — so the intended first run is: add the library,
configure its profile, tick it on, scan, read the Files tab, then queue. Turn
on `schedule.process_after_scan` once you trust it and every scan queues its
own work. Setting a library's `output.replace_original` to false makes it
encode and then discard, which is a good way to check settings against real
files.

## The panel

**Dashboard** — live counters, currently-encoding files with progress, speed
and ETA, the queue, the last scan's per-library results, and recent history.
Buttons for *Scan now*, *Queue pending*, *Cancel all*, and a toggle for
periodic scans.

**Libraries** — one card per library showing its paths, which stages are on,
and its own counts and reclaimed bytes. *Configure* expands the full profile
inline; *Scan* scans just that library; the checkbox includes or excludes it
from scans, and starts unticked on a new library. *Add library* takes a name
and paths — overlapping paths are rejected, since a file under two libraries
would have an ambiguous profile.

The dashboard shows the stage each job is in: *encoding* with its speed as a
multiple of realtime, then *copying* with MB/s as the verified file is put back
on the library. Only one file is copied back at a time however many workers are
running — the library is usually one network link, and parallel copies only
halve each other — so a job waiting its turn says *waiting to copy*.

**Files** — every tracked file with search, and filters for library and
status. Click a row for the detail drawer: the full plan (every output stream,
what is dropped and why), its state, past runs, and per-file actions —
*Re-check*, *Process now*, *Force retry*, *Cancel*.

**History** — every run: before, after, percentage saved, how long it took.

**Settings** — the global settings, with their documentation, validated on
save and written back to `config.toml`. Per-library settings live on the
Libraries tab. Below them, *Processing modes* defines the named overrides the
API accepts, and *Integration* shows the API key and a ready-made script for
Sonarr and Radarr.

Every change made in the panel is written straight to `config.toml`.

## Command line

```bash
docker compose run --rm transcoder libraries
docker compose run --rm transcoder libraries --add "TV" --path /media/TV
docker compose run --rm transcoder libraries -L tv --set subtitles.enabled=false
docker compose run --rm transcoder libraries -L tv --set video.crf_1080p=21
docker compose run --rm transcoder libraries -L old --remove

docker compose run --rm transcoder scan               # report, touch nothing
docker compose run --rm transcoder scan -L tv         # one library
docker compose run --rm transcoder scan --no-cache    # re-probe everything
docker compose run --rm transcoder check "/media/TV/Show/S01E01.mkv"
docker compose run --rm transcoder run                # scan, then process
docker compose run --rm transcoder run --dry-run      # log, encode nothing
docker compose run --rm transcoder process "/media/TV/Show/S01E01.mkv"
docker compose run --rm transcoder status --failed
docker compose run --rm transcoder config --set workers.pools=8
```

```bash
docker compose run --rm transcoder modes
docker compose run --rm transcoder modes --add "Audio only"
docker compose run --rm transcoder modes -m audio-only --set video.enabled=false
docker compose run --rm transcoder modes -m audio-only --unset video.enabled
docker compose run --rm transcoder check -m cleanup "/media/TV/Show/S01E01.mkv"
docker compose run --rm transcoder process -m cleanup "/media/TV/Show/S01E01.mkv"
```

`scan`, `check`, `libraries` and `modes` take `--json` for scripting.

```
File        Library  Res   Video         Audio   Subs    Size
----------  -------  ----  ------------  ------  ------  ------
sample.mkv  Movies   720p  h264 copy     2 kept  2 kept  579 KB
sample.mkv  TV       720p  h264 -> x265  2 kept  1 kept  579 KB

  sample.mkv
      - drop extra audio
      - add aac stereo downmix
      x drop audio ac3 6ch fre
  sample.mkv
      - encode video h264 -> x265 crf 23
      - drop extra audio
      - add aac stereo downmix
      - drop unwanted subtitle
      x drop audio ac3 6ch fre
      x drop subtitle subrip jpn
```

## Sonarr and Radarr

`POST /api/process` queues a single file, which is the call to make from a
Custom Script on import. It returns immediately — encoding happens on the
workers, so the hook never blocks an import.

```sh
#!/bin/sh
# Sonarr: Settings > Connect > Custom Script, on Import and Upgrade.
# Radarr: use $radarr_moviefile_path instead.
curl -fsS -X POST http://transcoder:8080/api/process \
  -H "X-Api-Key: $TRANSCODER_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"$sonarr_episodefile_path\", \"mode\": \"cleanup\"}"
```

**`mode` picks what actually gets done.** It names a set of overrides on top
of the owning library's profile:

| Mode | What it does |
| --- | --- |
| *(omitted)* | The library's own profile, exactly as a scan would. |
| `all` | The same thing, named explicitly. |
| `cleanup` | Everything except re-encoding video — audio and subtitles cleaned, container normalised, every video stream copied. Seconds rather than hours. |

A mode is **one-shot**. It is never stored against the file, so the next
scheduled scan plans that file under its library's normal profile again. An
import hook using `cleanup` gets the cheap wins immediately, and the file
still queues for its x265 encode on the next scan. An unknown mode is a 400,
not a silent full re-encode.

Define your own on the Settings tab, or from the command line. Overrides use
the same dotted keys as a library's settings, and are validated when the mode
is saved rather than when a hook fires:

```bash
docker compose run --rm transcoder modes --add "Subs only"
docker compose run --rm transcoder modes -m subs-only \
  --set video.enabled=false --set audio.enabled=false
```

### Telling Jellyfin afterwards

Sonarr calls the transcoder on import; the transcoder calls whoever is next.
Turn **Notifications** on for a library and list one URL per line:

```toml
[libraries.notify]
enabled = true
urls = ["http://jellyfin:8096/Library/Media/Updated?api_key=KEY"]
method = "POST"
headers = ["X-Api-Key: KEY"]
timeout = 15.0
retries = 3
```

The calls fire **only after** a processed file has been verified and copied
back over the original — never for a file that needed no work, and never for
one that failed. `POST` and `PUT` carry the details as a JSON body:

```json
{"event": "processed", "status": "done", "path": "/media/TV/Show/ep.mkv",
 "name": "ep.mkv", "stem": "ep", "dir": "/media/TV/Show",
 "original_path": "/media/TV/Show/ep.avi", "library": "tv", "mode": "cleanup",
 "in_size": 8203471290, "out_size": 3011225533, "saved": 5192245757,
 "elapsed": 1794.2, "reasons": ["..."], "at": 1757635200.0}
```

`GET`, `HEAD` and `DELETE` send no body, so put what the far end needs in the
URL: `{path}` `{name}` `{stem}` `{dir}` `{library}` `{mode}` `{status}` are
substituted and URL-encoded, in the URL and in header values alike.

Everything is queued on one background thread, so a bulk import that finishes
twenty files at once queues sixty calls and drains them without holding up a
single encode. Timeouts, connection errors and 5xx are retried with a growing
delay; a 4xx is not, because it will not start working. **Delivery is best
effort**: a webhook that never succeeds is logged and dropped. The file on
disk is already correct, and the transcoder's state must not depend on
somebody else answering.

A **mode** can override all of this for one request, since `notify.*` are
ordinary library keys — so an import hook can add a callback that scheduled
scans do not make:

```bash
docker compose run --rm transcoder modes --add "Import"
docker compose run --rm transcoder modes -m import   --set video.enabled=false   --set notify.enabled=true   --set notify.urls=http://jellyfin:8096/Library/Media/Updated?api_key=KEY
```

*Test hooks* on the Libraries tab fires a sample payload at the configured
URLs so a typo shows up while you are looking at the panel, not at 3am.

### The API key

One is generated on first start, written to `config.toml`, and shown on the
Settings tab under *Integration* along with a ready-made script. Send it as
an `X-Api-Key` header, an `Authorization: Bearer` header, or an `?apikey=`
parameter.

The panel is served with the key embedded so the UI can call its own API,
which means anyone who can load the panel can read it. **It authenticates
Sonarr and Radarr; it does not make the panel safe to expose.** Keep it on a
trusted network regardless.

## Safety

Nothing overwrites a source file until the encode has been verified:

- the encode goes to the temp directory, never over the original
- the output is re-probed — it must have a video stream, the expected stream
  count, and at least 98% of the source duration, which catches truncated
  encodes that still exit 0
- the result is discarded unless its size lands inside the library's window,
  `output.min_size_ratio` to `output.max_size_ratio` (30%–110% of the source by
  default). The ceiling catches an encode that did not pay off, allowing a
  little room for the stereo track a run may have added; the floor catches one
  that came back implausibly small. Set the floor to 0 or the ceiling to 10 to
  turn either off
- the verified file is staged next to the original and moved into place with
  `os.replace`, so a crash mid-copy can never leave a half-written file
- three failed attempts and a file is left alone until you retry it
- `docker stop` cancels running encodes cleanly and leaves originals intact
- a work directory left in the scratch space by a kill -9 or a host reboot is
  swept on the next start; anything not written by an encode is left alone
- deleting a library forgets its tracked state; no media files are touched.
  Deleting the last one is allowed: with no libraries there is simply nothing
  to scan

## Tuning throughput

`workers.count` × `workers.pools` should land near your host's CPU thread
count. x265 scales poorly past about 12 threads, so throughput comes from
running several concurrent encodes rather than one very wide one. The default
2 × 4 suits an ordinary 8-thread host; on a 24-thread box, 4 workers × 6 pool
threads is a good starting point. Workers are shared across all libraries.

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
| GET | `/api/status` | counters, active jobs, queue, libraries, recent history |
| GET | `/api/files` | `?status=&library=&q=&order=&limit=&offset=` |
| GET | `/api/file` | `?path=` — state, plan and run history |
| GET | `/api/history` | `?limit=&offset=` |
| GET | `/api/libraries` | each library with its schema and stats |
| POST | `/api/libraries/add` | `{"name": "TV", "paths": ["/media/TV"]}` |
| POST | `/api/libraries/update` | `{"id": "tv", "updates": {"video.enabled": false}}` |
| POST | `/api/libraries/delete` | `{"id": "tv"}` |
| GET | `/api/config` | global schema, values, rendered TOML |
| POST | `/api/config` | `{"updates": {"workers.pools": 8}}` |
| POST | `/api/scan` | `{"library": "tv"}` / `{"paths": [...]}`, both optional |
| GET | `/api/modes` | each mode with its overrides and schema |
| POST | `/api/modes/add` | `{"name": "Subs only", "overrides": {...}}` |
| POST | `/api/modes/update` | `{"id": "cleanup", "updates": {...}}` |
| POST | `/api/modes/delete` | `{"id": "cleanup"}` |
| POST | `/api/check` | `{"path": "...", "mode": null}` — plan one file |
| POST | `/api/process` | `{"path": "...", "mode": null, "force": false}` |
| POST | `/api/cancel` / `/api/cancel-all` | stop work |
| POST | `/api/queue-pending` | queue everything that needs work |
| POST | `/api/retry` | reset failures, optionally `{"path": "..."}` |
| POST | `/api/schedule` | `{"enabled": true}` |
| POST | `/api/notify/test` | `{"library": "tv", "mode": null}` — fire a sample webhook |
| POST | `/api/history/clear` | wipe history, keep file state |

## Development

```bash
python -m unittest discover -s tests -v
python -m app -c ./config/config.toml scan
```

The planning rules are pure functions over ffprobe output, so most of the
suite needs no media files. The API tests run a real server on an ephemeral
port. Notably, the suite asserts that planning the output of a previous run
reports no work to do — for every combination of stage switches, since a
library that keeps all audio or skips subtitles must be just as stable as one
that does everything.

## Licence

MIT. See [LICENSE](LICENSE).
