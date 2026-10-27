"""Local browser dashboard. Run python web_app.py --open --autostart."""
import argparse
import asyncio
import atexit
import concurrent.futures
import json
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, session
from filelock import FileLock, Timeout
import backup


class BlinkSetup:
    """Keep the same async Blink session alive across login and 2FA requests."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.lock = threading.RLock()
        self.client = self.http = None
        self.waiting = False
        self.modules = []

    def call(self, coroutine):
        with self.lock:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            try:
                return future.result(timeout=210)
            except concurrent.futures.TimeoutError:
                future.cancel()
                asyncio.run_coroutine_threadsafe(self.close(), self.loop)
                raise RuntimeError("Blink sign-in timed out. Check your connection and try again.") from None

    async def close(self):
        if self.http:
            await self.http.close()
        self.http = self.client = None
        self.waiting = False

    def shutdown(self):
        if self.loop.is_closed():
            return
        self.call(self.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)
        if not self.loop.is_running():
            self.loop.close()

    async def begin(self, email, password):
        from aiohttp import ClientSession, ClientTimeout
        from blinkpy.auth import Auth, BlinkTwoFARequiredError
        from blinkpy.blinkpy import Blink
        from blink_compat import install_blinkpy_2fa_compatibility_fix
        await self.close()
        self.modules = []
        install_blinkpy_2fa_compatibility_fix()
        cached = backup.STATE / "blink_auth.json"
        data = backup.read_json(cached) if cached.exists() else {}
        if email and email != data.get("username"):
            data = {"username": email}
        if password:
            data["password"] = password
        if not data.get("username") or not data.get("password"):
            raise ValueError("Enter your Blink email and password.")
        self.http = ClientSession(timeout=ClientTimeout(total=180))
        self.client = Blink(session=self.http)
        self.client.auth = Auth(data, no_prompt=True, session=self.http)
        try:
            started = await self.client.start()
        except BlinkTwoFARequiredError:
            self.waiting = True
            return {"needs_code": True, "modules": []}
        except Exception:
            await self.close()
            raise RuntimeError("Blink sign-in failed. Check your account and connection.") from None
        if not started or not self.client.available:
            await self.close()
            raise RuntimeError("Blink did not accept sign-in. Check credentials and try again.")
        return await self.finish()

    async def verify(self, code):
        if not self.client or not self.waiting:
            raise ValueError("Start Blink sign-in again before entering a code.")
        if not code or len(code) > 16:
            raise ValueError("Enter the verification code Blink sent you.")
        if not await self.client.send_2fa_code(code):
            raise ValueError("Blink rejected the code. Try again or restart sign-in.")
        return await self.finish()

    async def finish(self):
        await self.client.refresh()
        backup.write_json(backup.STATE / "blink_auth.json", self.client.auth.login_attributes)
        self.modules = [{"id": str(m.sync_id), "name": m.name}
                        for m in self.client.sync.values() if m.local_storage]
        modules = list(self.modules)
        await self.close()
        return {"needs_code": False, "modules": modules}


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.desired = False
        self.next_restart = 0
        self.last_exit = None
        self.blink_setup = None
        self.oauth = None
        self.shutdown = threading.Event()

    def setup(self):
        if not self.blink_setup:
            self.blink_setup = BlinkSetup()
        return self.blink_setup

    def busy(self):
        if self.process and self.process.poll() is None:
            return True
        try:
            with FileLock(str(backup.STATE / "process.lock"), timeout=0):
                return False
        except Timeout:
            return True

    def require_stopped(self):
        if self.busy():
            raise ValueError("Stop the backup and wait for it to finish before changing setup or settings.")

    def configured(self):
        s = backup.read_json(backup.ROOT / "settings.json")
        return bool(s.get("folder_id") and s.get("module_id") and
                    (backup.STATE / "google_token.json").exists() and
                    (backup.STATE / "blink_auth.json").exists())

    def start(self):
        with self.lock:
            if self.busy():
                raise ValueError("A backup is already running.")
            if self.blink_setup and self.blink_setup.waiting:
                raise ValueError("Finish Blink verification first.")
            if self.oauth:
                raise ValueError("Finish or cancel Google sign-in first.")
            if not self.configured():
                raise ValueError("Connect Google Drive and Blink, then select your Sync Module in Setup.")
            (backup.STATE / "stop.request").unlink(missing_ok=True)
            console = backup.STATE / "worker_console.log"
            if console.exists() and console.stat().st_size > 1_000_000:
                console.replace(console.with_suffix(".previous.log"))
            with console.open("a", encoding="utf-8") as output:
                self.process = subprocess.Popen(
                    [sys.executable, "-u", str(backup.ROOT / "backup.py"), "--run", "--managed"],
                    cwd=backup.ROOT, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            self.desired = True
            self.next_restart = 0

    def stop(self):
        with self.lock:
            self.desired = False
            if self.busy():
                (backup.STATE / "stop.request").write_text("stop", encoding="utf-8")

    def worker_state(self):
        alive = self.busy()
        return {"running": alive, "stopping": alive and backup.stop_requested(),
                "supervised": bool(self.process and self.process.poll() is None),
                "restart_enabled": self.desired, "last_exit": self.last_exit,
                "restart_in": max(0, round(self.next_restart - time.time())) if self.next_restart else 0}

    def supervise(self):
        while not self.shutdown.wait(2):
            with self.lock:
                if not self.desired or self.busy():
                    continue
                if self.process:
                    self.last_exit = self.process.returncode
                if not self.next_restart:
                    self.next_restart = time.time() + 60
                elif time.time() >= self.next_restart:
                    try:
                        self.start()
                    except Exception:
                        self.next_restart = time.time() + 60

    def close(self):
        self.shutdown.set()
        # Closing the dashboard tab has no effect. Stopping the Python web server
        # requests a clean stop of a worker it owns, without killing transfers.
        if self.process and self.process.poll() is None:
            self.stop()
        if self.blink_setup:
            self.blink_setup.shutdown()


def safe_read(path, default):
    try:
        return backup.read_json(path)
    except (OSError, ValueError):
        return default


def queue_data(search="", stage="", page=1):
    path = backup.STATE / "journal.sqlite3"
    if not path.exists():
        return {"jobs": [], "total": 0, "counts": {}}
    with sqlite3.connect(path, timeout=3) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
            return {"jobs": [], "total": 0, "counts": {}}
        counts = {r[0]: r[1] for r in db.execute("SELECT stage,COUNT(*) FROM jobs GROUP BY stage")}
        clause, args = ["1=1"], []
        if stage:
            clause.append("stage=?")
            args.append(stage)
        if search:
            clause.append("name LIKE ?")
            args.append("%" + search[:120] + "%")
        where = " AND ".join(clause)
        total = db.execute("SELECT COUNT(*) FROM jobs WHERE " + where, args).fetchone()[0]
        jobs = [dict(r) for r in db.execute("""SELECT key,name,recorded,expected_size,stage,
            download_attempts,upload_attempts,next_download,next_upload,last_error,missing_since
            FROM jobs WHERE """ + where + " ORDER BY recorded DESC,key LIMIT 50 OFFSET ?",
            args + [(page - 1) * 50])]
        return {"jobs": jobs, "total": total, "counts": counts}


def log_tail():
    lines = []
    for name in ("worker_console.log", "backup.log"):
        path = backup.STATE / name
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as f:
                # Bounded file read, even if someone replaced the log manually.
                f.seek(max(0, path.stat().st_size - 40000))
                lines.append({"name": name, "lines": f.read().splitlines()[-100:]})
    return lines


def create_app(controller=None, port=8765):
    backup.STATE.mkdir(parents=True, exist_ok=True)
    (backup.STATE / "staging").mkdir(exist_ok=True)
    secret_path = backup.STATE / "ui_secret.key"
    if not secret_path.exists():
        with secret_path.open("x", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
        secret_path.chmod(0o600)
    controller = controller or Controller()
    app = Flask(__name__, static_folder="web/static", template_folder="web/templates")
    app.config.update(SECRET_KEY=secret_path.read_text().strip(), MAX_CONTENT_LENGTH=128_000,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    origin = f"http://127.0.0.1:{port}"

    @app.before_request
    def protect_local_access():
        if request.host != f"127.0.0.1:{port}":
            if request.host == f"localhost:{port}" and request.path == "/" and request.method == "GET":
                return redirect(origin)
            abort(403)
        if request.method != "GET":
            if request.headers.get("Origin") != origin:
                abort(403)
            token = request.headers.get("X-CSRF-Token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf", "")):
                abort(403)
            if not request.is_json:
                abort(415)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return response

    @app.errorhandler(Exception)
    def errors(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, (ValueError, RuntimeError)):
            return jsonify(error=str(error)), 400
        # Do not return arbitrary API exceptions: they may contain tokens or URLs.
        return jsonify(error="The operation failed. Check your connection or retry setup."), 500

    @app.get("/")
    def home():
        if request.args.get("state") or request.args.get("code") or request.args.get("error"):
            return google_callback()
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return render_template("index.html", csrf=session["csrf"])

    @app.get("/api/status")
    def status():
        settings = backup.read_json(backup.ROOT / "settings.json")
        report = safe_read(backup.STATE / "status.json", {})
        queue = queue_data()
        staged = sum(p.stat().st_size for p in (backup.STATE / "staging").glob("*.mp4"))
        return jsonify(worker=controller.worker_state(), configured=controller.configured(),
            settings=settings, report=report, counts=queue["counts"], staging_bytes=staged,
            auth={"google_saved": (backup.STATE / "google_token.json").exists(),
                  "blink_saved": (backup.STATE / "blink_auth.json").exists(),
                  "client_saved": (backup.ROOT / "client_secret.json").exists(),
                  "needs_code": bool(controller.blink_setup and controller.blink_setup.waiting),
                  "modules": controller.blink_setup.modules if controller.blink_setup else []},
            google_pending=bool(controller.oauth))

    @app.get("/api/clips")
    def clips():
        page = min(100000, max(1, request.args.get("page", 1, type=int)))
        return jsonify(queue_data(request.args.get("search", ""), request.args.get("stage", ""), page))

    @app.get("/api/logs")
    def logs():
        return jsonify(logs=log_tail())

    @app.post("/api/start")
    def start():
        controller.start()
        return jsonify(ok=True)

    @app.post("/api/stop")
    def stop():
        controller.stop()
        return jsonify(ok=True)

    @app.post("/api/retry")
    def retry():
        key = request.get_json().get("key")
        with sqlite3.connect(backup.STATE / "journal.sqlite3", timeout=10) as db:
            clause, params = (" AND key=?", (key,)) if key else ("", ())
            result = db.execute("UPDATE jobs SET next_download=0,next_upload=0 WHERE stage IN ('download','upload')" + clause, params)
        return jsonify(ok=True, affected=result.rowcount)

    @app.post("/api/settings")
    def save_settings():
        with controller.lock:
            controller.require_stopped()
            data = request.get_json()
            bounds = {"max_bytes": (1_000_000, 1_000_000_000_000), "max_staging_bytes": (1_000_000, 1_000_000_000_000),
                      "poll_seconds": (60, 3600), "download_delay_seconds": (1, 60), "max_clips_per_cycle": (1, 500)}
            settings = backup.read_json(backup.ROOT / "settings.json")
            for key, (low, high) in bounds.items():
                value = data.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ValueError(f"{key} must be an integer between {low} and {high}.")
                settings[key] = value
            backup.write_json(backup.ROOT / "settings.json", settings)
        return jsonify(ok=True)

    @app.post("/api/setup/google-client")
    def google_client():
        with controller.lock:
            controller.require_stopped()
            data = request.get_json().get("client")
            config = data.get("installed", {}) if isinstance(data, dict) else {}
            if not all(config.get(k) for k in ("client_id", "client_secret", "auth_uri", "token_uri")):
                raise ValueError("Choose a Google Desktop app OAuth client JSON file.")
            for name in ("auth_uri", "token_uri"):
                url = urlparse(config[name])
                if url.scheme != "https" or url.hostname not in {"accounts.google.com", "oauth2.googleapis.com"}:
                    raise ValueError("The OAuth client must use Google's official authentication endpoints.")
            backup.write_json(backup.ROOT / "client_secret.json", data)
            controller.oauth = None
        return jsonify(ok=True)

    @app.post("/api/setup/google")
    def google_begin():
        from google_auth_oauthlib.flow import Flow
        with controller.lock:
            controller.require_stopped()
            path = backup.ROOT / "client_secret.json"
            if not path.exists():
                raise ValueError("Choose your Google Desktop OAuth client JSON file first.")
            flow = Flow.from_client_secrets_file(str(path), scopes=backup.SCOPES,
                redirect_uri=origin + "/", autogenerate_code_verifier=True)
            url, state = flow.authorization_url(access_type="offline", prompt="consent")
            controller.oauth = {"flow": flow, "state": state, "started": time.time()}
            session["oauth_state"] = state
        return jsonify(url=url)

    @app.get("/oauth/callback")
    def google_callback():
        with controller.lock:
            pending = controller.oauth
            state = request.args.get("state", "")
            if (not pending or time.time() - pending["started"] > 600 or not state
                    or not secrets.compare_digest(state, pending["state"])
                    or not secrets.compare_digest(state, session.get("oauth_state", ""))):
                raise ValueError("Google sign-in expired or did not match. Return to Setup and try again.")
            controller.require_stopped()
            try:
                if request.args.get("error"):
                    raise ValueError("Google authorization was declined. You can try again in Setup.")
                code = request.args.get("code")
                if not code:
                    raise ValueError("Google did not provide an authorization code.")
                pending["flow"].fetch_token(code=code, timeout=120)
                backup.write_json(backup.STATE / "google_token.json", json.loads(pending["flow"].credentials.to_json()))
                service = backup.google_service(False)
                backup.ensure_folder(service, backup.read_json(backup.ROOT / "settings.json"), True)
            finally:
                controller.oauth = None
                session.pop("oauth_state", None)
        return redirect("/#setup")

    @app.post("/api/setup/google-folder")
    def google_folder():
        with controller.lock:
            controller.require_stopped()
            service = backup.google_service(False)
            backup.ensure_folder(service, backup.read_json(backup.ROOT / "settings.json"), True)
        return jsonify(ok=True)

    @app.post("/api/setup/blink")
    def blink_begin():
        with controller.lock:
            controller.require_stopped()
            data = request.get_json()
            return jsonify(controller.setup().call(controller.setup().begin(
                str(data.get("email", "")).strip(), str(data.get("password", "")))))

    @app.post("/api/setup/blink-code")
    def blink_code():
        with controller.lock:
            controller.require_stopped()
            return jsonify(controller.setup().call(controller.setup().verify(str(request.get_json().get("code", "")).strip())))

    @app.post("/api/setup/modules")
    def modules():
        with controller.lock:
            controller.require_stopped()
            # Saved credentials can enumerate modules without re-entering passwords.
            result = controller.setup().call(controller.setup().begin("", ""))
            return jsonify(result)

    @app.post("/api/setup/module")
    def select_module():
        with controller.lock:
            controller.require_stopped()
            chosen = str(request.get_json().get("module_id", ""))
            available = controller.setup().modules
            if chosen not in {m["id"] for m in available}:
                raise ValueError("Load modules, then select a module with active local storage.")
            settings = backup.read_json(backup.ROOT / "settings.json")
            settings["module_id"] = chosen
            backup.write_json(backup.ROOT / "settings.json", settings)
        return jsonify(ok=True)

    @app.post("/api/setup/cancel")
    def cancel():
        with controller.lock:
            controller.oauth = None
            if controller.blink_setup:
                controller.blink_setup.call(controller.blink_setup.close())
        return jsonify(ok=True)

    app.controller = controller
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535.")
    from waitress import serve
    app = create_app(port=args.port)
    with FileLock(str(backup.STATE / "dashboard.lock"), timeout=0):
        atexit.register(app.controller.close)
        if args.autostart and app.controller.configured():
            try:
                app.controller.start()
            except ValueError:
                pass
        threading.Thread(target=app.controller.supervise, daemon=True).start()
        url = f"http://127.0.0.1:{args.port}"
        print("Blink backup dashboard:", url)
        print("Keep this process running. Closing the browser tab does not stop backups.")
        if args.open:
            threading.Timer(1, lambda: webbrowser.open(url)).start()
        serve(app, host="127.0.0.1", port=args.port, threads=6)


if __name__ == "__main__":
    main()
