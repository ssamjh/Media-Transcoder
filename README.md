# transcoder

A self-hosted media library transcoder. It scans your libraries on a schedule,
works out what each file actually needs, and only then touches it — driven
from a web panel or the command line.

**A library says where files are; a mode says what happens to them.** TV can
be treated with a mode that re-encodes to x265 and trims subtitles to English,
while Movies uses one that leaves the video alone and only cleans the audio,
and Home Video is scanned but never touched. Every stage — video, audio,
subtitles, container, replacement — has its own switch, and any number of
libraries can share one mode, so two libraries that should behave the same are
configured in one place rather than two that drift apart.

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
| **Video** | SD (≤576p) → x265 CRF 23, 720p → CRF 23, 1080p → CRF 22, each band with its own configurable CRF. Already-HEVC is copied, never re-encoded. Above 1200p left alone entirely. | Every video stream copied untouched, and the height ceiling no longer applies — a 4K file still gets its audio and subtitles cleaned. |
| **Audio** | Every file gets a safe 2.0 default while preserving the source track title. An existing stereo track is converted to AAC only at a configured bitrate below its known source rate; otherwise it is copied, avoiding a larger lossy transcode. A file with no stereo track gets AAC folded down from its widest surround mix at `audio.stereo_bitrate`. `keep_stereo_only` then drops the other tracks; off (the default) keeps them all. Commentary and described audio are never folded down and never dropped. | Every audio track copied untouched, dispositions left alone. |
| **Subtitles** | Keeps configured languages only. If none match and there is exactly one *unlabelled* track, that one is kept. A text track the target container cannot carry — `mov_text` from an MP4, say — is converted to SubRip rather than copied, since Matroska refuses it at the muxer. | Every subtitle track copied, converted to SubRip if the container demands it. |
| **Output** | Container normalised to MKV (or `keep` to leave the extension alone), cover art dropped, originals replaced once verified and inside the size window (30%–110% of the source by default). The 110% ceiling is only asked of a run that re-encoded the video; one rejected by it is rebuilt around the source video stream so the audio and subtitle work still lands. | — |
| **Notifications** | Once the verified file is back in place, calls the URLs you list — Jellyfin, Plex, anything with an HTTP endpoint. Queued and delivered in the background. | Nothing is called. |

Chapters and metadata are always preserved.

### How the stereo track is made

Picking the wrong source is the failure that matters here — a film whose
default track is a director talking over it — so the choice is explicit rather
than scored:

1. A 2.0 track already in the file wins. Folding the surround mix down when a
   stereo mix exists is a second lossy generation for nothing. If it is not
   AAC, it is converted only when a configured AAC bitrate is below its known
   source bitrate. A 192k AC-3 track therefore becomes 128k AAC; a source with
   no safe lower rung, or no reported bitrate, is copied unchanged.
2. Otherwise, candidates are tracks in `audio.preferred_languages` with a
   channel count in `audio.downmix_channels` (6 or 8). Anything carrying the
   `comment` or `visual_impaired` disposition, or a title matching
   `audio.commentary_pattern`, is excluded. Widest mix wins, then highest
   bitrate, then lowest stream index.
3. If exclusion empties the candidate list, **nothing is downmixed**. The file
   is logged and flagged for review instead of guessed at.

The fold-down itself uses the decoder (`audio.downmix_request`, `-downmix
stereo`) for codecs that carry their own Lo/Ro coefficients — AC-3, E-AC-3,
DTS, TrueHD — so the mix engineer's own settings are applied. Anything else
falls back to the standard matrix, normalised with
`aresample=rematrix_maxval=1.0` so the sum cannot clip. There is no hand-written
pan matrix and no centre-channel boost. libfdk_aac is used when the ffmpeg
build has it and the native `aac` encoder otherwise; both at constant bitrate.

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

The dashboard updates quickly while work is active. Each job keeps its encode
progress separate from the copy-back progress, including copy percentage,
bytes, MB/s, and ETA; a job waiting for the serialized library copy is shown as
waiting for its copy slot.

**Dashboard** — live counters, currently-encoding files with progress, speed
and ETA, the queue, the last scan's per-library results, and recent history.
Buttons for *Scan now*, *Queue pending*, *Cancel all*, and a toggle for
periodic scans.

**Libraries** — one card per library showing its paths, the mode it is
treated with, and its own counts and reclaimed bytes. *Configure* expands its
routing settings inline — paths, extensions, exclusions, size floor and which
mode to use; the mode button opens the mode itself, since that is where the
stages live. *Scan* scans just that library; the checkbox includes or excludes
it from scans, and starts unticked on a new library. *Add library* takes a name
and paths — overlapping paths are rejected, since a file under two libraries
would have an ambiguous profile.

**Modes** — one card per mode: what it does, which libraries run on it, and the
full set of settings behind *Configure*. Editing one changes every library
using it, which is the point; the card names them so it is never a surprise.
*Add mode* starts from a copy of an existing mode, and a mode in use cannot be
deleted until the libraries on it are pointed elsewhere.

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
save and written back to `config.toml`. What happens to files lives under
*Modes*, and where they are lives under *Libraries*. *Integration* shows the
API key and a ready-made script for Sonarr and Radarr.

Every change made in the panel is written straight to `config.toml`.

## Command line

```bash
docker compose run --rm transcoder libraries
docker compose run --rm transcoder libraries --add "TV" --path /media/TV
docker compose run --rm transcoder libraries -L tv --set mode=cleanup
docker compose run --rm transcoder libraries -L tv --set min_size_mb=200
docker compose run --rm transcoder modes -m standard --set video.crf_1080p=21
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

The recommended chain is:

```text
download client -> Sonarr/Radarr import -> Media-Transcoder
                -> Arr rescan + targeted rename -> AutoPulse -> Jellyfin
