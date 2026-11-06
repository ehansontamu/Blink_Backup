"""Subscription-free Blink clip backup to a size-limited synced folder.

Run --setup once, then --run (or --once). See README.md.
"""
import argparse
import asyncio
from datetime import timezone
import getpass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sqlite3
import sys
import time
import shutil
import random
import struct

from retention import retention_plan

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
LOG = logging.getLogger("backup")
MANAGED_CLIP = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_.+_([a-f0-9]{64})\.mp4$")


def stop_requested():
    return (STATE / "stop.request").exists()


async def interruptible_sleep(seconds):
    deadline = time.monotonic() + seconds
    while not stop_requested() and time.monotonic() < deadline:
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.chmod(0o600)
    for attempt in range(5):
        try:
            temp.replace(path)
            return
        except PermissionError:
            # Windows can briefly deny replacement while a scanner/reader has
            # the destination open. Preserve the old file and retry atomically.
            if sys.platform != "win32" or attempt == 4:
                raise
            time.sleep(.05 * 2 ** attempt)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def clip_key(module, clip):
    # Include timestamp and camera: clip IDs may repeat across devices/eras.
    raw = f"{module.sync_id}|{clip.name}|{clip.id}|{clip.created_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def recorded_time(clip):
    date = clip.created_at
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat(timespec="microseconds")


def valid_video(path):
    """Check MP4 box boundaries and a video container, without decoding frames.

    Blink's manifest size is advisory, not an authoritative HTTP byte count.
    The download library reads the entire HTTP response (and checks transport
    completeness); these checks independently catch malformed/truncated boxes.
    This is structural validation, not a full audio/video integrity test.
    """
    def boxes(stream, start, end):
        result, offset = [], start
        while offset < end:
            if end - offset < 8:
                raise ValueError("Incomplete MP4 box header")
            stream.seek(offset)
            size, kind = struct.unpack(">I4s", stream.read(8))
            header = 8
            if size == 1:
                if end - offset < 16:
                    raise ValueError("Incomplete extended MP4 box")
                size = struct.unpack(">Q", stream.read(8))[0]
                header = 16
            elif size == 0:
                size = end - offset
            if size < header or offset + size > end:
                raise ValueError("MP4 box exceeds file boundary")
            result.append((kind, offset + header, offset + size))
            offset += size
        return result

    try:
        if not path.exists() or path.stat().st_size < 32:
            return False
        with path.open("rb") as stream:
            top = boxes(stream, 0, path.stat().st_size)
            has_type = any(k == b"ftyp" and end - start >= 8 for k, start, end in top)
            has_data = any(k == b"mdat" and end > start for k, start, end in top)
            has_track = False
            for kind, start, end in top:
                if kind == b"moov":
                    children = boxes(stream, start, end)
                    has_track |= any(k == b"trak" and b > a for k, a, b in children)
            return has_type and has_data and has_track
    except (OSError, ValueError, struct.error):
        return False


