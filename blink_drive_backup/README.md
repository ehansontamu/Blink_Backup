# Blink Vault application folder

Blink Vault downloads Blink local-storage recordings into `state\staging`,
validates them, and copies them into the local folder configured by
`destination_path` in `settings.json`.

This version intentionally does **not** use the Google Drive API or Google OAuth.
Google Drive for desktop is responsible for synchronizing the destination folder
and displaying cloud-sync status.

## Run

- `START_DASHBOARD.bat` starts the local dashboard and supervised worker.
- `CHECK_STATUS.bat` prints the last completed scan.
- `backup.py --once` performs one scan/copy cycle.
- `backup.py --status` reads status without signing in.

The configured destination must be an existing absolute directory. For this
installation it is `C:\Users\ethan\Documents\Blink Backup`.

## Upgrade and state

Preserve `settings.json` and the entire `state` directory. The state directory
contains the Blink credential cache, SQLite transfer journal, logs, and pending
MP4 staging files. Do not place the application or its state directory in a
public or shared location.

Old Google OAuth files are not used. Existing API-era `uploaded` journal entries
remain historical successes; the migration does not duplicate them into an
initially empty synchronized folder. Pending staging files continue normally.

## Destination guarantees

- Blink Vault only inventories and expires files matching its generated MP4
  filename and 64-character clip-key format.
- Each new copy is verified by byte length, MD5, and MP4 structure before its
  staging payload is removed.
- Unrelated destination content is ignored and never deleted.
- Drive for desktop can move deletions to Google Drive Trash, which may continue
  to consume account storage temporarily.
- “Backed up” in the dashboard means verified in the local synchronized folder.
  Check Drive for desktop for final cloud-sync completion or errors.

Run the offline tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