```

This deliberately starts **after the Arr import**. With copy imports (no hard
links), Media-Transcoder changes only the library copy; the torrent payload
stays untouched and can continue seeding. Do not put the transcoder between
the download client and Arr, because then Arr has no settled, managed file to
identify and recover.

Create one Webhook connection in each Arr application pointing to
`POST /api/webhook/sonarr/<id>` or `POST /api/webhook/radarr/<id>`, and select
**On Import** and **On Upgrade**. Remove the direct Arr-to-AutoPulse hook for
those events: Media-Transcoder calls AutoPulse only after the file and its Arr
name are final.

The native endpoint commits the request to SQLite before acknowledging it.
The durable stages are independent:

1. Process the imported library file (or record that it needed no work).
2. If it changed, ask Arr to rescan the series/movie and rename only that file.
3. Send the final path to AutoPulse's manual trigger.

A restart resumes the current stage. Arr and AutoPulse failures use bounded
backoff and remain visible as failed work; retry them with
`POST /api/workflow/retry` without running FFmpeg again. Duplicate webhook
deliveries are deduplicated. If a changed file cannot be reconciled with Arr,
AutoPulse is not called with a stale filename.

The endpoint accepts Arr's native `Download` payload, including upgrades, and
extracts the series/movie id, file id, and final path. Rename, Test, Health,
Grab, and other non-import events are acknowledged with `ignored: true` and do
not enqueue work. Send the configured key as `X-Api-Key` (or use an
`Authorization: Bearer ...` header).

When more than one instance of a provider is configured, address the named
profile explicitly. The `instanceName` in an Arr payload is also matched to
the configured profile name. A profile selects a one-shot mode, supplies the
outbound Arr connection, and may have its own inbound `secret`:

```toml
[[integrations.sonarr]]
id = "tv"
name = "TV Sonarr"
mode = "cleanup"
url = "http://sonarr:8989"
api_key = "sonarr-api-key"
path_from = "/arr/media"
path_to = "/media"
request_timeout = 15.0
command_timeout = 300.0
poll_interval = 2.0
max_retries = 3
secret = "replace-with-a-long-random-value"

[[integrations.radarr]]
id = "movies"
name = "Movies Radarr"
mode = "cleanup"
url = "http://radarr:7878"
api_key = "radarr-api-key"
path_from = "/arr/media"
path_to = "/media"
request_timeout = 15.0
command_timeout = 300.0
poll_interval = 2.0
max_retries = 3
secret = "replace-with-a-long-random-value"

[integrations.autopulse]
enabled = true
url = "http://autopulse:2875"
username = "autopulse-user"
password = "autopulse-password"
trigger_endpoint = "/triggers/manual"
timeout = 15.0
max_retries = 3
```

`path_from` is the prefix Arr writes into its webhook/API paths; `path_to` is
the same directory as mounted in Media-Transcoder. Set both or neither. The
Arr `url` and `api_key` are required for the rescan/rename stage. AutoPulse is
optional; when disabled, the workflow completes after Arr reconciliation.
Secrets are stored as ordinary TOML strings and no third-party package is
required. Keep the panel on a trusted network: its global API key is embedded
in the panel page and authenticates integrations rather than making the panel
an internet-safe boundary.

For simple integrations that do not need durable Arr reconciliation,
`POST /api/process` still queues a single file. The legacy custom-script hook
returns immediately, but it does not carry the Arr identity required by the
rescan/rename workflow.

```sh
#!/bin/sh
# Sonarr: Settings > Connect > Custom Script, on Import and Upgrade.
# Radarr: use $radarr_moviefile_path instead.
curl -fsS -X POST http://transcoder:8080/api/process \
  -H "X-Api-Key: $TRANSCODER_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"$sonarr_episodefile_path\", \"mode\": \"cleanup\"}"
```

**`mode` picks what actually gets done** — the same modes the Modes tab
defines, naming one for this file instead of whatever its library normally
uses:

| Mode | What it does |
| --- | --- |
| *(omitted)* | The mode the file's library uses, exactly as a scan would. |
| `standard` | Re-encode to x265, best audio plus a stereo downmix, subtitles trimmed, container normalised. |
| `cleanup` | Everything except re-encoding video — audio and subtitles cleaned, container normalised, every video stream copied. Seconds rather than hours. |

Naming a mode is **one-shot**. It is never stored against the file, so the next
scheduled scan plans that file under its library's own mode again. An import
hook using `cleanup` gets the cheap wins immediately, and the file still queues
for its x265 encode on the next scan. An unknown mode is a 400, not a silent
full re-encode.

Define your own on the Modes tab, or from the command line. A new mode starts
as a copy of an existing one, and is validated when it is saved rather than
when a hook fires:

```bash
docker compose run --rm transcoder modes --add "Subs only" --copy-from cleanup
docker compose run --rm transcoder modes -m subs-only \
  --set audio.enabled=false --set video.enabled=false
```

### Telling Jellyfin afterwards

The durable AutoPulse hand-off above is the preferred import path. Modes also
have generic notification hooks for unrelated services and scheduled work.
Turn **Notifications** on for a mode and list one URL per line:

```toml
[modes.notify]
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
effort**: a generic mode webhook that never succeeds is logged and dropped.
This does not apply to the native Arr/AutoPulse outbox, whose failure is kept
for an explicit retry. The file on
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
- the result is discarded unless its size lands inside the mode's window,
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
| GET | `/health` | unauthenticated container liveness check |
| GET | `/api/status` | counters, active jobs, queue, libraries, recent history |
| GET | `/api/files` | `?status=&library=&q=&order=&limit=&offset=` |
| GET | `/api/file` | `?path=` — state, plan and run history |
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
