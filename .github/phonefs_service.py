"""Unattended, Telegram-owned PhoneFS sessions for the Windows runner.

Download the user's pinned ZIP from Google Drive (the GitHub copy was removed),
not the setup BAT's unrelated fallback URL. Only the fixed agent is installed;
passwords/configuration are per session.
"""

import argparse
from collections import deque
from dataclasses import dataclass, field
import getpass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# The pinned package now lives on Google Drive (public link). The old GitHub
# copy was deleted, so raw.githubusercontent.com and the GitHub API both 404.
# The shared file is the generated single-file agent itself, not a ZIP.
PACKAGE_FILE_ID = "11ZqoB5wQ0C0Ajj8MMCydDnD3OC_GEGKS"
PACKAGE_URL = f"https://drive.google.com/uc?export=download&id={PACKAGE_FILE_ID}"
# Google Drive serves large or virus-scanned files from a second host after a
# one-time confirmation step; it is the same file id, used as a mirror.
PACKAGE_MIRROR_URLS = (
    f"https://drive.usercontent.google.com/download?export=download&id={PACKAGE_FILE_ID}",
)
PACKAGE_URLS = (PACKAGE_URL, *PACKAGE_MIRROR_URLS)
PACKAGE_SHA256 = "fb90ead072c01158a1ec08c564124a3b36bc33f9dd6a910c8c1e89924f4838e6"
AGENT_NAME = "phonefs_win_FIXED.py"
# CLI/API surface this service invokes; used to confirm a bare downloaded file
# really is the PhoneFS agent before it is installed and later executed.
AGENT_MARKERS = ("set-password", "tunnel", "/api/login")
PREFERRED_PORTS = (8877, 8878, 8879, 8890, 8891, 8892)
USER_AGENT = "telegram-code-runner/phonefs"
# Bounded read: a truncated download fails the checksum instead of installing
# a partial archive. The pinned ZIP is far smaller than this cap.
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
ZIP_MAGIC = b"PK\x03\x04"
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]{0,62}\.trycloudflare\.com", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INSTALL_LOCK = threading.Lock()
# The bundle otherwise announces each URL to a hard-coded, public Filebin.
# Telegram is the only announcement channel for sessions managed by this bot.
MAILBOX_START = "    threading.Thread(target=_announce, daemon=True).start()"
MAILBOX_DISABLED = "    # URL announcements are handled privately by the Telegram runner."


def default_install_dir():
    return Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "telegram-code-runner" / "phonefs"


def _verify_pinned(data):
    """Refuse anything that is not byte-for-byte the pinned package."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != PACKAGE_SHA256:
        detail = "; downloaded content starts with: " + repr(data[:120])
        raise RuntimeError(
            "PhoneFS package checksum mismatch; refusing to execute it "
            f"(downloaded sha256={digest}, pinned={PACKAGE_SHA256}){detail}. "
            "If the file was legitimately replaced, update PACKAGE_SHA256 "
            "in .github/phonefs_service.py and re-run the workflow."
        )


def _agent_from_zip(archive):
    """Read the single named agent out of the verified ZIP; extract nothing else."""
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        # Read one explicitly named file. Do not extract arbitrary ZIP paths.
        members = [info for info in bundle.infolist()
                   if PurePosixPath(info.filename).name == AGENT_NAME and not info.is_dir()]
        if len(members) != 1:
            raise RuntimeError(f"PhoneFS ZIP must contain exactly one {AGENT_NAME}")
        return bundle.read(members[0]).decode("utf-8")


def _agent_source(package):
    """Accept either the pinned ZIP or the generated single-file agent itself."""
    _verify_pinned(package)
    if package[:4] == ZIP_MAGIC:
        source = _agent_from_zip(package)
    else:
        # The Google Drive file is the generated agent script, not an archive.
        try:
            source = package.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                "Downloaded PhoneFS package is neither a ZIP nor a UTF-8 agent script: "
                + str(exc)) from exc
        missing = [marker for marker in AGENT_MARKERS if marker not in source]
        if missing:
            raise RuntimeError(
                "Downloaded PhoneFS file is not the expected agent; missing: "
                + ", ".join(missing))
    if source.count(MAILBOX_START) != 1:
        raise RuntimeError("Unexpected PhoneFS build: cannot disable the public URL mailbox safely")
    source = source.replace(MAILBOX_START, MAILBOX_DISABLED)
    compile(source, AGENT_NAME, "exec")
    return source.encode("utf-8")


def _cookie_header(response):
    """Collect Set-Cookie pairs so the confirmed download keeps Drive's session."""
    headers = getattr(response, "headers", None)
    raw = headers.get_all("Set-Cookie") if headers is not None else None
    pairs = []
    for cookie in raw or []:
        pair = cookie.split(";", 1)[0].strip()
        if pair:
            pairs.append(pair)
    return "; ".join(pairs) or None


