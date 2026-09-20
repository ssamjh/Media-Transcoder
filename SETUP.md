# Setting up with Sonarr, Radarr, AutoPulse and Jellyfin

This is the deployment Media-Transcoder is built for: Sonarr and Radarr import
a release, the transcoder processes the imported file, the Arr is told to
rescan and rename it, and AutoPulse hands the final path to Jellyfin.

```text
download client -> Sonarr/Radarr import -> Media-Transcoder
                -> Arr rescan + targeted rename -> AutoPulse -> Jellyfin
```

**The chain starts after the Arr import, deliberately.** With copy imports (no
hard links) the transcoder only ever changes the library copy, so the torrent
payload stays untouched and keeps seeding. Putting the transcoder between the
download client and the Arr leaves the Arr with no settled, managed file to
identify. Don't.

Order of work below: containers → libraries and modes → Arr webhooks →
outbound Arr credentials → AutoPulse → Jellyfin → verify.

---

## 1. Paths every container must agree on

The one thing that breaks this setup more than anything else is the media path
being spelled differently in each container. The transcoder receives a path
from the Arr webhook and has to open that file itself.

The simplest arrangement is to mount the media at the **same path everywhere**:

```yaml
# sonarr, radarr, and transcoder alike
volumes:
  - /mnt/nfs/media:/media
```

If the Arrs already see the library somewhere else, don't remount them. Map
the prefix on the transcoder side instead, with `path_from` (what the Arr
writes) and `path_to` (where the transcoder sees it), covered in step 4.

Ownership matters too, because the transcoder replaces files in place. Set
`PUID`/`PGID` to whoever owns the media on the host:

```bash
stat -c '%u:%g' /mnt/nfs/media
```

```yaml
services:
  transcoder:
    container_name: transcoder
    build: .
    restart: unless-stopped
    ports:
      - 8080:8080
    environment:
      - TZ=Pacific/Auckland
      - TRANSCODER_CONFIG=/config/config.toml
      - PUID=1000
      - PGID=1000
    volumes:
      - ./config:/config
      - /mnt/nfs/media:/media
      - ./transcode_cache:/tmp/transcoder   # real disk, one encode per worker
    cpus: 8
    mem_limit: 4g
```

Put the transcoder on the same Docker network as Sonarr, Radarr and AutoPulse
so they can reach each other by container name. Start it once to generate the
config and an API key:

```bash
docker compose up -d --build
grep api_key config/config.toml      # the key the Arrs will send
```

> The panel has no authentication and the API key is embedded in the page it
> serves, so anyone who can load the panel can read it. That key authenticates
> Sonarr and Radarr; it does not make the panel safe to expose. Keep it on a
> trusted network.

## 2. Libraries and modes

A **library** says where files are; a **mode** says what happens to them. Add
one library per Arr root, in the Libraries tab or from the CLI:

```bash
docker compose run --rm transcoder libraries --add "TV" --path /media/TV
docker compose run --rm transcoder libraries --add "Movies" --path /media/Movies
```

A new library arrives **disabled**. Tick it on once its mode looks right.
Libraries are what scheduled scans walk; the webhook path works even for a file
outside every library, but its tracked state belongs to a library, so add them.

Two modes ship by default:

| Mode | What it does | Typical use |
| --- | --- | --- |
| `standard` | x265 re-encode, best audio plus an AAC stereo downmix, subtitles trimmed, container normalised. | Scheduled scans, catching up on a back catalogue. |
| `cleanup` | Everything *except* re-encoding video. Seconds, not hours. | On import, so a new episode is playable-everywhere immediately. |

The recommended split is **`cleanup` on import, `standard` on the schedule**.
Naming a mode on an import is one-shot: it is never stored against the file, so
the next scheduled scan still plans that file under its library's own mode and
the x265 encode happens later, off the import path.

**Nothing is encoded until you ask for it.** A scan only plans; what it finds
waits in `pending` until you press *Queue pending*. Turn on
`schedule.process_after_scan` once you trust the result.

## 3. The webhook in Sonarr and Radarr

In each Arr: **Settings → Connect → + → Webhook**.

