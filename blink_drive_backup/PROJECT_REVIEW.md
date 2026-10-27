# Blink Vault continuation review

Reviewed 2026-10-27. Recommendation: validate a real backup on the existing
Windows installation first. If removing the PC dependency becomes a requirement,
prototype a scheduled Python Cloud Run job before committing to an Apps Script
rewrite. No migration or live-account actions were performed in this review.

## What this checkout establishes

The attached code requests local-storage clips through BlinkPy's internet API:
manifest, prepare, then download. It does not read the XR+ microSD card over LAN.
The module and its internet connection remain necessary for every hosting option.

The code already treats manifest size as advisory, validates MP4 container
structure, and verifies uploaded bytes against Drive size/MD5. It persists
discovery and separate transfer retries in SQLite, rescans the complete manifest,
and saves downloads during Drive inventory/upload outages. Retention orders
app-owned files by recording time and rechecks folder, tag, key, and MIME type
before permanent cloud deletion. No Blink source-deletion call was found.

This checkout contains default settings with null account/folder selections. It
has no client secret or runtime state. The stated Ethan installation path is not
present on the available machine, and no listener was found on port 8765.
Consequently, deployed code, real transfers, cached-token refresh, and Google
OAuth publishing status remain unverified.

Two small fixes build on the existing implementation:

- Missing sources previously occupied limited download selections. Enough such
  rows could prevent available clips from ever being attempted. Selection now
  streams pending rows and caps actual source attempts; missing jobs stay
  unresolved, and a completed local payload can still recover without its source.
- Dashboard SQLite reads and retry writes now explicitly close their connections.
  A SQLite transaction context alone does not close the connection. This caused
  a Windows file-lock error during the original dashboard test cleanup.

A root `.gitignore` now excludes private configuration, runtime state, recordings,
sidecars, environments, and ZIPs. It does not untrack files already committed.
The existing tracked `settings.json` contains defaults; do not commit deployment
changes containing private account/folder information. No settings were changed.

Validation: all 41 offline tests passed under Python 3.11 on Windows, including
manifest-size disagreement, truncated MP4 rejection, persistence/recovery,
retention safeguards, request protections, and the two new download-batch tests.
The validation venv used existing Python 3.11 packages and the installed pure-Python
filelock package from Python 3.12 via an additional module search path. Package
downloads were unavailable here. This is not a clean dependency-install test or
a new real-camera/Google transfer test. The earlier generated-MP4 validation
remains historical evidence; this review did not repeat it.

## Identify the deployed version without exposing credentials

On the Plex server, open PowerShell in the installed app directory:

```powershell
Set-Location -LiteralPath 'C:\Users\ethan\Documents\Blink Backup\blink_drive_backup'
.\.venv\Scripts\python.exe -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('backup.py').read_text(encoding='utf-8').encode('utf-8')).hexdigest())"
```

This hashes source with normalized line endings, not settings or credentials.
Known fingerprints of `backup.py`:

| Source | Normalized SHA-256 |
| --- | --- |
| Attached original, already containing the advisory-size fix | `c9d3c19287c52fdcc430af04f21006acd5a3852ac041e5433978929b1481ef96` |
| This review's update, also fixing missing-source batch starvation | `e81901b202b4b9405577c5934bf157edf5d20565d243ee687d44ae2e87cf1810` |

A different fingerprint requires inspecting that file. A matching fingerprint
identifies disk contents; fully restart the dashboard and worker to load them.
To apply this review, stop backup, wait for the worker to exit, close the dashboard
process, and copy `backup.py`, `web_app.py`, and the updated tests into the existing
installation. Preserve `settings.json`, `client_secret.json`, `.venv/`, and all of
`state/`. Restart with `START_DASHBOARD.bat`. No new application dependency or
credential setup is introduced by these fixes.

## Confirm one real transfer, then restart recovery