def file_md5(path):
    result = hashlib.md5()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS done (key TEXT PRIMARY KEY, status TEXT)")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, name TEXT NOT NULL, recorded TEXT NOT NULL,
            expected_size INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'download',
            download_attempts INTEGER NOT NULL DEFAULT 0, upload_attempts INTEGER NOT NULL DEFAULT 0,
            next_download REAL NOT NULL DEFAULT 0, next_upload REAL NOT NULL DEFAULT 0,
            last_error TEXT, last_seen REAL, missing_since REAL)""")

    def seen(self, key):
        return self.db.execute("SELECT 1 FROM done WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key, status):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", (key, status))
            self.db.execute("UPDATE jobs SET stage=?, last_error=NULL WHERE key=?", (status, key))

    def discover(self, key, name, recorded, expected_size=0, visible=True):
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO jobs(key,name,recorded,expected_size)
                VALUES(?,?,?,?)""", (key, name, recorded, expected_size))
            done = self.db.execute("SELECT status FROM done WHERE key=?", (key,)).fetchone()
            if done:
                self.db.execute("UPDATE jobs SET stage=? WHERE key=?", (done[0], key))
            if visible:
                self.db.execute("UPDATE jobs SET last_seen=?, missing_since=NULL WHERE key=?", (time.time(), key))

    def ready(self, stage, limit=None):
        column = "next_download" if stage == "download" else "next_upload"
        # Stream rows so unavailable sources can be skipped without loading the
        # whole queue or consuming slots intended for actual transfer attempts.
        return self.db.execute(f"""SELECT * FROM jobs WHERE stage=? AND {column}<=?
            ORDER BY {column}, recorded DESC, key LIMIT ?""",
            (stage, time.time(), -1 if limit is None else limit))

    def downloaded(self, key):
        with self.db:
            self.db.execute("UPDATE jobs SET stage='upload',next_upload=0,last_error=NULL WHERE key=?", (key,))

    def fail(self, key, stage, reason):
        attempts = "download_attempts" if stage == "download" else "upload_attempts"
        next_try = "next_download" if stage == "download" else "next_upload"
        row = self.db.execute(f"SELECT {attempts} FROM jobs WHERE key=?", (key,)).fetchone()
        count = row[0] + 1
        delay = min(3600, 60 * 2 ** min(count - 1, 6))
        with self.db:
            self.db.execute(f"""UPDATE jobs SET {attempts}=?,{next_try}=?,last_error=? WHERE key=?""",
                (count, time.time() + delay + random.uniform(0, delay * .1), reason, key))

    def note_missing(self, visible):
        with self.db:
            for row in self.db.execute("SELECT key FROM jobs WHERE stage='download'").fetchall():
                if row[0] not in visible:
                    self.db.execute("UPDATE jobs SET missing_since=COALESCE(missing_since,?) WHERE key=?",
                        (time.time(), row[0]))

    def report(self):
        counts = {r[0]: r[1] for r in self.db.execute("SELECT stage,COUNT(*) FROM jobs GROUP BY stage")}
        missing = self.db.execute("SELECT COUNT(*) FROM jobs WHERE stage='download' AND missing_since IS NOT NULL").fetchone()[0]
        errors = [dict(r) for r in self.db.execute("""SELECT key,name,stage,download_attempts,upload_attempts,
            last_error,missing_since FROM jobs WHERE stage IN ('download','upload')
            AND (last_error IS NOT NULL OR missing_since IS NOT NULL) ORDER BY recorded DESC LIMIT 100""")]
        return {"counts": counts, "missing_from_latest_manifest": missing, "problem_clips": errors}