| Field | Value |
| --- | --- |
| Name | `Media-Transcoder` |
| Triggers | **On Import** and **On Upgrade** only |
| URL | `http://transcoder:8080/api/webhook/sonarr/tv` (Radarr: `.../radarr/movies`) |
| Method | POST |
| Headers | `X-Api-Key: <the key from config.toml>` |

`Authorization: Bearer <key>` works too. The last path segment (`tv`,
`movies`) is the integration profile id from step 4. With only one profile per
provider configured you can use the bare `/api/webhook/sonarr`, and Arr's own
`instanceName` is also matched against a profile's name.

Arr's **Test** button sends a `Test` event: it is acknowledged with
`ignored: true` and queues nothing, which is the correct result. Rename,
Health and Grab events are ignored the same way. Only import, download,
upgrade and manual-import events enqueue work.

The endpoint commits the request to SQLite *before* acknowledging it, so a
restart mid-encode resumes rather than loses the job, and a redelivered webhook
is deduplicated instead of encoding twice.

If you already have an Arr → AutoPulse webhook for import events, **remove
it**. The transcoder calls AutoPulse itself, after the file and its Arr name
are final; leaving the old one in place tells Jellyfin about a filename that is
about to change.

## 4. Outbound Arr credentials (rescan and rename)

The webhook is inbound. The rescan/rename stage is outbound, and needs each
Arr's URL and API key (**Settings → General → API Key** in the Arr). Add one
profile per Arr instance to `config/config.toml`:

```toml
[[integrations.sonarr]]
id = "tv"                     # the <id> in /api/webhook/sonarr/<id>
name = "TV Sonarr"            # also matched against Arr's instanceName
enabled = true
mode = "cleanup"              # one-shot mode for files from this Arr
url = "http://sonarr:8989"
api_key = "sonarr-api-key"
path_from = ""                # Arr-side prefix, if it differs
path_to = ""                  # transcoder-side prefix
request_timeout = 15.0
command_timeout = 300.0       # how long to wait for a rescan/rename command
poll_interval = 2.0
max_retries = 3
secret = ""                   # optional per-profile inbound key

[[integrations.radarr]]
id = "movies"
name = "Movies Radarr"
enabled = true
mode = "cleanup"
url = "http://radarr:7878"
api_key = "radarr-api-key"
```

- `url` and `api_key` are **required** for the rescan/rename stage. Without
  them the job fails at `arr_reconcile` and AutoPulse is never called with a
  stale name.
- `path_from`/`path_to` map the Arr's view of the library onto the
  transcoder's. Set **both or neither**. Config validation rejects one alone.
  With identical mounts, leave both empty.
- `mode` is the one-shot mode applied to files arriving from this Arr.
- `secret` lets an Arr authenticate with its own credential instead of the
  global key.
- A 4K Radarr and an HD Radarr are two profiles, two webhook URLs, and can name
  different modes.

Restart after editing the file by hand: `docker compose restart transcoder`.

## 5. AutoPulse

AutoPulse is what actually pokes Jellyfin. One block, shared by every profile:

```toml
[integrations.autopulse]
enabled = true
url = "http://autopulse:2875"
username = "autopulse-user"
password = "autopulse-password"
trigger_endpoint = "/triggers/manual"
timeout = 15.0
max_retries = 3
```

The transcoder calls `GET /triggers/manual?path=<final path>` with HTTP basic
auth, using the path **after** the Arr rename, which is the point of doing it
in this order. The `path` AutoPulse receives is the transcoder's path, so
AutoPulse's own rewrite rules must map it to what Jellyfin sees (the same
concern as step 1, one hop further along).

AutoPulse is optional. With `enabled = false` the workflow finishes after the
Arr reconciliation, and you can point Jellyfin at the file some other way,
see the next step.

## 6. Jellyfin

With AutoPulse in the chain, Jellyfin needs nothing: AutoPulse triggers the
scan. Configure the Jellyfin target in AutoPulse's own config.

**Without AutoPulse**, call Jellyfin directly from the mode's notification
hooks. These fire only after a processed file has been verified and copied back
over the original. Never for a file that needed no work, and never for one
that failed:

