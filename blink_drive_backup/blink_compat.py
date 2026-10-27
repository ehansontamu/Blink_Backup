"""Retains the OAuth HTTP-202 fix from your working downloader."""
import logging
from blinkpy import api as blink_api
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


def install_blinkpy_2fa_compatibility_fix() -> None:
    """Patch the blinkpy 0.26 beta 202/2FA compatibility gap for this app."""
    original = getattr(blink_api, "oauth_signin", None)
    if original is not None and not getattr(original, "_local_202_fix", False):
        _oauth_signin_accepts_202._local_202_fix = True
        blink_api.oauth_signin = _oauth_signin_accepts_202