def _fetch(url, timeout, cookie=None):
    """GET one URL; return (body, cookie header for the follow-up request)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_PACKAGE_BYTES + 1)
        return data, _cookie_header(response)


def _is_interstitial(data):
    """True when Google Drive answered with an HTML page instead of the file."""
    head = data[:2048].lstrip().lower()
    return (head.startswith(b"<!doctype html") or head.startswith(b"<html")
            or b"<form" in head or b"<input" in head)


def _confirm_form(html):
    """Return (action, hidden fields) of a Drive confirmation form, if present."""
    match = re.search(r"<form[^>]*action=[\"']([^\"']+)[\"'][^>]*>", html, re.I)
    action = match.group(1) if match else None
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", html, re.I):
        name = re.search(r"\bname=[\"']([^\"']+)[\"']", tag, re.I)
        if not name:
            continue
        value = re.search(r"\bvalue=[\"']([^\"']*)[\"']", tag, re.I)
        fields[name.group(1)] = value.group(1) if value else ""
    return action, fields


def _confirmed_url(url, page):
    """Build the download URL behind Drive's 'scan anyway / download anyway' page."""
    html = page[:65536].decode("utf-8", "replace")
    action, fields = _confirm_form(html)
    token = fields.get("confirm")
    if not token:
        match = re.search(r"[?&]confirm=([^&\"'<>\s]+)", html)
        token = match.group(1) if match else None
    if not token:
        return None
    query = {"id": fields.get("id") or PACKAGE_FILE_ID, "export": "download", "confirm": token}
    if fields.get("uuid"):
        query["uuid"] = fields["uuid"]
    base = urllib.parse.urljoin(url, action) if action else url
    separator = "&" if urllib.parse.urlparse(base).query else "?"
    return base + separator + urllib.parse.urlencode(query)


def _download_package(url, timeout=30):
    """Fetch the pinned ZIP from Google Drive, following its confirmation step."""
    data, cookie = _fetch(url, timeout)
    if data[:4] != ZIP_MAGIC and _is_interstitial(data):
        confirmed = _confirmed_url(url, data)
        if confirmed:
            data, _ = _fetch(confirmed, timeout, cookie=cookie)
        if data[:4] != ZIP_MAGIC and _is_interstitial(data):
            raise urllib.error.URLError(
                f"{url} kept returning a Google Drive confirmation page instead "
                "of the PhoneFS ZIP (file may be private, rate-limited, or too "
                "large for Drive to scan)")
    return data


def install_phonefs(install_dir=None):
    """Install a checksum-verified bundle outside the checkout; reuse verified cache."""
    directory = Path(install_dir) if install_dir is not None else default_install_dir()
    archive_path = directory / "package.bin"
    agent_path = directory / AGENT_NAME
    with INSTALL_LOCK:
        archive = None
        if archive_path.is_file():
            cached = archive_path.read_bytes()
            if hashlib.sha256(cached).hexdigest() == PACKAGE_SHA256:
                archive = cached
        if archive is None:
            failures = []
            for url in PACKAGE_URLS:
                try:
                    data = _download_package(url)
                    # Check *before* caching or executing downloaded code.
                    _verify_pinned(data)
                    archive = data
                    break
                except (OSError, urllib.error.URLError) as exc:
                    failures.append(str(exc))
            if archive is None:
                raise RuntimeError("Could not download the pinned PhoneFS ZIP: " + "; ".join(failures))
        source = _agent_source(archive)
        directory.mkdir(parents=True, exist_ok=True)
        # Rebuild the installed source from the verified ZIP even if it was edited.
        for destination, content in ((archive_path, archive), (agent_path, source)):
            if not destination.is_file() or destination.read_bytes() != content:
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                temporary.write_bytes(content)
                os.replace(temporary, destination)
        return agent_path.resolve()


def generate_password():
    # 144 random bits, with all four required character classes guaranteed.
    return "Aa1-" + secrets.token_urlsafe(18)


