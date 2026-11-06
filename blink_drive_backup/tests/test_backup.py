import asyncio
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


def name_for(key, timestamp="2026-03-01_00-00-00"):
    return f"{timestamp}_Camera_{key}.mp4"


def item(path, key, recorded):
    return {"id": str(path), "path": path, "key": key, "name": path.name,
            "size": path.stat().st_size, "recorded": recorded}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.destination_path = self.root / "Blink Backup"
        self.destination_path.mkdir()
        self.key = "a" * 64
        self.path = self.root / name_for(self.key)
        self.path.write_bytes(VIDEO)
        self.destination = backup.SyncedFolder(self.destination_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_retention_uses_recording_time_not_input_or_copy_order(self):
        files = [{"id": "new", "size": 4, "recorded": "2026-03"},
                 {"id": "old", "size": 4, "recorded": "2026-01"},
                 {"id": "mid", "size": 4, "recorded": "2026-02"}]
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

    def test_inventory_only_manages_blink_vault_filenames(self):
        managed = self.destination_path / self.path.name
        managed.write_bytes(VIDEO)
        (self.destination_path / "personal.mp4").write_bytes(VIDEO)
        files = self.destination.inventory()
        self.assertEqual([f["key"] for f in files], [self.key])

    def test_large_old_incoming_does_not_evict_new_recordings(self):
        new_key = "b" * 64
        target = self.destination_path / name_for(new_key, "2026-03-02_00-00-00")
        target.write_bytes(VIDEO)
        files = [item(target, new_key, "2026-03")]
        result = self.destination.accept(self.path, self.key, "2025-01", len(VIDEO), files)
        self.assertEqual(result, "aged_out")
        self.assertTrue(target.exists())
        self.assertFalse((self.destination_path / self.path.name).exists())

    def test_makes_room_and_copies_atomically_with_checksum(self):
        old_key = "b" * 64
        old = self.destination_path / name_for(old_key, "2026-01-01_00-00-00")
        old.write_bytes(VIDEO)
        files = [item(old, old_key, "2026-01")]
        result = self.destination.accept(self.path, self.key, "2026-03", len(VIDEO), files)
        target = self.destination_path / self.path.name
        self.assertEqual(result, "uploaded")
        self.assertFalse(old.exists())
        self.assertEqual(target.read_bytes(), VIDEO)
        self.assertFalse((self.destination_path / (self.path.name + ".part")).exists())
        self.assertTrue(self.path.exists())  # Caller removes only after journal commit.

    def test_deletion_rechecks_parent_filename_and_key(self):
        target = self.destination_path / self.path.name
        target.write_bytes(VIDEO)
        outside = self.root / self.path.name
        for candidate in [item(outside, self.key, "2026-01"),
                          dict(item(target, self.key, "2026-01"), key="b" * 64)]:
            with self.assertRaises(RuntimeError):
                self.destination.delete(candidate)
        self.assertTrue(target.exists())

    def test_copy_failure_keeps_staging_and_removes_partial(self):
        with patch.object(backup.shutil, "copy2", side_effect=OSError("disk offline")):
            with self.assertRaises(OSError):
                self.destination.accept(self.path, self.key, "2026-03", len(VIDEO) * 2, [])
        self.assertTrue(self.path.exists())
        self.assertFalse((self.destination_path / (self.path.name + ".part")).exists())

    def test_existing_destination_clip_is_not_copied_twice(self):
        target = self.destination_path / self.path.name
        target.write_bytes(VIDEO)
        files = [item(target, self.key, "2026-03")]
        with patch.object(backup.shutil, "copy2") as copy:
            self.assertEqual(self.destination.accept(self.path, self.key, "2026-03", len(VIDEO) * 2, files), "uploaded")
        copy.assert_not_called()

    def test_oversized_clip_is_not_copied(self):
        self.assertEqual(self.destination.accept(self.path, self.key, "2026-03", 1, []), "oversized")
        self.assertFalse((self.destination_path / self.path.name).exists())

    def test_journal_persists_expired_ids_across_restart(self):
        path = self.root / "journal.sqlite3"
        journal = backup.Journal(path)
        journal.mark("past-clip", "uploaded")
        journal.db.close()
        reopened = backup.Journal(path)
        self.assertTrue(reopened.seen("past-clip"))
        self.assertFalse(reopened.seen("new-clip"))
        reopened.db.close()

    def test_completed_staging_recovers_without_source_manifest(self):
        staging = self.root / "staging"
        staging.mkdir()
        path = staging / self.path.name
        path.write_bytes(VIDEO)
        backup.write_json(path.with_suffix(".meta.json"), {"key": self.key, "recorded": "2026-03"})

        class Module:
            _local_storage = {"manifest": set()}
            async def update_local_storage_manifest(self):
                return False

        journal = backup.Journal(self.root / "journal.sqlite3")
        with patch.object(backup, "STATE", self.root):
            report = asyncio.run(backup.sync_cycle(None, Module(), self.destination, journal,
                {"max_bytes": len(VIDEO) * 2, "max_clips_per_cycle": 50,
                 "download_delay_seconds": 0, "min_free_disk_bytes": 0}))
        self.assertFalse(report["manifest_ok"])
        self.assertTrue(journal.seen(self.key))
        self.assertFalse(path.exists())
        self.assertTrue((self.destination_path / self.path.name).exists())
        journal.db.close()

    def test_interrupted_cleanup_does_not_block_following_cycles(self):
        staging = self.root / "staging"
        staging.mkdir()
        meta = staging / self.path.with_suffix(".meta.json").name
        backup.write_json(meta, {"key": self.key, "recorded": "2026-03"})

        class Module:
            _local_storage = {"manifest": set()}
            async def update_local_storage_manifest(self):
                return False

        journal = backup.Journal(self.root / "journal.sqlite3")
        journal.mark(self.key, "uploaded")
        with patch.object(backup, "STATE", self.root):
            asyncio.run(backup.sync_cycle(None, Module(), self.destination, journal,
                {"max_bytes": len(VIDEO) * 2, "max_clips_per_cycle": 50,
                 "download_delay_seconds": 0, "min_free_disk_bytes": 0}))
        self.assertFalse(meta.exists())
        journal.db.close()


if __name__ == "__main__":
    unittest.main()
