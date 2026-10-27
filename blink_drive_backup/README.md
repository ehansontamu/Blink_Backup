# Blink rolling backup to Google Drive

For the continuation review, deployment fingerprints, real-transfer validation,
and Apps Script/Cloud Run tradeoffs, see [PROJECT_REVIEW.md](PROJECT_REVIEW.md).

## Browser dashboard (Windows 10)

Double-click **START_DASHBOARD.bat**. It installs any missing Python dependencies,
starts the local web server, and opens **http://127.0.0.1:8765** in your browser.
`START_BACKUP.bat` and `SETUP.bat` now open the same dashboard.

The dashboard includes:

- **Overview:** worker health, last completed scan, pending transfers, missing
  sources, Google Drive usage, and local buffer usage.
- **Clip history:** searchable recording history, status filters, pagination,
  retry counts/deadlines, and individual or bulk retry controls.
- **Activity log:** transfer activity and startup/connection messages.
- **Backup settings:** edit cloud/local budgets, polling pause, transfer delay,
  and batch size while the worker is stopped.
- **Connections:** upload your Google Desktop OAuth client JSON, authorize Drive,
  sign in to Blink, enter a verification code, and select your Sync Module.

Google still requires your own Desktop app OAuth client once. Follow the Google
Cloud steps below, then choose that JSON file in **Connections**. Click **Connect
Google Drive**, then **Continue to Google**. Authorization completes in another
browser tab and creates the app's Drive folder. Existing saved accounts and folder
settings are reused; they do not need to be re-created when upgrading.

Use **Start backup** / **Stop backup** at the top right. Stop is graceful: a
current network transfer can finish before the worker exits. Closing a browser
tab does not stop the web server or backup. Keep the launcher's Python process
running. Closing that process requests a clean stop of its owned worker.

With `--autostart` (used by the launcher), a configured backup starts when the
dashboard starts. The dashboard supervises its worker and retries an unexpected
worker exit after about a minute. Revoked credentials still need your sign-in in
Connections; automatic restarting cannot bypass account verification.

The dashboard is bound to **127.0.0.1 only** and is intended for a browser on the
Plex server, including through Remote Desktop. It does not open a firewall port,
publish a website, or enable LAN access. Requests use a browser-session CSRF token,
local Host/Origin checks, and no remote scripts, fonts, or analytics. Passwords and
tokens are not returned by the dashboard's status API.

### Updating an existing installation to the dashboard

1. Stop your existing backup and any scheduled task that starts it.
2. Extract this ZIP to a temporary folder.
3. Copy `backup.py`, `web_app.py`, `blink_compat.py`, `retention.py`,
   `requirements.txt`, the `web/` and `tests/` directories, and all `.bat` files
   into your existing app folder.
4. **Preserve** your `settings.json`, `client_secret.json`, `.venv/`, and the entire
   `state/` directory. Do not overwrite them with fresh defaults.
5. Run **START_DASHBOARD.bat**. It adds Flask/Waitress to the environment and reuses
   saved setup, the upload queue, and backup history.

If you scheduled the old `backup.py --run` command, update that task's arguments to
the full quoted path to **web_app.py**, followed by **--autostart**. Keep the
program set to your app's `.venv\Scripts\python.exe`. Add `--open` only if you
want a browser opened when the task runs in an interactive Windows session.
Do not schedule both the dashboard and a separate backup worker.

The browser UI is tested with offline fixtures and a real local browser. Your
actual Google/Blink sign-in and Windows startup still need a local smoke test.

## Backup behavior

Automatically copies Blink Sync Module local-storage clips to a dedicated
**Blink Rolling Backup** folder in Google Drive. Default retention is
**10,000,000,000 bytes (10 decimal GB)** across the app's backed-up video files.
It checks the Blink manifest every 60 seconds after the previous cycle finishes.
This is polling, not an instant notification when the SD card is written.

This is a separate companion app using the same clip retrieval calls and
HTTP-202 sign-in workaround as your working downloader. Your original downloader
and viewer are unchanged. Setup creates the Drive folder under the Google account
you authorize on your computer; it has not been created or activated remotely.

## What you need

- Windows with Python 3.11 (the same version used by your downloader).
- An always-on computer with internet access and enough local disk for staged clips.
- Your Blink account with working local-storage downloads; no Blink subscription
  is required for this workflow. XR+ behavior still needs a real-device test.
- A Google account with available Drive storage and permission to authorize your
  own desktop app. Google Workspace policies may require administrator approval.

