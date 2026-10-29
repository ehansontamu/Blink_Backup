import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup
from mp4_fixture import VIDEO
from retention import retention_plan


def item(key, size, recorded):
    return {"id": key, "key": key, "name": key + ".mp4", "size": size, "recorded": recorded}


class Request:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def execute(self, **kwargs):
        if self.error:
            raise self.error
        return self.value

    def next_chunk(self, **kwargs):
        if self.error:
            raise self.error
        return None, self.value


class FakeService:
    def __init__(self, path):
        self.path = path
        self.deleted = []
        self.created = []
        self.fail_upload = False
        self.parents = ["folder"]
        self.props = {"backup_app": backup.TAG, "clip_key": "old"}

    def files(self):
        return self

    def get(self, **kwargs):
        return Request({"id": kwargs["fileId"], "parents": self.parents,
                        "appProperties": self.props, "mimeType": "video/mp4"})

    def delete(self, fileId):
        self.deleted.append(fileId)
        return Request({})

    def generateIds(self, **kwargs):
        return Request({"ids": ["stable-upload-id"]})

    def create(self, **kwargs):
        self.created.append(kwargs["body"])
        return Request({"id": "stable-upload-id", "name": self.path.name,
                        "size": self.path.stat().st_size,
                        "md5Checksum": hashlib.md5(self.path.read_bytes()).hexdigest()},
                       OSError("offline") if self.fail_upload else None)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "test.mp4"
        self.path.write_bytes(VIDEO)
        self.service = FakeService(self.path)
        self.drive = backup.Drive(self.service, "folder")

    def tearDown(self):
        self.tmp.cleanup()

    def test_retention_uses_recording_time_not_input_or_upload_order(self):
        files = [item("new", 4, "2026-03"), item("old", 4, "2026-01"), item("mid", 4, "2026-02")]
        self.assertEqual([f["id"] for f in retention_plan(files, 8)], ["old"])
        self.assertEqual(retention_plan(files, 12), [])

    def test_blink_login_imports_and_requires_local_setup(self):
        with patch.object(backup, "STATE", self.root):
            with self.assertRaisesRegex(RuntimeError, "sign-in required"):
                asyncio.run(backup.blink_login(None, False))

    def test_worker_refresh_callback_saves_rotated_tokens_between_cycles(self):
        auth = SimpleNamespace(login_attributes={"token": "initial", "refresh_token": "initial-refresh"})
        blink = SimpleNamespace(start=AsyncMock(return_value=True), available=True, refresh=AsyncMock())
        with patch.object(backup, "STATE", self.root), patch("blinkpy.auth.Auth", return_value=auth), \
                patch("blinkpy.blinkpy.Blink", return_value=blink):
            backup.write_json(self.root / "blink_auth.json", {"username": "test@example.invalid"})
            asyncio.run(backup.blink_login(None, False))
            auth.login_attributes = {"token": "new", "refresh_token": "rotated-refresh"}
            auth.callback()
            self.assertEqual(backup.read_json(self.root / "blink_auth.json")["refresh_token"], "rotated-refresh")

    def test_atomic_auth_save_retries_temporary_windows_file_lock(self):
        path = self.root / "auth.json"
        path.write_text('{"refresh_token": "old"}', encoding="utf-8")
        replace = Path.replace
        calls = []

        def briefly_locked(temp, target):
            calls.append(target)
            if len(calls) <= 2:
                self.assertEqual(backup.read_json(path)["refresh_token"], "old")
                raise PermissionError("temporary file lock")
            return replace(temp, target)

        with patch.object(backup.sys, "platform", "win32"), patch.object(Path, "replace", briefly_locked), \
                patch.object(backup.time, "sleep") as sleep:
            backup.write_json(path, {"refresh_token": "new"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(backup.read_json(path)["refresh_token"], "new")

    def test_atomic_auth_save_reports_persistent_lock_and_keeps_old_file(self):
        path = self.root / "auth.json"
        path.write_text('{"refresh_token": "old"}', encoding="utf-8")
        with patch.object(backup.sys, "platform", "win32"), \
                patch.object(Path, "replace", side_effect=PermissionError("persistent lock")) as replace, \
                patch.object(backup.time, "sleep"):
            with self.assertRaises(PermissionError):
                backup.write_json(path, {"refresh_token": "new"})
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(backup.read_json(path)["refresh_token"], "old")

    def test_large_old_incoming_does_not_evict_new_recordings(self):
        files = [item("new", 50, "2026-03")]
        self.assertEqual(self.drive.accept(self.path, "ancient", "2025-01", 60, files), "aged_out")
        self.assertEqual(self.service.deleted, [])
        self.assertEqual(self.service.created, [])

    def test_makes_room_without_exceeding_cap(self):
        files = [item("old", 50, "2026-01")]
        result = self.drive.accept(self.path, "new", "2026-02", 60, files)
        self.assertEqual(result, "uploaded")
        self.assertEqual(self.service.deleted, ["old"])
        self.assertLessEqual(sum(int(f["size"]) for f in files), 60)
        self.assertTrue(self.path.exists())  # Caller removes only after journal commit.

    def test_deletion_rechecks_folder_and_tag(self):
        for parents, props in [(["elsewhere"], self.service.props),
                               (["folder"], {"backup_app": "someone-else", "clip_key": "old"}),
                               (["folder"], {"backup_app": backup.TAG, "clip_key": "wrong"})]:
            self.service.parents, self.service.props = parents, props
            with self.assertRaises(RuntimeError):
                self.drive.delete(item("old", 5, "2026-01"))
        self.assertEqual(self.service.deleted, [])

    def test_upload_failure_keeps_staging_and_reuses_drive_id(self):
        self.service.fail_upload = True
        with self.assertRaises(OSError):
            self.drive.accept(self.path, "new", "2026-02", 100, [])
        self.assertTrue(self.path.exists())
        receipt = json.loads(self.path.with_suffix(".upload.json").read_text())
        self.assertEqual(receipt["id"], "stable-upload-id")
        self.service.fail_upload = False
        self.drive.accept(self.path, "new", "2026-02", 100, [])
        self.assertEqual([f["id"] for f in self.service.created], ["stable-upload-id"] * 2)

    def test_existing_remote_clip_is_not_uploaded_twice(self):
        files = [item("already", len(VIDEO), "2026-02")]
        files[0]["md5Checksum"] = backup.file_md5(self.path)
        self.assertEqual(self.drive.accept(self.path, "already", "2026-02", 100, files), "uploaded")
        self.assertEqual(self.service.created, [])

    def test_oversized_clip_is_not_uploaded(self):
        self.assertEqual(self.drive.accept(self.path, "huge", "2026-02", 1, []), "oversized")
        self.assertEqual(self.service.created, [])

    def test_journal_persists_expired_ids_across_restart(self):
        path = self.root / "journal.sqlite3"
        journal = backup.Journal(path)
        journal.mark("past-clip", "uploaded")
        journal.db.close()
        reopened = backup.Journal(path)
        self.assertTrue(reopened.seen("past-clip"))
        self.assertFalse(reopened.seen("new-clip"))
        reopened.db.close()

    def test_inventory_refuses_unmanaged_files(self):
        self.service.list = lambda **kw: Request({"files": [{"id": "personal", "mimeType": "video/mp4"}]})
        with self.assertRaises(RuntimeError):
            self.drive.inventory()
        self.assertEqual(self.service.deleted, [])

    def test_completed_staging_recovers_without_source_manifest(self):
        staging = self.root / "staging"
        staging.mkdir()
        path = staging / "pending.mp4"
        path.write_bytes(self.path.read_bytes())
        backup.write_json(path.with_suffix(".meta.json"), {"key": "new", "recorded": "2026-02"})
        self.service.path = path
        self.drive.inventory = lambda: []
        class Module:
            _local_storage = {"manifest": set()}
            async def update_local_storage_manifest(self):
                return False
        journal = backup.Journal(self.root / "journal.sqlite3")
        with patch.object(backup, "STATE", self.root):
            report = asyncio.run(backup.sync_cycle(None, Module(), self.drive, journal,
                {"max_bytes": 100, "max_clips_per_cycle": 50, "download_delay_seconds": 0}))
        self.assertFalse(report["manifest_ok"])
        self.assertTrue(journal.seen("new"))
        self.assertFalse(path.exists())
        self.assertEqual(len(self.service.created), 1)
        journal.db.close()

    def test_interrupted_cleanup_does_not_block_following_cycles(self):
        staging = self.root / "staging"
        staging.mkdir()
        meta = staging / "pending.meta.json"
        backup.write_json(meta, {"key": "new", "recorded": "2026-02"})
        self.drive.inventory = lambda: []
        class Module:
            _local_storage = {"manifest": set()}
            async def update_local_storage_manifest(self):
                return False
        journal = backup.Journal(self.root / "journal.sqlite3")
        journal.mark("new", "uploaded")
        with patch.object(backup, "STATE", self.root):
            asyncio.run(backup.sync_cycle(None, Module(), self.drive, journal,
                {"max_bytes": 100, "max_clips_per_cycle": 50, "download_delay_seconds": 0}))
        self.assertFalse(meta.exists())
        journal.db.close()


if __name__ == "__main__":
    unittest.main()