```toml
[modes.notify]
enabled = true
urls = ["http://jellyfin:8096/Library/Media/Updated"]
method = "POST"
headers = ["X-Api-Key: jellyfin-api-key"]
timeout = 15.0
retries = 3
```

Notifications are part of the **mode**, so the import mode and the scheduled
mode can notify differently. Delivery is best effort on its own thread: 5xx and
timeouts retry with backoff, 4xx does not, and a hook that never succeeds is
logged and dropped, because the file on disk is already correct.

Test one without encoding anything: *Send test* in the panel, or
`POST /api/notify/test` with `{"library": "tv"}`.

## 7. Check it works

```bash
curl -s http://transcoder:8080/health                       # no key needed
curl -s -H "X-Api-Key: $KEY" http://transcoder:8080/api/status | jq .workflow
```

`workflow` counts durable jobs and outbox actions by status. A healthy import
moves through the stages:

```text
processing -> arr_reconcile -> autopulse -> complete
```

Then, end to end: press **Test** in the Arr (expect `ignored: true`), import
or re-import one episode, and watch it appear in the panel's queue, land back
in the library renamed by the Arr, and show up in Jellyfin.

## When something fails

Failures are visible and retryable, and **a retry never re-runs FFmpeg**. The
encode and the remote calls have separate lifetimes on purpose.

```bash
docker compose logs -f transcoder
curl -s -X POST -H "X-Api-Key: $KEY" http://transcoder:8080/api/workflow/retry \
  -H 'Content-Type: application/json' -d '{}'          # or {"job_id": 12}
```

| Symptom | Cause to check first |
| --- | --- |
| Webhook returns 401 | `X-Api-Key` doesn't match `web.api_key` (or the profile's `secret`). |
| Webhook returns 400, "no final file path" / "no series id" | Not an import event, or a custom payload. Use Arr's own Webhook connection, not a custom script. |
| Job fails at `processing`, file not found | Path mismatch: fix the mounts, or set `path_from`/`path_to`. |
| Job fails at `arr_reconcile` | Arr `url`/`api_key` missing or wrong, or the Arr is slow. Raise `command_timeout`. |
| Job fails at `autopulse` | AutoPulse URL, credentials, or `trigger_endpoint`. |
| Jellyfin shows the old filename | An Arr → AutoPulse webhook is still firing on import. Remove it. |
| Nothing is ever encoded | Library disabled, or scanned work is still `pending`. Press *Queue pending*, or set `schedule.process_after_scan`. |

The database behind all of this is snapshotted daily into `config/backups/`
and the last seven are kept; see [Backups](README.md#backups) for restoring
one.

## A complete worked example

Two Arrs, shared media mount, AutoPulse into Jellyfin, `cleanup` on import and
`standard` overnight:

```toml
state_db = "/config/state.db"

[schedule]
enabled = true
scan_interval_hours = 6.0
process_after_scan = false      # review first, queue by hand

[[integrations.sonarr]]
id = "tv"
name = "TV Sonarr"
mode = "cleanup"
url = "http://sonarr:8989"
api_key = "..."

[[integrations.radarr]]
id = "movies"
name = "Movies Radarr"
mode = "cleanup"
url = "http://radarr:7878"
api_key = "..."

[integrations.autopulse]
enabled = true
url = "http://autopulse:2875"
username = "..."
password = "..."

[[libraries]]
id = "tv"
name = "TV"
enabled = true
paths = ["/media/TV"]
mode = "standard"

[[libraries]]
id = "movies"
name = "Movies"
enabled = true
paths = ["/media/Movies"]
mode = "standard"
```

Sonarr's webhook points at `http://transcoder:8080/api/webhook/sonarr/tv`,
Radarr's at `http://transcoder:8080/api/webhook/radarr/movies`, both with
**On Import** and **On Upgrade** and the `X-Api-Key` header.

New episodes get the cheap audio/subtitle/container cleanup within seconds of
import and reach Jellyfin immediately; the expensive x265 pass happens when a
scheduled scan plans the file under its library's `standard` mode and you queue
it.