The Drive app connection in ChatGPT cannot be exported as credentials for this
local Python process. A one-time Google desktop authorization is required.

## One-time Google setup

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create/select
   a project. Enable **Google Drive API** in APIs & Services / Library.
2. Configure Google Auth Platform (or OAuth consent screen). Use your own account
   as a test user if the app's audience is External and its status is Testing.
3. Create an **OAuth client ID** with application type **Desktop app**.
4. Download its JSON and save it next to `backup.py` as **client_secret.json**.
5. Double-click **START_DASHBOARD.bat** and use **Connections** to sign in to Google
   and Blink. Choose your XR+ from the Sync Module list and save the selection.
6. Click **Start backup** when ready. Setup alone does not expire videos.

The original command-line setup remains available for advanced use:
`.venv\Scripts\python.exe backup.py --setup`.

The app requests Google's limited `drive.file` permission: files this app creates
or has been explicitly granted access to, rather than access to your entire Drive.
It creates its own folder so that permission works without a separate file picker.
Do not point it at an arbitrary pre-existing folder or change its app tags.

For long-running personal use, an External OAuth app left in **Testing** may have
refresh tokens expire after seven days. Publish your personal app to Production
when appropriate, or rerun setup when Google requires authorization again. A
published personal-use app is not the same thing as submitting a public app for
verification. Follow Google's current console guidance for your account.

## Start the backup

Double-click **START_DASHBOARD.bat** and use the Start/Stop button. Keep its
Python process running; the browser tab may be closed.

For a single real cycle, including retention:

```powershell
.venv\Scripts\python.exe backup.py --once
```

Each successful scan records every visible clip in a persistent SQLite queue.
The first run includes existing clips, newest first. Later scans add newly visible
clips even if their recording time is earlier than clips already downloaded.
Each cycle attempts at most 50 downloads and 50 uploads, with a two-second pause
between transfer attempts. Neither interval guarantees avoidance of Blink rate limits.
Clips older than the cloud rolling window may be marked `aged_out` rather than
uploaded; that is intentional retention, not a successful backup claim.

## Rolling retention: read before starting

- The app keeps the newest clips by **recording time**, across all cameras on the
  selected module, up to the configured budget. It expires oldest clips first.
- Expired Drive copies are **permanently deleted**, bypassing Trash. Moving them
  to Trash would continue consuming Google account space.
- It never deletes recordings from your Blink SD card and never empties Drive Trash.
- Deletion is restricted to MP4s with this app's ownership tag in its own folder.
  Unknown files/subfolders make the backup stop that cycle; move them out to resume.
- To stay below the cap even during upload, the app downloads a new clip locally,
  makes room by deleting old Drive copies, then uploads. If upload fails, the
  completed local clip is retained and retried; already expired Drive copies
  cannot be restored. A full Google account can still prevent uploading.
- The local retry buffer has its **own 10 GB limit** by default, in addition to
  the 10 GB Drive limit, and reserves 1 GB of free local disk. While Drive is down,
  downloads can continue into that buffer. If it fills, downloads pause visibly
  until uploads make space. Unknown-size source clips use a 100 MB reservation;
  a transfer can transiently exceed the buffer before its actual size is checked.
- The limit is an application policy, not a reserved Google storage partition.
  Drive metadata, unrelated account files, and pre-existing Trash are not part of
  the clip budget. Storage display updates may lag.
- Don't run another instance using a different copy of the app against the same
  folder. The included lock prevents two processes using this same state directory.
- Completed backups are reconciled against the Drive folder. If you manually
  delete/move one, the app queues recovery when the source is still available.
  Deliberately expired backups are remembered and are not re-uploaded. To preserve
  important footage outside the rolling window, make a separate copy.

## Automatic startup on Windows

After setup and a successful `--once` test, use Windows Task Scheduler:

1. Create a task triggered **At log on**, for your normal Windows user.
2. Program: the full path to `.venv\Scripts\python.exe` inside this folder.
3. Arguments: the full quoted path to `web_app.py`, followed by `--autostart`.
4. Start in: the directory containing `web_app.py`.
5. Set **Do not start a new instance** if the task is already running. Disable
   any automatic task stop-after-duration setting for continuous use.
6. Under Settings, set **If the task fails, restart every 1 minute** with enough
   restart attempts for the outages you want to tolerate. The app can exit when
   initial sign-in/network setup fails; a saved queue still survives that exit.

The computer must stay awake. Successful setup saves credentials for reuse;
authentication can eventually require another interactive `SETUP.bat` run.
This package does not install a scheduled task on your computer from ChatGPT.