1. Note current uploaded/pending counts and the latest scan time. Create a short,
   recognizable recording in the Blink app and wait for its local-storage entry.
2. Start the configured backup. For clips already on cooldown from the old error,
   use the dashboard's existing retry control once. A successful scan must show
   the new recording; a failed scan or an absent clip is not successful discovery.
3. Match that clip in history and activity: `Downloaded:` followed by `uploaded:`.
   The upload path checks Drive size and MD5 before completion. An advisory-size
   difference is informational. `aged_out` and `oversized` are not uploads.
4. Open that exact MP4 in the designated Drive folder and play/download it. Confirm
   recognizable content and expected duration. A playable Drive copy provides
   additional evidence beyond structural MP4 validation and the queue count.
5. Confirm a fresh report with `manifest_ok=true`, `drive_ok=true`, inspect
   `cycle_errors`/problem clips, and verify `drive_bytes <= max_bytes` after the
   completed cycle. Other unresolved clips can still exist even when this one
   succeeds. A `--once` exit code of zero alone is insufficient: stage failures
   are deliberately caught and queued for retry.
6. Stop/restart the app and make another recognizable recording. Confirm saved
   authorization is reused and the first clip is not duplicated. Separately
   exercise an outage/restart with a staged clip, then confirm it uploads after
   connectivity returns. Keep the journal and staged payload throughout.

`CHECK_STATUS.bat` / `backup.py --status` reads the last completed report without
signing in. Running `backup.py --once` performs real uploads **and retention**;
stop the dashboard worker before using it. Do not treat it as a read-only check.
Logs/status can include camera names and recording times; share only necessary
counts, sanitized errors, and source fingerprints. Never share token/auth files.

