import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blinkpy.auth import Auth, TokenRefreshFailed
import backup
import blink_compat


class OAuthRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        blink_compat.install_blinkpy_2fa_compatibility_fix()
        self.session = Mock()
        response = Mock(status=200)
        response.json = AsyncMock(return_value={"manifest": "available"})
        self.session.get = AsyncMock(return_value=response)
        self.auth = Auth({"username": "test@example.invalid", "password": "test-password",
                          "token": "expired-access", "refresh_token": "saved-refresh",
                          "expiration_date": 1, "hardware_id": "saved-device",
                          "host": "example.invalid", "region_id": "test", "account_id": 123},
                         no_prompt=True, session=self.session)
        self.auth.login = AsyncMock(side_effect=AssertionError("Legacy login must not run"))

    async def query(self):
        return await self.auth.query("https://example.invalid/manifest", headers=self.auth.header)

    async def test_expired_query_uses_v2_and_persists_rotation_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blink_auth.json"
            self.auth.callback = lambda: backup.write_json(path, self.auth.login_attributes)

            async def source_request(**kwargs):
                saved = backup.read_json(path)
                self.assertEqual(saved["refresh_token"], "rotated-refresh")
                self.assertEqual(saved["hardware_id"], "saved-device")
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer new-access")
                return Mock(status=200, json=AsyncMock(return_value={"ok": True}))

            self.session.get.side_effect = source_request
            with patch.object(blink_compat.blink_api, "oauth_refresh_token", AsyncMock(return_value={
                    "access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 3600})) as refresh:
                self.assertEqual(await self.query(), {"ok": True})
            refresh.assert_awaited_once_with(self.auth, "saved-refresh", "saved-device")
        self.auth.login.assert_not_awaited()
        self.assertFalse(self.auth.need_refresh())
        self.assertFalse(self.auth.is_errored)

    async def test_refresh_response_can_keep_existing_refresh_token(self):
        with patch.object(blink_compat.blink_api, "oauth_refresh_token", AsyncMock(return_value={
                "access_token": "new-access"})):
            await self.query()
        self.assertEqual(self.auth.refresh_token, "saved-refresh")
        self.assertEqual(self.auth.expires_in, 3600)

    async def test_failed_or_malformed_refresh_preserves_credentials(self):
        responses = [None, {}, {"access_token": "new", "expires_in": 0},
                     {"access_token": "new", "expires_in": "bad"},
                     {"access_token": "new", "expires_in": float("inf")},
                     {"access_token": "new", "refresh_token": None}]
        self.auth.callback = Mock()
        for response in responses:
            with self.subTest(response=response):
                with patch.object(blink_compat.blink_api, "oauth_refresh_token", AsyncMock(return_value=response)):
                    with self.assertRaises(TokenRefreshFailed):
                        await self.query()
                self.assertEqual(self.auth.token, "expired-access")
                self.assertEqual(self.auth.refresh_token, "saved-refresh")
                self.assertEqual(self.auth.expiration_date, 1)
        self.session.get.assert_not_awaited()
        self.auth.callback.assert_not_called()

    async def test_transient_failure_retries_without_password_login(self):
        self.auth.callback = Mock()
        with patch.object(blink_compat.blink_api, "oauth_refresh_token", AsyncMock(side_effect=[
                OSError("network unavailable"), {"access_token": "recovered", "expires_in": 3600}])) as refresh:
            with self.assertRaises(TokenRefreshFailed):
                await self.query()
            self.assertEqual(await self.query(), {"manifest": "available"})
        self.assertEqual(refresh.await_count, 2)
        self.auth.login.assert_not_awaited()
        self.auth.callback.assert_called_once()

    async def test_concurrent_queries_refresh_rotating_token_only_once(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def refresh_response(*args):
            started.set()
            await release.wait()
            return {"access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 3600}

        with patch.object(blink_compat.blink_api, "oauth_refresh_token", AsyncMock(side_effect=refresh_response)) as refresh:
            first = asyncio.create_task(self.query())
            await started.wait()
            second = asyncio.create_task(self.query())
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)
        self.assertEqual(results, [{"manifest": "available"}] * 2)
        refresh.assert_awaited_once()
        for call in self.session.get.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer new-access")

    async def test_non_refresh_call_preserves_original_behavior(self):
        with patch.object(blink_compat._oauth_refresh_tokens, "_original", AsyncMock(return_value=True)) as original:
            self.assertTrue(await self.auth.refresh_tokens(refresh=False))
        original.assert_awaited_once_with(self.auth, refresh=False)

    async def test_compatibility_installation_is_idempotent(self):
        refresh = Auth.refresh_tokens
        original = blink_compat._oauth_refresh_tokens._original
        blink_compat.install_blinkpy_2fa_compatibility_fix()
        self.assertIs(Auth.refresh_tokens, refresh)
        self.assertIs(blink_compat._oauth_refresh_tokens._original, original)

    async def test_http_202_two_factor_workaround_still_releases_response(self):
        for status, expected in [(202, "2FA_REQUIRED"), (412, "2FA_REQUIRED"), (302, "SUCCESS")]:
            with self.subTest(status=status):
                response = Mock(status=status)
                self.session.post = AsyncMock(return_value=response)
                result = await blink_compat.blink_api.oauth_signin(self.auth, "test", "password", "csrf")
                self.assertEqual(result, expected)
                response.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