## Settings and diagnostics

Edit `settings.json` while stopped. `max_bytes` defaults to 10 GB;
`poll_seconds` must be at least 60. Lowering the budget expires excess backups on
the next real run. To select another module, set `module_id` to null and rerun setup.

Logs: `state/backup.log` (rotated). Transfer queue/journal: `state/journal.sqlite3`.
Pending MP4s: `state/staging/`. Successful uploads are size/MD5 checked before
removing the staged copy. Interrupted uploads retain a Drive file ID to avoid
duplicate files on retry. Entire manifests are rescanned; a time watermark is
not used, so late-appearing clips are eligible on later polls.

Each clip has independent download/upload retry counters and deadlines, saved
across restarts. Failed transfers retry after about 1, 2, 4, 8, 16, 32, then 60
minutes, with small jitter, with **no retry-count cutoff**. One bad clip no longer
blocks other clips. A temporarily missing source remains unresolved and is checked
on later scans. Blink's manifest size is advisory: it is logged when different,
not used as an exact byte-for-byte acceptance test. The downloader reads the full
HTTP response; MP4 box boundaries, a movie track, file type, and media data are
checked before staging. Cloud size/MD5 is checked before completion. These MP4
checks detect structural problems and common truncation, not every possible
frame-level corruption; the app does not fully decode every video.

### Fix for DownloadSizeMismatch

The first release required exact equality between Blink's manifest size and the
downloaded MP4 size. That assumption could reject otherwise complete downloads.
This update removes that exact comparison and strengthens MP4 structural checks.
The manifest field's precise units/representation have not been verified on your
XR+; the update does not assume a KB/byte conversion. Actual downloaded bytes are
used for storage accounting after successful validation.

For this fix, stop the backup, wait for it to stop, close the dashboard's command
window, and replace **only `backup.py`** from this ZIP in your existing app folder.
Keep your credentials, settings, and entire `state/` directory. Restart with
**START_DASHBOARD.bat**, then use **Clip history → Retry pending clips** to clear
existing cooldowns. No new dependencies or OAuth setup are needed.

Run **CHECK_STATUS.bat** (or `python backup.py --status`) to see `state/status.json`:
pending downloads/uploads, uploaded/expired/aged-out counts, manifest/Drive errors,
and up to 100 problem clips. `checked_at` is a Unix timestamp; check its freshness.
This is a local report, not an email/push alert. A failed scan never means an empty
successful scan. Unexpected whole-cycle errors back off up to 15 minutes.

No software can guarantee capture of a clip Blink never exposes, or one removed
before download. Keep the source SD card healthy and with available recording
space: this app does not clear it. Long outages, a full staging buffer, expired
sign-in, or a sustained backlog can still cause missed source clips. Confirm a
real-device test and check the pending/error report regularly.

## Updating from the first package

Use the dashboard upgrade instructions at the top of this README. Missing new
settings use defaults. The existing completion journal is migrated automatically,
so previously completed clips stay completed.

Keep `state/` and `client_secret.json` private and outside any synced/shared folder.
The Blink auth cache can contain your password and refresh tokens; Google tokens
also grant account access. Unix permissions are restricted; on Windows, use your
private user-profile directory and normal Windows access controls. Do not upload
the extracted live app folder or credential files to a public repository.

## Validation performed

The package includes 41 offline tests for dashboard controls, worker supervision, sign-in state handling,
request protections, retention ordering, strict cloud size
enforcement, deletion safeguards, independent retries, recovery across restarts,
late/missing source clips, truncated downloads, Drive outages, local-buffer limits,
duplicate avoidance, and journal persistence. Python compilation and dependency
imports were also checked. A real generated H.264 MP4 was accepted and a copy
with a truncated media-data box was rejected by the updated validator.
No live Blink or Google account credentials were used: authentication, actual
XR+ downloads, Drive uploads, and Task Scheduler still need your local smoke test.

The 2026-10-27 continuation review also verifies that missing sources cannot
consume the download-attempt batch and explicitly closes dashboard SQLite
connections to avoid Windows file locks. All 41 offline tests passed; see the
review for the validation environment and the remaining live-test requirements.

Run offline tests: `python -m unittest discover -s tests -v`.

References:
- [Google Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google Drive deletion](https://developers.google.com/workspace/drive/api/guides/delete)
- [Google desktop setup](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Google refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [BlinkPy](https://github.com/fronzbot/blinkpy)