Check Google Cloud Console → Google Auth Platform → Audience for the OAuth
project used by this installation. Its actual publishing status cannot be inferred
from the saved JSON. External apps in Testing receive seven-day refresh tokens
for scopes such as `drive.file`. After switching to Production, obtain a fresh
authorization if necessary; Production still permits revocation and other token
expiry conditions. [Google refresh-token documentation](https://developers.google.com/identity/protocols/oauth2#expiration)

## Hosting comparison

| Option | Reuse and operational tradeoff | Assessment |
| --- | --- | --- |
| Existing Windows server | Keeps Python, SQLite, staged files, and the local dashboard. Requires an awake PC, internet, and supervised startup. | Lowest-change route; validate now. |
| Apps Script | Requires JavaScript Blink authentication/downloads and a new durable queue/staging design. Native Drive integration is convenient, but Google execution limits constrain polling and backlog recovery. | Experimental until a small end-to-end proof passes. |
| Scheduled Cloud Run job | Keeps Python/BlinkPy and most transfer/retention logic. Needs durable state, staging, credential updates, a distributed lease, and billing. | Preferred cloud prototype. |
| Small cloud VM with persistent disk | Can keep SQLite, filesystem staging, and process supervision with fewer backend changes. Requires OS updates, monitoring, protected administration, and ongoing VM/disk costs. | Consider if preserving the filesystem architecture matters more than serverless operation. |

Apps Script currently allows 6 minutes per execution, 90 minutes/day of trigger
runtime for consumer accounts (6 hours for Workspace), and URL Fetch responses
and POST bodies up to 50 MB per call. Properties storage is limited to 500 KB
per store and 9 KB per value, so it cannot replace a recording spool or a growing
SQLite journal. A one-minute trigger runs up to 1,440 times/day; 90 minutes allows
an average 3.75 seconds/run before other trigger usage. Blink preparation waits,
downloads, and retry delays must fit this budget. Large clips require a separately
proven chunked approach; do not assume Blink supports HTTP Range.
[Apps Script quotas](https://developers.google.com/apps-script/guides/services/quotas)

The Apps Script proof should use a separate test folder with retention disabled.
Demonstrate Blink login including HTTP-202/2FA handling, refresh across separate
executions, a complete manifest, preparation/download of one genuine XR+ clip,
and a verified/playable Drive copy. Then test a failed upload surviving a restart
and measure scan time, preparation time, largest clip, transfers/day, and backlog
drain time. Test requests from Google's infrastructure; success from the PC does
not establish acceptance of a different network/client. Implement durable stage
transitions, stable clip IDs, idempotent uploads, and locking before adding rolling
retention.

Drive `appProperties` are private to the requesting app. A new Apps Script OAuth
identity must not assume it can read the desktop app's ownership tags or access
the same files under `drive.file`. A migration needs an explicit ownership/access
handoff; weakening deletion safeguards is not a valid handoff.
[Drive custom properties](https://developers.google.com/workspace/drive/api/guides/properties)

Cloud Run's default writable filesystem consumes memory and is discarded when
the instance stops. Simply containerizing `backup.py --once` loses the queue,
tokens, and staged retries. Use a transactional durable queue (for example
Firestore), private object storage for staged payloads, and protected credentials
with durable refresh-token updates. Keep SQLite off an object-storage FUSE mount;
object storage is for blobs, not SQLite transactions. One task per execution is
insufficient to prevent overlap between executions: use a distributed lease with
renewal/fencing, including around Drive retention. Preserve clip keys, ownership
tags, retry deadlines, and upload receipts. Handle termination without marking an
unverified upload complete. A cloud dashboard requires a separate authenticated
design; the localhost dashboard should not be published unchanged.
[Cloud Run filesystem contract](https://docs.cloud.google.com/run/docs/container-contract#file_system_access)

For the user's personal Drive, retain explicit user OAuth authorization; the
Cloud Run service identity used for Cloud Storage/Firestore does not by itself
grant access to the user's Drive backups. Keep reusable tokens out of images,
build contexts, Git, and logs. A cloud job also needs a way to request human 2FA
when cached Blink authorization stops working.

Cloud Run jobs bill the instance lifetime with a one-minute minimum per started
instance. Illustrative 30-day compute-only estimates for one task, 1 vCPU/1 GiB,
us-central1, at most one minute of actual runtime/run, and an otherwise unused
billing-account free tier:

| Schedule | Runs/month | Minimum billable seconds | Approximate compute/month |
| --- | ---: | ---: | ---: |
| Every 15 minutes | 2,880 | 172,800 | $0 |
| Every 5 minutes | 8,640 | 518,400 | $5.15 |
| Every minute | 43,200 | 2,592,000 | $46.62 |

These use current on-demand rates of $0.000018/vCPU-second and
$0.000002/GiB-second after 240,000 CPU seconds and 450,000 memory GiB-seconds
free. Longer transfers/retries, extra tasks, or shared free-tier usage increase
compute cost. Add durable staging/database, secrets, logs, builds/images,
networking, and Drive storage; confirm applicable Drive traffic pricing instead
of assuming it is free. The 10 GB rolling archive cap does not cap monthly
transfer volume. [Cloud Run pricing](https://cloud.google.com/run/pricing)
Scheduler adds $0.10/job/month after three free jobs per billing account.
[Cloud Scheduler pricing](https://cloud.google.com/scheduler/pricing)

## Remaining reliability limitation to address after the live baseline

Google service creation/folder verification happens before Blink login in
`run()`. The outage-buffer behavior therefore applies once initialization has
succeeded; a Google startup/authorization outage can prevent any source scan
until the worker successfully restarts. Making Blink discovery and staged-payload
recovery independent of initial Google availability is a useful next change,
with explicit expired-authorization reporting and regression tests. This review
does not claim that initialization has been redesigned.

Measure recording volume and the source retention horizon before reducing poll
frequency to save cloud costs. No host can recover footage that Blink never
exposes or removes before the application downloads it. Validate the Windows
baseline, then compare a limited cloud prototype against that measured workload.
