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

Order of work below: containers → libraries and modes → integration
profiles → Arr webhooks → AutoPulse → Jellyfin → verify. Everything from
step 3 on is done in the panel's **Integrations** tab; the config file it
writes is shown at the end of step 5 for anyone who prefers it.

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

A **library** says where files are; a **mode** says what happens to them. In
the **Libraries** tab, press *Add library* once per Arr root - one for
`/media/TV`, one for `/media/Movies` - and pick the mode each should use.

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

## 3. Create the profile in the panel

Open **Integrations** in the panel and press **Add Sonarr** or **Add Radarr**.
Give it a name, ideally the same one the Arr calls itself, and press Create.

The card that appears has the webhook URL for that profile, with a **Copy**
button. It also says what is still missing: a fresh profile can receive
webhooks but cannot talk back to the Arr yet.

Press **Configure** on the card and fill in:

| Field | Value |
| --- | --- |
| `url` | Base URL of that Arr as reachable from this container, such as `http://sonarr:8989`. |
| `api_key` | The Arr's own key, from its **Settings → General → API Key**. Masked, with a Show box. |
| `mode` | `cleanup` for imports. Empty means the file's library decides. |
| `path_from` / `path_to` | Only if the mounts differ. Both or neither. |
| `secret` | Optional, if this Arr should use its own key instead of the global one. |

Press **Save changes**, then **Test**. The Test button calls that Arr's
`system/status`, which proves the URL, the port and the key without asking it
to do any work. It answers with the Arr's version, or with what went wrong.

Every change is written straight to `config/config.toml`, so nothing needs a
restart and the file stays the source of truth. Editing that file by hand
still works; restart with `docker compose restart transcoder` if you do.

## 4. Point the Arr at the webhook

In each Arr: **Settings → Connect → + → Webhook**.

| Field | Value |
| --- | --- |
| Name | `Media-Transcoder` |
| Triggers | **On Import** and **On Upgrade** only |
| URL | the URL copied from the profile card, such as `http://transcoder:8080/api/webhook/sonarr/tv-sonarr` |
| Method | POST |
| Headers | `X-Api-Key: <the key from the Integrations tab>` |

`Authorization: Bearer <key>` works too. With only one profile per provider
you can shorten the URL to `/api/webhook/sonarr`, and the `instanceName` an Arr
sends is matched against the profile name as well.

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

## 5. Jellyfin

The **Jellyfin** card at the bottom of the Integrations tab is shared by every
profile. Tick it on, press
**Configure**, and fill in:

| Field | Value |
| --- | --- |
| `url` | `http://jellyfin:8096` |
| `api_key` | A Jellyfin API key. |
| `timeout`, `max_retries` | How long to wait, and how many times to retry before parking the job. |

The transcoder posts Jellyfin's `Updates` payload to
`/Library/Media/Updated`, using the path **after** the Arr rename. The same
targeted call is made for files processed manually or by a scheduled scan.

The card's **Test** button confirms the URL it will call. It does not fire a
real trigger, because the only verb AutoPulse offers starts a real scan.

AutoPulse is optional. Left off, the workflow finishes after the Arr
reconciliation, and you can point Jellyfin at the file another way. See the
next step.

### The same thing in config.toml

The panel writes this. It is here so you can read what it wrote, or set it up
without the panel:

```toml
[[integrations.sonarr]]
id = "tv-sonarr"              # the <id> in /api/webhook/sonarr/<id>
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

[integrations.autopulse]
enabled = true
url = "http://jellyfin:8096"
api_key = "jellyfin-api-key"
timeout = 15.0
max_retries = 3
```

- `url` and `api_key` are **required** for the rescan/rename stage. Without
  them the job fails at `arr_reconcile`, and AutoPulse is never called with a
  stale name.
- `path_from`/`path_to` map the Arr's view of the library onto the
  transcoder's. Set **both or neither**. Validation rejects one alone, and the
  panel refuses the save rather than applying half of it.
- A 4K Radarr and an HD Radarr are two profiles, two webhook URLs, and can
  name different modes.

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

The Integrations tab is the first place to look: each card says whether its
credentials are complete, and **Test** proves them.

Below the profiles, the **Imports** list is the durable queue itself: one row
per accepted import, the stage it reached, what the Arr and AutoPulse hand-offs
did, and the error if one of them refused. The same thing over HTTP:

```bash
curl -s http://transcoder:8080/health                       # no key needed
curl -s -H "X-Api-Key: $KEY" http://transcoder:8080/api/workflow | jq .
```

A healthy import moves through the stages:

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
| Not sure a profile's credentials are right | Press **Test** on its card in the Integrations tab. It calls the Arr's `system/status` and answers with its version. |
| Job fails at `autopulse` | AutoPulse URL, credentials, or `trigger_endpoint`. |
| Jellyfin shows the old filename | An Arr → AutoPulse webhook is still firing on import. Remove it. |
| Nothing is ever encoded | Library disabled, or scanned work is still `pending`. Press *Queue pending*, or set `schedule.process_after_scan`. |

The database behind all of this is snapshotted daily into `config/backups/`
and the last seven are kept. Settings -> Database snapshots lists them, takes
one on demand, and restores one without stopping the daemon; see
[Backups](README.md#backups).

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
