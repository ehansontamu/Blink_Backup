# Blink Vault

Blink Vault is a local Windows application that downloads recordings from Blink
Sync Module local storage and copies them into a folder synchronized by Google
Drive for desktop. It maintains a size-limited rolling archive, a persistent
retry queue, and a local staging buffer.

**Blink Vault never deletes recordings from Blink source storage.** Retention
removes only MP4 files whose names match Blink Vault's generated filename and
clip-key format inside the configured destination folder.

## Current installation

The configured synchronized destination is:

```text
C:\Users\ethan\Documents\Blink Backup
```

Google authentication is owned by Google Drive for desktop. Blink Vault has no
Google OAuth client, token, API dependency, or Google Cloud project requirement.

## Data flow

```text
Blink Sync Module -> validated local staging file -> synchronized folder
                                                   -> Google Drive for desktop
```

Blink Vault verifies the local destination copy by size, MD5, and MP4 structure
before committing the journal and removing its staging copy. Google Drive for
desktop independently reports whether that file has finished syncing to Google.
The dashboard therefore means **Backed up** as “verified in the synchronized
folder,” not as a direct Google API confirmation.

## Start

1. In Google Drive for desktop, add the destination under **My Computer** and
   enable **Sync with Google Drive**.
2. Double-click `blink_drive_backup\START_DASHBOARD.bat`.
3. In **Connections**, sign in to Blink and select the Sync Module if needed.
4. Confirm the destination shown in **Backup settings**.
5. Click **Start backup**.

The dashboard runs at <http://127.0.0.1:8765>.

## Retention and Google Drive Trash

The default active destination limit is 10 decimal GB. Blink Vault removes the
oldest managed destination files first. Drive for desktop may move synchronized
deletions to Google Drive Trash. Trashed files can continue consuming Google
storage until Google permanently removes them or you empty Trash.

Unrelated files, subfolders, and MP4s without a Blink Vault clip key are ignored
and are never counted or deleted.

## Migration from the former Drive API destination

The existing transfer journal and staging directory are preserved. Pending
staged clips are copied into the new synchronized folder. Clips previously
recorded as uploaded through the old Google Drive API are left as historical
successes and are not automatically downloaded or duplicated merely because the
new local folder starts empty.

The old `client_secret.json` and `state\google_token.json` files are no longer
read by the application. They may be removed manually after confirming the new
workflow, but they are ignored by Git and should always remain private.

## Settings

`blink_drive_backup\settings.json` contains:

| Setting | Purpose |
| --- | --- |
| `destination_path` | Existing absolute folder synchronized by Drive for desktop |
| `max_bytes` | Maximum active managed bytes in that folder |
| `max_staging_bytes` | Maximum bytes waiting in the retry buffer |
| `min_free_disk_bytes` | Free-space reserve maintained before downloads |
| `poll_seconds` | Pause after a completed cycle; minimum 60 seconds |
| `download_delay_seconds` | Delay between transfer attempts |
| `max_clips_per_cycle` | Maximum downloads and destination copies per cycle |
| `module_id` | Selected Blink Sync Module |

Stop the worker before changing settings in the dashboard.

## Recovery and safety

- Each download and destination-copy stage has independent exponential retry.
- Completed staging files survive restarts and destination outages.
- Copies use a temporary `.part` file followed by an atomic rename.
- Existing matching destination files must pass size and MD5 verification.
- A process lock prevents two workers from using the same state directory.
- Keep `blink_drive_backup\state` private; the Blink auth cache can contain
  account credentials and rotating tokens.

Useful commands, run from `blink_drive_backup`:

```powershell
.\.venv\Scripts\python.exe backup.py --status
.\.venv\Scripts\python.exe backup.py --once
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
