"""OAuth HTTP-202 and runtime refresh fixes for the pinned BlinkPy beta."""
import asyncio
import logging
import math
from blinkpy import api as blink_api
from blinkpy.auth import Auth, TokenRefreshFailed
from blinkpy.helpers.constants import OAUTH_SIGNIN_URL, OAUTH_USER_AGENT

async def _oauth_signin_accepts_202(auth, email: str, password: str, csrf_token: str):
    """Handle Blink's alternate HTTP 202 response for a required 2FA PIN."""
    headers = {
        "User-Agent": OAUTH_USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://api.oauth.blink.com",
        "Referer": OAUTH_SIGNIN_URL,
    }
    response = await auth.session.post(
        OAUTH_SIGNIN_URL,
        headers=headers,
        data={"username": email, "password": password, "csrf-token": csrf_token},
        allow_redirects=False,
    )
    try:
        if response.status in (202, 412):
            return "2FA_REQUIRED"
        if response.status in (301, 302, 303, 307, 308):
            return "SUCCESS"
        logging.getLogger(__name__).warning("Blink sign-in was not accepted (HTTP %s).", response.status)
        return None
    finally:
        response.release()


async def _oauth_refresh_tokens(auth, refresh=False):
    """Keep runtime refresh on the OAuth v2 flow already used by startup.

    BlinkPy 0.26.0b0's query() calls the legacy refresh_tokens() when an access
    token expires, even though startup() obtains OAuth v2 tokens. Use its v2
    refresh API instead, without starting a password/2FA login on each failure.
    """
    if not refresh:
        return await _oauth_refresh_tokens._original(auth, refresh=False)
    lock = getattr(auth, "_blink_vault_refresh_lock", None)
    if lock is None:
        lock = auth._blink_vault_refresh_lock = asyncio.Lock()
    async with lock:
        # Other requests may have refreshed this same Auth while we waited.
        if not auth.need_refresh():
            return True
        auth.is_errored = True
        try:
            if not auth.refresh_token or not auth.hardware_id:
                raise TokenRefreshFailed
            data = await blink_api.oauth_refresh_token(auth, auth.refresh_token, auth.hardware_id)
            if not isinstance(data, dict):
                raise TokenRefreshFailed
            token = data.get("access_token")
            refresh_token = data.get("refresh_token", auth.refresh_token)
            expires = data.get("expires_in", 3600)
            if (not isinstance(token, str) or not token
                    or not isinstance(refresh_token, str) or not refresh_token
                    or isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or expires <= 0):
                raise TokenRefreshFailed
            # Validate all fields before mutating saved credentials. OAuth may
            # omit a new refresh token when the existing one remains valid.
            auth.login_response = dict(data, refresh_token=refresh_token, expires_in=expires)
            auth.extract_login_info()
            auth.is_errored = False
        except TokenRefreshFailed:
            raise
        except Exception as error:
            # Keep external response bodies/URLs out of dashboard messages.
            raise TokenRefreshFailed from error
        return True


def install_blinkpy_2fa_compatibility_fix() -> None:
    """Install the app's 202/2FA and runtime OAuth refresh compatibility fixes."""
    original = getattr(blink_api, "oauth_signin", None)
    if original is not None and not getattr(original, "_local_202_fix", False):
        _oauth_signin_accepts_202._local_202_fix = True
        blink_api.oauth_signin = _oauth_signin_accepts_202
    original_refresh = Auth.refresh_tokens
    if not getattr(original_refresh, "_blink_vault_oauth_refresh_fix", False):
        _oauth_refresh_tokens._original = original_refresh
        _oauth_refresh_tokens._blink_vault_oauth_refresh_fix = True
        Auth.refresh_tokens = _oauth_refresh_tokens