class SyncedFolder:
    """A dedicated local folder synchronized by Google Drive for desktop.

    Only filenames generated by this app are inventoried or deleted. The
    journal remains the source of transfer history; the folder is the durable
    payload destination that Drive for desktop synchronizes independently.
    """
    def __init__(self, path):
        self.folder = Path(path).expanduser()
        self.on_expire = None

    def inventory(self):
        if not self.folder.is_dir():
            raise RuntimeError("Configured synced backup folder is missing or unavailable.")
        files = []
        for path in self.folder.iterdir():
            match = MANAGED_CLIP.fullmatch(path.name)
            try:
                safe_parent = path.resolve().parent == self.folder.resolve()
            except OSError:
                safe_parent = False
            if not path.is_file() or not match or not safe_parent:
                continue
            # Filename timestamps sort in recording order. The full journal
            # timestamp is used for new files during the current process.
            recorded = path.name[:19].replace("_", "T", 1)
            files.append({"id": str(path), "path": path, "name": path.name,
                          "size": path.stat().st_size, "recorded": recorded,
                          "key": match.group(1)})
        return files

    def delete(self, file):
        path = Path(file.get("path", file["id"]))
        match = MANAGED_CLIP.fullmatch(path.name)
        try:
            safe_parent = path.resolve().parent == self.folder.resolve()
        except OSError:
            safe_parent = False
        if not safe_parent or not match or match.group(1) != file["key"]:
            raise RuntimeError("Retention safety check failed; no deletion performed.")
        path.unlink()
        if self.on_expire:
            self.on_expire(file)
        LOG.info("Expired oldest backup: %s", file["name"])

    def accept(self, path, key, recorded, budget, files):
        match = MANAGED_CLIP.fullmatch(path.name)
        if not match or match.group(1) != key:
            raise RuntimeError("Staged filename/key safety check failed; copy refused.")
        # The caller also checks this; keep retries idempotent after interrupted runs.
        existing = next((f for f in files if f["key"] == key), None)
        if existing:
            target = Path(existing.get("path", existing["id"]))
            if int(existing["size"]) != path.stat().st_size or file_md5(target) != file_md5(path):
                raise RuntimeError("Existing backup checksum mismatch; staged clip retained.")
            return "uploaded"
        size = path.stat().st_size
        if size > budget:
            LOG.warning("Clip exceeds the entire storage budget; skipping.")
            return "oversized"
        candidate = dict(id="incoming", key=key, size=size, recorded=recorded)
        removals = retention_plan(files + [candidate], budget)
        if candidate in removals:
            return "aged_out"
        # Download is complete locally before making space. If the copy fails, the
        # staged file remains for the next retry. A strict cap requires making
        # space first, so old backups may expire before the copy succeeds.
        for file in removals:
            self.delete(file)
            files.remove(file)
        target = self.folder / path.name
        partial = self.folder / (path.name + ".part")
        partial.unlink(missing_ok=True)
        try:
            shutil.copy2(path, partial)
            if partial.stat().st_size != size or file_md5(partial) != file_md5(path) or not valid_video(partial):
                raise RuntimeError("Synced-folder copy failed verification; staged clip retained.")
            partial.replace(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        result = {"id": str(target), "path": target, "name": target.name,
                  "size": size, "key": key, "recorded": recorded}
        files.append(result)
        return "uploaded"


async def blink_login(session, interactive):
    from blinkpy.auth import Auth, BlinkTwoFARequiredError
    from blinkpy.blinkpy import Blink
    from blink_compat import install_blinkpy_2fa_compatibility_fix
    install_blinkpy_2fa_compatibility_fix()
    path = STATE / "blink_auth.json"
    data = read_json(path) if path.exists() else {}
    if interactive:
        print("Blink login (Enter keeps any saved value).")
        email = input("Blink email: ").strip()
        password = getpass.getpass("Blink password: ")
        if email:
            if email != data.get("username"):
                data = {}
            data["username"] = email
        if password:
            data["password"] = password
    if not data.get("username"):
        raise RuntimeError("Blink sign-in required. Run --setup first.")
    blink = Blink(session=session)
    blink.auth = Auth(data, no_prompt=True, session=session)
    # query() invokes this after a successful runtime refresh, before the next
    # network request. Persist rotated tokens even if a later transfer fails.
    blink.auth.callback = lambda: write_json(path, blink.auth.login_attributes)
    try:
        started = await blink.start()
    except BlinkTwoFARequiredError:
        if not interactive:
            raise RuntimeError("Blink requires 2FA again. Run --setup.") from None
        started = await blink.send_2fa_code(input("Blink verification code: ").strip())
    if not started or not blink.available:
        raise RuntimeError("Blink login failed. Run --setup to sign in again.")
    await blink.refresh()
    write_json(path, blink.auth.login_attributes)
    return blink


def choose_module(blink, settings, interactive):
    modules = [m for m in blink.sync.values() if m.local_storage]
    if settings.get("module_id") is not None:
        modules = [m for m in modules if str(m.sync_id) == str(settings["module_id"])]
    if not modules:
        raise RuntimeError("Configured Sync Module has no accessible local storage.")
    if len(modules) > 1:
        if not interactive:
            raise RuntimeError("Multiple Sync Modules found. Run --setup to select one.")
        for index, module in enumerate(modules, 1):
            print(f"{index}. {module.name} ({module.sync_id})")
        module = modules[int(input("Select module number: ")) - 1]
    else:
        module = modules[0]
    settings["module_id"] = str(module.sync_id)
    write_json(ROOT / "settings.json", settings)
    return module


async def sync_cycle(blink, module, destination, journal, settings):
    errors = []
    visible = {}
    scan_ok = False
    destination.on_expire = lambda file: journal.mark(file["key"], "expired")
    # Discovery is independent of destination availability: persist every API-visible
    # clip before trying transfers. Never use a newest-timestamp watermark.
    try:
        module._local_storage["manifest"].clear()
        if not await module.update_local_storage_manifest():
            raise RuntimeError("ManifestUnavailable")
        for clip in module._local_storage["manifest"]:
            key = clip_key(module, clip)
            visible[key] = clip
            camera = re.sub(r'[^\w .-]', '_', clip.name).strip('. ')[:60] or "Camera"
            name = f"{clip.created_at:%Y-%m-%d_%H-%M-%S}_{camera}_{key}.mp4"
            journal.discover(key, name, recorded_time(clip), int(clip.size or 0))
        journal.note_missing(visible)
        scan_ok = True
    except Exception as error:
        message = "Blink manifest: " + type(error).__name__
        if type(error).__name__ == "TokenRefreshFailed":
            message += "; will retry. If this persists, stop backup and reconnect Blink in Connections."
        errors.append(message)
        LOG.error("Blink scan failed; staged clips can still upload. (%s)", type(error).__name__)

    # Import previous-version staged files and recover a crash between download,
    # queue commit, upload commit, and cleanup. Sidecars never contain auth data.
    for meta_path in (STATE / "staging").glob("*.meta.json"):
        try:
            meta = read_json(meta_path)
            path = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".mp4")
            journal.discover(meta["key"], path.name, meta["recorded"], visible=False)
            if journal.seen(meta["key"]):
                cleanup_staging(path)
            elif valid_video(path):
                job = journal.db.execute("SELECT stage FROM jobs WHERE key=?", (meta["key"],)).fetchone()
                if job[0] != "upload":
                    journal.downloaded(meta["key"])
            else:
                # Leave it unresolved. A fresh source download will repair it.
                LOG.warning("Staged file needs source recovery: %s", path.name)
        except Exception as error:
            errors.append("Staging metadata: " + type(error).__name__)
            LOG.error("Unusable staging metadata; other clips will continue. (%s)", type(error).__name__)

    # Spool downloads locally even while the synced folder is unavailable. Separate retries
    # prevent a bad clip or failed upload from stopping the other cameras.
    staging = STATE / "staging"
    max_staging = settings.get("max_staging_bytes", 10_000_000_000)
    minimum_free = settings.get("min_free_disk_bytes", 1_000_000_000)
    download_attempts = 0
    for job in journal.ready("download"):
        if stop_requested():
            break
        key = job["key"]
        path = staging / job["name"]
        if valid_video(path):
            write_json(path.with_suffix(".meta.json"), {"key": key, "recorded": job["recorded"]})
            journal.downloaded(key)
            continue
        if not scan_ok or key not in visible:
            # Don't equate a temporary omission (or removed source) with success.
            continue
        if download_attempts >= settings["max_clips_per_cycle"]:
            break
        used = sum(p.stat().st_size for p in staging.glob("*.*") if p.is_file())
        expected = job["expected_size"]
        # Manifest values may be rounded or describe a different representation.
        # Reserve a conservative amount; enforce the real limit after download.
        reservation = expected or min(max_staging, 100_000_000)
        if used + reservation > max_staging or shutil.disk_usage(STATE).free < minimum_free + reservation:
            errors.append("Local staging full: downloads paused until uploads free space")
            break
        partial = path.with_suffix(".part")
        download_attempts += 1
        try:
            partial.unlink(missing_ok=True)
            clip = visible[key]
            if not await clip.prepare_download(blink):
                raise RuntimeError("PrepareFailed")
            if not await clip.download_video(blink, str(partial)) or not valid_video(partial):
                raise RuntimeError("DownloadFailedOrInvalidMP4")
            actual = partial.stat().st_size
            if expected and actual != expected:
                LOG.info("Blink size metadata differs: manifest=%s, downloaded=%s bytes; MP4 structure passed. %s",
                         expected, actual, job["name"])
            if used + actual > max_staging:
                raise RuntimeError("LocalStagingLimitExceeded")
            partial.replace(path)
            with journal.db:
                journal.db.execute("UPDATE jobs SET expected_size=? WHERE key=?", (actual, key))
            write_json(path.with_suffix(".meta.json"), {"key": key, "recorded": job["recorded"]})
            journal.downloaded(key)
            LOG.info("Downloaded: %s", job["name"])
        except Exception as error:
            reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            journal.fail(key, "download", reason + ": download incomplete")
            partial.unlink(missing_ok=True)
            LOG.warning("Download queued for retry: %s (%s)", job["name"], type(error).__name__)
        finally:
            await asyncio.sleep(settings["download_delay_seconds"])

    files = None
    try:
        files = destination.inventory()
    except Exception as error:
        errors.append("Backup folder inventory: " + type(error).__name__)
        LOG.error("Backup folder unavailable; downloaded clips stay queued locally. (%s)", type(error).__name__)
    if files is not None:
        # Existing managed files resolve interrupted copies. Historical uploads
        # from the former API destination are intentionally not requeued merely
        # because they are absent from the new synced folder.
        for file in files:
            job = journal.db.execute("SELECT * FROM jobs WHERE key=?", (file["key"],)).fetchone()
            if job:
                path = staging / job["name"]
                target = Path(file.get("path", file["id"]))
                if path.exists() and (int(file["size"]) != path.stat().st_size or file_md5(target) != file_md5(path)):
                    errors.append("Backup-folder verification mismatch: " + job["name"])
                    journal.fail(job["key"], "upload", "Destination size/MD5 mismatch; staged copy retained")
                    continue
                journal.mark(file["key"], "uploaded")
                cleanup_staging(path)
            else:
                journal.mark(file["key"], "uploaded")
        for file in retention_plan(files, settings["max_bytes"]):
            destination.delete(file)
            journal.mark(file["key"], "expired")
            files.remove(file)
        for job in journal.ready("upload", settings["max_clips_per_cycle"]):
            if stop_requested():
                break
            key, path = job["key"], staging / job["name"]
            try:
                if not valid_video(path):
                    # Requeue a missing/corrupt local payload instead of pretending
                    # it was backed up. It may be fetched from the SD card again.
                    with journal.db:
                        journal.db.execute("UPDATE jobs SET stage='download', next_download=0 WHERE key=?", (key,))
                    raise RuntimeError("StagedPayloadMissing")
                status = destination.accept(path, key, job["recorded"], settings["max_bytes"], files)
                journal.mark(key, status)
                cleanup_staging(path)
                LOG.info("%s: %s", status, job["name"])
            except Exception as error:
                reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                journal.fail(key, "upload", reason + ": upload incomplete")
                LOG.warning("Upload queued for retry: %s (%s)", job["name"], type(error).__name__)
            finally:
                await asyncio.sleep(settings["download_delay_seconds"])

    if blink is not None:
        write_json(STATE / "blink_auth.json", blink.auth.login_attributes)
    report = journal.report()
    report.update({"checked_at": time.time(), "manifest_ok": scan_ok,
                   "visible_clips": len(visible), "folder_ok": files is not None,
                   "cycle_errors": errors,
                   "folder_bytes": sum(int(f["size"]) for f in files) if files is not None else None})
    write_json(STATE / "status.json", report)
    pending = report["counts"].get("download", 0) + report["counts"].get("upload", 0)
    LOG.info("Scan: %d visible, %d pending, %d source-missing. %s", len(visible), pending,
             report["missing_from_latest_manifest"], report["counts"])
    if report["missing_from_latest_manifest"]:
        LOG.error("ATTENTION: known clips are absent from the latest successful manifest before download. See state/status.json.")
    if errors:
        LOG.warning("ATTENTION: %s", "; ".join(errors))
    return report


