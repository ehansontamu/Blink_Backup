from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
import asyncio
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup
from mp4_fixture import VIDEO, box


class Clip:
    def __init__(self, id, fails=False, short=False):
        self.id, self.name = id, "Camera"
        self.created_at = datetime(2026, 10, 27, 0, 0, id, tzinfo=timezone.utc)
        self.size = len(VIDEO)
        self.fails, self.short, self.calls = fails, short, 0

    async def prepare_download(self, blink):
        self.calls += 1
        return not self.fails

    async def download_video(self, blink, path):
        Path(path).write_bytes(VIDEO[:-1] if self.short else VIDEO)
        return True


class Module:
    sync_id = 123
    def __init__(self, clips):
        self.clips = clips
        self._local_storage = {"manifest": set()}
    async def update_local_storage_manifest(self):
        self._local_storage["manifest"].update(self.clips)
        return True


class Drive:
    def __init__(self):
        self.unavailable = False
        self.fail_upload = False
        self.files = []
        self.calls = 0
    def inventory(self):
        if self.unavailable:
            raise OSError("offline")
        return list(self.files)
    def accept(self, path, key, recorded, budget, files):
        self.calls += 1
        if self.fail_upload:
            raise OSError("upload offline")
        f = {"key": key, "id": key, "size": path.stat().st_size,
             "md5Checksum": backup.file_md5(path), "recorded": recorded, "name": path.name}
        self.files.append(f)
        files.append(f)
        return "uploaded"


class RetryQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "staging").mkdir()
        self.journal = backup.Journal(self.root / "journal.sqlite3")
        self.drive = Drive()
        self.settings = {"max_bytes": 100000, "max_clips_per_cycle": 50,
                         "download_delay_seconds": 0, "min_free_disk_bytes": 0}
        self.state_patch = patch.object(backup, "STATE", self.root)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.journal.db.close()
        self.tmp.cleanup()

    def cycle(self, module):
        return asyncio.run(backup.sync_cycle(None, module, self.drive, self.journal, self.settings))

    def test_bad_clip_does_not_block_good_clip(self):
        bad, good = Clip(2, fails=True), Clip(1)
        module = Module([bad, good])
        result = self.cycle(module)
        self.assertFalse(self.journal.seen(backup.clip_key(module, bad)))
        self.assertTrue(self.journal.seen(backup.clip_key(module, good)))
        self.assertEqual(result["counts"]["download"], 1)
        self.assertEqual(result["counts"]["uploaded"], 1)

    def test_failed_download_retries_after_restart_and_backoff(self):
        clip = Clip(1, fails=True)
        module = Module([clip])
        self.cycle(module)
        row = self.journal.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["download_attempts"], 1)
        self.assertGreater(row["next_download"], backup.time.time())
        self.journal.db.close()
        self.journal = backup.Journal(self.root / "journal.sqlite3")
        clip.fails = False
        self.cycle(module)
        self.assertEqual(clip.calls, 1)  # Cooldown survived restart.
        with self.journal.db:
            self.journal.db.execute("UPDATE jobs SET next_download=0")
        self.cycle(module)
        self.assertEqual(clip.calls, 2)
        self.assertTrue(self.journal.seen(backup.clip_key(module, clip)))

    def test_drive_outage_keeps_ingesting_source_clips(self):
        a, b = Clip(1), Clip(2)
        module = Module([a])
        self.drive.unavailable = True
        self.cycle(module)
        module.clips = [a, b]
        self.cycle(module)
        self.assertEqual(len(list((self.root / "staging").glob("*.mp4"))), 2)
        self.assertFalse(self.journal.seen(backup.clip_key(module, a)))
        self.drive.unavailable = False
        module.clips = []  # Even a removed source is safe once fully staged.
        result = self.cycle(module)
        self.assertEqual(result["counts"]["uploaded"], 2)
        self.assertFalse(list((self.root / "staging").glob("*.mp4")))

    def test_missing_source_is_unresolved_then_recovers(self):
        clip = Clip(1, fails=True)
        module = Module([clip])
        self.cycle(module)
        module.clips = []
        result = self.cycle(module)
        self.assertEqual(result["missing_from_latest_manifest"], 1)
        self.assertFalse(self.journal.seen(backup.clip_key(module, clip)))
        module.clips, clip.fails = [clip], False
        with self.journal.db:
            self.journal.db.execute("UPDATE jobs SET next_download=0")
        result = self.cycle(module)
        self.assertEqual(result["missing_from_latest_manifest"], 0)
        self.assertTrue(self.journal.seen(backup.clip_key(module, clip)))

    def test_truncated_download_is_never_marked_complete(self):
        clip = Clip(1, short=True)
        module = Module([clip])
        self.cycle(module)
        self.assertFalse(self.journal.seen(backup.clip_key(module, clip)))
        self.assertEqual(self.drive.calls, 0)
        self.assertFalse(list((self.root / "staging").glob("*.mp4")))

    def test_manifest_size_disagreement_does_not_block_complete_mp4(self):
        clip = Clip(1)
        clip.size = 1234  # Advisory metadata can differ from downloaded bytes.
        module = Module([clip])
        report = self.cycle(module)
        self.assertTrue(self.journal.seen(backup.clip_key(module, clip)))
        self.assertEqual(report["counts"]["uploaded"], 1)
        row = self.journal.db.execute("SELECT expected_size FROM jobs").fetchone()
        self.assertEqual(row[0], len(VIDEO))

    def test_mp4_validation_rejects_header_only_and_missing_movie(self):
        path = self.root / "invalid.mp4"
        path.write_bytes(b"\0\0\0\x18ftypisom" + b"x" * 20)
        self.assertFalse(backup.valid_video(path))
        path.write_bytes(box(b"ftyp", b"isom\0\0\0\0") + box(b"mdat", b"x" * 20))
        self.assertFalse(backup.valid_video(path))

    def test_mp4_validation_handles_box_lengths_and_legal_eof_data(self):
        path = self.root / "clip.mp4"
        path.write_bytes(VIDEO)
        self.assertTrue(backup.valid_video(path))
        path.write_bytes(VIDEO[:-1])
        self.assertFalse(backup.valid_video(path))
        path.write_bytes(VIDEO[:-9] + b"\0\0\0\0mdatx")
        self.assertTrue(backup.valid_video(path))

    def test_upload_backoff_is_not_reset_by_staging_recovery(self):
        module = Module([Clip(1)])
        self.drive.fail_upload = True
        self.cycle(module)
        self.assertEqual(self.drive.calls, 1)
        self.cycle(module)
        self.assertEqual(self.drive.calls, 1)
        self.drive.fail_upload = False
        with self.journal.db:
            self.journal.db.execute("UPDATE jobs SET next_upload=0")
        result = self.cycle(module)
        self.assertEqual(result["counts"]["uploaded"], 1)

    def test_staging_limit_pauses_download_without_discarding_job(self):
        module = Module([Clip(1)])
        self.settings["max_staging_bytes"] = 1
        result = self.cycle(module)
        self.assertEqual(result["counts"]["download"], 1)
        self.assertTrue(result["cycle_errors"])
        self.assertFalse(self.journal.seen(backup.clip_key(module, module.clips[0])))

    def test_late_clip_older_than_last_seen_timestamp_is_discovered(self):
        newest, late = Clip(2), Clip(1)
        module = Module([newest])
        self.cycle(module)
        module.clips = [late, newest]
        self.cycle(module)
        self.assertTrue(self.journal.seen(backup.clip_key(module, late)))
        self.assertEqual(len(self.drive.files), 2)

    def test_missing_remote_backup_is_queued_for_recovery(self):
        clip = Clip(1)
        module = Module([clip])
        self.cycle(module)
        self.drive.files.clear()
        self.cycle(module)
        self.assertFalse(self.journal.seen(backup.clip_key(module, clip)))
        self.cycle(module)
        self.assertTrue(self.journal.seen(backup.clip_key(module, clip)))
        self.assertEqual(clip.calls, 2)

    def test_intentional_retention_expiry_is_not_requeued(self):
        clip = Clip(1)
        module = Module([clip])
        self.cycle(module)
        self.drive.files.clear()
        self.journal.mark(backup.clip_key(module, clip), "expired")
        result = self.cycle(module)
        self.assertEqual(clip.calls, 1)
        self.assertEqual(result["counts"]["expired"], 1)


if __name__ == "__main__":
    unittest.main()
