import asyncio
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock, AsyncMock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup
import web_app


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(backup, "ROOT", self.root)
        self.state_patch = patch.object(backup, "STATE", self.root / "state")
        self.root_patch.start()
        self.state_patch.start()
        backup.write_json(self.root / "settings.json", {"folder_name": "Blink Rolling Backup",
            "folder_id": None, "module_id": None, "max_bytes": 10_000_000_000,
            "max_staging_bytes": 10_000_000_000, "poll_seconds": 60,
            "download_delay_seconds": 2, "max_clips_per_cycle": 50})
        self.controller = web_app.Controller()
        self.app = web_app.create_app(self.controller)
        self.app.testing = True
        self.client = self.app.test_client()
        self.origin = "http://127.0.0.1:8765"
        page = self.client.get("/", base_url=self.origin)
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]

    def tearDown(self):
        self.controller.close()
        self.root_patch.stop()
        self.state_patch.stop()
        self.tmp.cleanup()

    def get(self, url):
        return self.client.get(url, base_url=self.origin)

    def post(self, url, data):
        return self.client.post(url, json=data, base_url=self.origin,
            headers={"Origin": self.origin, "X-CSRF-Token": self.csrf})

    def test_dashboard_renders_and_static_assets_exist(self):
        home = self.get("/")
        self.assertIn("Blink Vault", home.text)
        self.assertIn("script-src 'self'", home.headers["Content-Security-Policy"])
        for asset in ("app.js", "app.css"):
            with self.get("/static/" + asset) as response:
                self.assertEqual(response.status_code,200)

    def test_untrusted_hosts_and_cross_origin_mutations_are_rejected(self):
        self.assertEqual(self.client.get("/api/status", base_url="http://evil.example:8765").status_code,403)
        missing = self.client.post("/api/stop", json={}, base_url=self.origin, headers={"Origin":self.origin})
        self.assertEqual(missing.status_code, 403)
        cross = self.client.post("/api/stop", json={}, base_url=self.origin,
            headers={"Origin":"https://evil.example", "X-CSRF-Token":self.csrf})
        self.assertEqual(cross.status_code,403)

    def test_status_does_not_expose_saved_passwords_or_tokens(self):
        backup.write_json(backup.STATE / "blink_auth.json", {"password":"PRIVATE-PASSWORD", "token":"PRIVATE-TOKEN"})
        backup.write_json(backup.STATE / "google_token.json", {"refresh_token":"PRIVATE-REFRESH"})
        response = self.get("/api/status")
        self.assertEqual(response.status_code,200)
        self.assertNotIn("PRIVATE", response.text)
        self.assertTrue(response.json["auth"]["blink_saved"])
        self.assertEqual(self.get("/state/blink_auth.json").status_code,404)

    def test_settings_validate_and_keep_existing_folder_identity(self):
        settings = backup.read_json(self.root / "settings.json")
        data = {k:settings[k] for k in ["max_bytes","max_staging_bytes","poll_seconds","download_delay_seconds","max_clips_per_cycle"]}
        bad = dict(data,poll_seconds=1)
        self.assertEqual(self.post("/api/settings",bad).status_code,400)
        data["max_bytes"] = 5_000_000_000
        self.assertEqual(self.post("/api/settings",data).status_code,200)
        saved = backup.read_json(self.root / "settings.json")
        self.assertEqual(saved["max_bytes"],5_000_000_000)
        self.assertIn("folder_id",saved)
        with patch.object(self.controller,"busy",return_value=True):
            self.assertEqual(self.post("/api/settings",data).status_code,400)

    def test_queue_filtering_retry_and_missing_status(self):
        journal = backup.Journal(backup.STATE / "journal.sqlite3")
        journal.discover("one","Camera A.mp4","2026-10-27T10:00:00+00:00",100)
        journal.discover("two","Camera B.mp4","2026-10-27T11:00:00+00:00",100)
        journal.fail("one","download","offline")
        journal.mark("two","uploaded")
        journal.db.close()
        result = self.get("/api/clips?stage=download&search=Camera+A").json
        self.assertEqual(result["total"],1)
        self.assertEqual(result["jobs"][0]["key"],"one")
        response = self.post("/api/retry",{"key":"one"})
        self.assertEqual(response.json["affected"],1)
        with closing(sqlite3.connect(backup.STATE / "journal.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT next_download FROM jobs WHERE key='one'").fetchone()[0],0)

    def test_worker_start_requires_setup_and_stop_requests_graceful_exit(self):
        self.assertEqual(self.post("/api/start",{}).status_code,400)
        with patch.object(self.controller,"busy",return_value=True):
            self.assertEqual(self.post("/api/stop",{}).status_code,200)
        self.assertTrue(backup.stop_requested())
        self.assertFalse(self.controller.desired)

    def test_google_callback_rejects_unmatched_state_without_contacting_google(self):
        self.controller.oauth = {"state":"expected", "started":web_app.time.time(), "flow":Mock()}
        response = self.get("/oauth/callback?state=wrong&code=secret-code")
        self.assertEqual(response.status_code,400)
        self.controller.oauth["flow"].fetch_token.assert_not_called()

    def test_google_desktop_client_upload_is_validated_and_not_returned(self):
        invalid = {"installed":{"client_id":"test","client_secret":"secret","auth_uri":"https://evil.example/auth","token_uri":"https://evil.example/token"}}
        self.assertEqual(self.post("/api/setup/google-client",{"client":invalid}).status_code,400)
        valid = {"installed":{"client_id":"test","client_secret":"LOCAL-SECRET","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}
        self.assertEqual(self.post("/api/setup/google-client",{"client":valid}).status_code,200)
        self.assertNotIn("LOCAL-SECRET", self.get("/api/status").text)

    def test_module_selection_must_match_discovered_module(self):
        fake = Mock()
        fake.modules = [{"id":"123", "name":"XR+"}]
        self.controller.blink_setup = fake
        self.assertEqual(self.post("/api/setup/module",{"module_id":"999"}).status_code,400)
        self.assertEqual(self.post("/api/setup/module",{"module_id":"123"}).status_code,200)
        self.assertEqual(backup.read_json(self.root / "settings.json")["module_id"],"123")
        self.controller.blink_setup = None

    def test_stop_interrupts_poll_wait_without_losing_queue(self):
        (backup.STATE / "stop.request").write_text("stop")
        start = web_app.time.monotonic()
        asyncio.run(backup.interruptible_sleep(600))
        self.assertLess(web_app.time.monotonic() - start,1)

    def test_google_callback_saves_authorization_and_returns_to_dashboard(self):
        flow = Mock()
        flow.credentials.to_json.return_value = json.dumps({"refresh_token":"SAVED-SECRET"})
        self.controller.oauth = {"state":"expected", "started":web_app.time.time(), "flow":flow}
        with self.client.session_transaction(base_url=self.origin) as cookie:
            cookie["oauth_state"] = "expected"
        with patch.object(backup,"google_service"), patch.object(backup,"ensure_folder") as folder:
            result = self.get("/?state=expected&code=test-code")
        self.assertEqual(result.status_code,302)
        self.assertEqual(result.headers["Location"],"/#setup")
        self.assertEqual(backup.read_json(backup.STATE / "google_token.json")["refresh_token"],"SAVED-SECRET")
        self.assertIsNone(self.controller.oauth)
        folder.assert_called_once()

    def test_blink_setup_keeps_session_for_two_factor_verification(self):
        from blinkpy.auth import BlinkTwoFARequiredError
        fake = SimpleNamespace(start=AsyncMock(side_effect=BlinkTwoFARequiredError()),
            refresh=AsyncMock(),send_2fa_code=AsyncMock(return_value=True),available=True,
            sync={"XR+":SimpleNamespace(sync_id=123,name="XR+",local_storage=True)})
        setup = web_app.BlinkSetup()
        try:
            with patch("blinkpy.blinkpy.Blink",return_value=fake):
                first = setup.call(setup.begin("owner@example.com","password"))
                self.assertTrue(first["needs_code"])
                self.assertIsNotNone(setup.http)
                result = setup.call(setup.verify("123456"))
            self.assertEqual(result["modules"],[{"id":"123","name":"XR+"}])
            self.assertFalse(result["needs_code"])
            self.assertIsNone(setup.http)
            self.assertTrue((backup.STATE / "blink_auth.json").exists())
        finally:
            setup.shutdown()

    def test_dashboard_launches_worker_without_a_second_interactive_login(self):
        fake = Mock()
        fake.poll.return_value = None
        with patch.object(self.controller,"configured",return_value=True), patch("web_app.subprocess.Popen",return_value=fake) as spawn:
            result = self.post("/api/start",{})
        self.assertEqual(result.status_code,200)
        self.assertTrue(self.controller.desired)
        self.assertEqual(spawn.call_args.args[0][-2:],["--run","--managed"])
        self.assertEqual(self.post("/api/start",{}).status_code,400)

    def test_supervisor_restarts_failed_worker_after_backoff(self):
        self.controller.desired = True
        self.controller.process = Mock(returncode=1)
        self.controller.shutdown = Mock()
        self.controller.shutdown.wait.side_effect = [False,False,True]
        with patch.object(self.controller,"busy",return_value=False), patch.object(self.controller,"start") as start, patch("web_app.time.time",side_effect=[100,161]):
            self.controller.supervise()
        start.assert_called_once()
        self.assertEqual(self.controller.last_exit,1)
        self.controller.process = None


if __name__ == "__main__":
    unittest.main()