def cleanup_staging(path):
    # Journal commit always precedes cleanup. A crash here is safe to repeat.
    path.unlink(missing_ok=True)
    path.with_suffix(".part").unlink(missing_ok=True)
    path.with_suffix(".upload.json").unlink(missing_ok=True)
    path.with_suffix(".meta.json").unlink(missing_ok=True)


async def run(args):
    from aiohttp import ClientSession, ClientTimeout, TCPConnector
    settings = read_json(ROOT / "settings.json")
    if settings["max_bytes"] <= 0 or settings["poll_seconds"] < 60 or settings["max_clips_per_cycle"] < 1:
        raise RuntimeError("Use a positive budget/batch size and poll_seconds >= 60.")
    destination_path = Path(settings.get("destination_path", "")).expanduser()
    if not destination_path.is_absolute() or not destination_path.is_dir():
        raise RuntimeError("Configure an existing absolute synced backup folder in settings.json.")
    destination = SyncedFolder(destination_path)
    print("Synced backup folder:", destination_path)
    async with ClientSession(timeout=ClientTimeout(total=180), connector=TCPConnector(limit=4)) as session:
        blink = await blink_login(session, args.setup)
        module = choose_module(blink, settings, args.setup)
        print("Blink module:", module.name)
        if args.setup:
            # Read-only preflight: no videos uploaded or deleted by setup.
            if not await module.update_local_storage_manifest():
                raise RuntimeError("Sign-in succeeded but storage manifest could not be read.")
            print("Setup complete. Visible clips:", len(module._local_storage["manifest"]))
            print("Run START_BACKUP.bat or python backup.py --run to begin.")
            return
        journal = Journal(STATE / "journal.sqlite3")
        failures = 0
        while not stop_requested():
            try:
                await sync_cycle(blink, module, destination, journal, settings)
                failures = 0
            except Exception as error:
                failures += 1
                LOG.error("Cycle failed (%s). Check Blink, disk space, and the synced folder.", type(error).__name__)
                if args.once:
                    raise
            if args.once:
                return
            await interruptible_sleep(min(900, settings["poll_seconds"] * (2 ** min(failures, 4))))
        LOG.info("Backup stopped gracefully; pending work is saved.")


def main():
    from filelock import FileLock, Timeout
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--setup", action="store_true")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--status", action="store_true", help="Show last scan and retry queue without signing in")
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.status:
        report_path = STATE / "status.json"
        print(report_path.read_text(encoding="utf-8") if report_path.exists() else "No completed scan recorded yet.")
        return 0
    (STATE / "staging").mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    LOG.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(STATE / "backup.log", maxBytes=2_000_000, backupCount=3)):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    # Do not emit Blink/HTTP debug logs containing account URLs or credentials.
    logging.getLogger("blinkpy").setLevel(logging.CRITICAL)
    try:
        with FileLock(str(STATE / "process.lock"), timeout=0):
            if not args.managed:
                (STATE / "stop.request").unlink(missing_ok=True)
            asyncio.run(run(args))
    except Timeout:
        print("Another backup/setup process is already running.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Completed copies remain in the synced folder.")
    except Exception as error:
        # RuntimeErrors are our user-actionable errors. Suppress external exception
        # strings because they can contain signed media URLs or account identifiers.
        print(str(error) if isinstance(error, RuntimeError) else f"Stopped: {type(error).__name__}. Check setup/network.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