def choose_port():
    for port in (*PREFERRED_PORTS, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
                return listener.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free local port for PhoneFS")


def child_environment():
    env = os.environ.copy()
    for name in ("TELEGRAM_BOT_TOKEN", "ALLOWED_CHAT_ID", "GITHUB_TOKEN", "GH_TOKEN",
                 "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        env.pop(name, None)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
    return env


def process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def terminate_process_tree(process):
    """Stop only this service's process tree, never all cloudflared processes."""
    if process is None:
        return
    try:
        if os.name == "nt":
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=8,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        if os.name != "nt":
            # The parent may have exited before its tunnel child did.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _login_ready(port, password):
    """Verify the exact password being sent to Telegram, not just an open socket."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/login",
        data=json.dumps({"password": password, "device": "telegram-startup-check"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        return result.get("ok") is True and bool(result.get("csrf"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


@dataclass
class _Session:
    chat_id: int
    password: str = field(default_factory=generate_password, repr=False)
    user_id: str = field(default_factory=getpass.getuser)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    state: str = "starting"
    port: int | None = None
    url: str | None = None
    error: str | None = None
    process: object = field(default=None, repr=False)
    thread: object = field(default=None, repr=False)
    config_dir: Path | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)


class PhoneFSService:
    def __init__(self, notify, cloudflared, desktop_url=lambda: None, install_dir=None):
        self.notify = notify
        self.cloudflared = cloudflared
        self.desktop_url = desktop_url
        self.install_dir = install_dir
        self._lock = threading.RLock()
        self._session = None

    @staticmethod
    def _redact(session, text):
        text = ANSI_RE.sub("", str(text))
        return text.replace(session.password, "[redacted]") if session.password else text

    def _send(self, session, text):
        with self._lock:
            if self._session is session and not session.stop_event.is_set():
                self.notify(session.chat_id, text)

    def _connection_text(self, session):
        lines = ["PhoneFS online", f"PhoneFS URL: {session.url}",
                 f"PhoneFS local port: {session.port}",
                 f"User ID (VM username): {session.user_id}",
                 f"Generated password: {session.password}",
                 "PhoneFS login is password-only; no username is required."]
        desktop = self.desktop_url()
        if desktop:
            lines += [f"Screen sharing URL: {desktop}", "Screen sharing local port: 5000"]
        lines += ["Keep these links and credentials private.",
                  "Use livestream status to see them again, livestream restart for new links/password,",
                  "or stop stream to close both tunnels. Links last at most this workflow run (6 hours)."]
        return "\n".join(lines)

    def status_text(self, chat_id):
        with self._lock:
            session = self._session
            if session is None:
                return "PhoneFS: stopped. Send livestream to start screen sharing + PhoneFS."
            if session.chat_id != chat_id:
                return "PhoneFS is controlled by another Telegram chat; credentials are private."
            if session.state == "online":
                return self._connection_text(session)
            lines = [f"PhoneFS: {session.state}"]
            if session.port:
                lines.append(f"Local port: {session.port}")
            if session.error:
                lines.append(f"Error: {session.error}")
            if session.state in ("failed", "stopped"):
                lines.append("Send livestream to retry, or livestream restart to restart both services.")
            return "\n".join(lines)

    def start(self, chat_id):
        with self._lock:
            existing = self._session
            if existing and existing.chat_id != chat_id:
                self.notify(chat_id, "PhoneFS is controlled by another Telegram chat; credentials are private.")
                return False
            if (existing and not existing.stop_event.is_set()
                    and existing.thread and existing.thread.is_alive()):
                self.notify(chat_id, self.status_text(chat_id))
                return False
            session = _Session(chat_id=chat_id)
            self._session = session
            session.thread = threading.Thread(target=self._run, args=(session,), daemon=True,
                                              name="PhoneFS")
            self.notify(chat_id, "Setting up PhoneFS automatically on a second port; its link and generated password will follow.")
            session.thread.start()
            return True

    def stop(self, chat_id=None):
        with self._lock:
            session = self._session
            if not session:
                return True
            if chat_id is not None and session.chat_id != chat_id:
                return False
            session.stop_event.set()
            session.state = "stopping"
            process, thread = session.process, session.thread
        terminate_process_tree(process)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            session.url = None
            session.state = "stopped"
        return True

    def restart(self, chat_id):
        if not self.stop(chat_id):
            self.notify(chat_id, "PhoneFS is controlled by another Telegram chat; credentials are private.")
            return False
        # A stopped installer may still be finishing a download. Its own stop
        # event prevents it from launching a process or announcing stale secrets.
        with self._lock:
            self._session = None
        return self.start(chat_id)

    def _run(self, session):
        try:
            session.state = "installing"
            agent = install_phonefs(self.install_dir)
            if session.stop_event.is_set():
                return
            cfd = self.cloudflared()
            if not (shutil.which(cfd) or Path(cfd).is_file()):
                raise RuntimeError("cloudflared is not installed; re-run the Telegram Runner workflow")
            session.config_dir = Path(tempfile.mkdtemp(prefix="session-", dir=agent.parent))
            # A fresh config directory prevents an earlier password from silently
            # overriding --password (the bundle only honors that flag on first run).
            result = subprocess.run(
                [sys.executable, "-u", str(agent), "--config-dir", str(session.config_dir), "set-password"],
                input=session.password + "\n", capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                env=child_environment(), cwd=agent.parent, **process_options(),
            )
            if result.returncode:
                detail = self._redact(session, result.stderr or result.stdout)[-1200:]
                raise RuntimeError("PhoneFS password setup failed: " + detail)
            if session.stop_event.is_set():
                return
            for attempt in range(1, 4):
                session.port = choose_port()
                session.state = "connecting"
                session.error = None
                session.logs.clear()
                code, was_online = self._attempt(session, agent, cfd, attempt)
                if session.stop_event.is_set():
                    return
                session.url = None
                if was_online:
                    # Respect 'End sharing now' and the bundle's six-hour expiry;
                    # do not silently reopen a tunnel after the user closed it.
                    session.state = "stopped" if code == 0 else "failed"
                    self._send(session, f"PhoneFS stopped (exit {code}). Screen sharing is unchanged. Send livestream to start PhoneFS again.")
                    return
                if attempt < 3:
                    self._send(session, f"PhoneFS could not connect (attempt {attempt}/3). Retrying with a separate tunnel; screen sharing is unchanged.")
                    if session.stop_event.wait(3):
                        return
            raise RuntimeError(session.error or "PhoneFS tunnel failed to start")
        except Exception as exc:
            session.error = self._redact(session, exc)
            session.state = "failed"
            self._send(session, "PhoneFS setup failed: " + session.error + "\nScreen sharing is unchanged. Send livestream to retry or livestream status for diagnostics.")
        finally:
            terminate_process_tree(session.process)
            session.process = None
            session.url = None
            if session.config_dir:
                shutil.rmtree(session.config_dir, ignore_errors=True)
            if session.stop_event.is_set():
                session.state = "stopped"

    def _attempt(self, session, agent, cfd, attempt):
        command = [sys.executable, "-u", str(agent), "--config-dir", str(session.config_dir),
                   "tunnel", "--port", str(session.port), "--hours", "6",
                   "--cloudflared", cfd, "--prefix", "", "--no-qr", "--no-pty", "--verbose"]
        if attempt > 1:
            command += ["--protocol", "http2"]
        output = queue.Queue()
        # Launch and publish the process handle atomically with respect to stop().
        with self._lock:
            if session.stop_event.is_set():
                return 0, False
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1,
                                       env=child_environment(), cwd=agent.parent, **process_options())
            session.process = process
        print(f"[phonefs] Starting verified agent on local port {session.port}", flush=True)

        def pump():
            try:
                for line in process.stdout:
                    output.put(line)
            finally:
                output.put(None)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + 115  # the bundle waits up to 90s for cloudflared
        candidate = None
        online = False
        next_probe = 0
        try:
            while not session.stop_event.is_set():
                try:
                    line = output.get(timeout=0.5)
                except queue.Empty:
                    line = None
                if line:
                    clean = self._redact(session, line.strip())
                    session.logs.append(clean)
                    # Keep diagnostics for the owning Telegram chat only.
                    # Raw output includes the public URL (sometimes wrapped
                    # across banner lines); do not publish it in Actions logs.
                    match = URL_RE.search(clean)
                    if match:
                        candidate = match.group(0) + "/"
                code = process.poll()
                if code is not None:
                    if not online or code != 0:
                        phase = "before startup" if not online else "after connecting"
                        session.error = "Agent exited %s (exit %s). %s" % (
                            phase, code, "\n".join(session.logs)[-1500:])
                    return code, online
                if candidate and not online and time.monotonic() >= next_probe:
                    next_probe = time.monotonic() + 1
                    if _login_ready(session.port, session.password):
                        with self._lock:
                            if session.stop_event.is_set():
                                return 0, False
                            session.url = candidate
                            session.state = "online"
                            online = True
                            print("[phonefs] Login verified; notifying the owning Telegram chat", flush=True)
                            self._send(session, self._connection_text(session))
                if not online and time.monotonic() >= deadline:
                    session.error = "Timed out waiting for a PhoneFS URL and a successful login. " + "\n".join(session.logs)[-1500:]
                    return 2, False
            return 0, online
        finally:
            terminate_process_tree(process)
            if process.stdout:
                process.stdout.close()
            with self._lock:
                if session.process is process:
                    session.process = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare the pinned PhoneFS Windows agent")
    parser.add_argument("command", choices=["install"])
    parser.add_argument("--install-dir", default=None)
    args = parser.parse_args()
    path = install_phonefs(args.install_dir)
    print(f"PhoneFS fixed Windows agent verified and installed: {path}")
