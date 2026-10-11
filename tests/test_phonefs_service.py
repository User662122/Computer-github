"""Offline tests: no Telegram token, Windows VM or public tunnel is required."""

from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github"))
import phonefs_service as pfs


def make_archive(filename=pfs.AGENT_NAME, source=None):
    if source is None:
        source = "def example():\n" + pfs.MAILBOX_START + "\n    return 0\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("PhoneFS_Windows_Agent (1)/phonefs/" + filename, source)
        archive.writestr("../do-not-extract.txt", "not an agent")
    return output.getvalue()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.archive = make_archive()
        self.digest = mock.patch.object(pfs, "PACKAGE_SHA256", hashlib.sha256(self.archive).hexdigest())
        self.digest.start()
        self.addCleanup(self.digest.stop)

    def test_downloads_fixed_file_and_disables_public_mailbox(self):
        with mock.patch.object(pfs.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            agent = pfs.install_phonefs(self.directory)
        self.assertEqual(agent.name, "phonefs_win_FIXED.py")
        self.assertNotIn(pfs.MAILBOX_START, agent.read_text())
        self.assertIn(pfs.MAILBOX_DISABLED, agent.read_text())
        self.assertFalse((self.directory.parent / "do-not-extract.txt").exists())
        self.assertCountEqual((p.name for p in self.directory.iterdir()),
                              [pfs.AGENT_NAME, "package.zip"])

    def test_raw_download_can_fall_back_to_github_api(self):
        with mock.patch.object(pfs.urllib.request, "urlopen", side_effect=[
            urllib.error.URLError("raw host unavailable"), io.BytesIO(self.archive),
        ]) as download:
            pfs.install_phonefs(self.directory)
        self.assertEqual(download.call_args_list[0].args[0].full_url, pfs.PACKAGE_URL)
        self.assertEqual(download.call_args_list[1].args[0].full_url, pfs.PACKAGE_API_URL)

    def test_checksum_mismatch_never_installs_or_executes(self):
        with mock.patch.object(pfs.urllib.request, "urlopen", return_value=io.BytesIO(b"untrusted")):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                pfs.install_phonefs(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_verified_cache_is_reused_and_tampered_agent_is_repaired(self):
        (self.directory / "package.zip").write_bytes(self.archive)
        (self.directory / pfs.AGENT_NAME).write_text("raise RuntimeError('edited')")
        with mock.patch.object(pfs.urllib.request, "urlopen") as download:
            agent = pfs.install_phonefs(self.directory)
            self.assertEqual(agent.read_bytes(), pfs._agent_source(self.archive))
            pfs.install_phonefs(self.directory)
        download.assert_not_called()

    def test_original_agent_is_not_used_as_fixed_build(self):
        archive = make_archive(filename="phonefs_bundle_orig.py")
        with mock.patch.object(pfs, "PACKAGE_SHA256", hashlib.sha256(archive).hexdigest()):
            with self.assertRaisesRegex(RuntimeError, "exactly one phonefs_win_FIXED.py"):
                pfs._agent_source(archive)

    def test_unexpected_mailbox_hook_fails_closed(self):
        archive = make_archive(source="print('different build')\n")
        with mock.patch.object(pfs, "PACKAGE_SHA256", hashlib.sha256(archive).hexdigest()):
            with self.assertRaisesRegex(RuntimeError, "disable the public URL mailbox"):
                pfs._agent_source(archive)


class HelperTests(unittest.TestCase):
    def test_passwords_are_unique_and_meet_public_exposure_policy(self):
        passwords = {pfs.generate_password() for _ in range(100)}
        self.assertEqual(len(passwords), 100)
        for password in passwords:
            self.assertGreaterEqual(len(password), 24)
            for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^a-zA-Z0-9]"):
                self.assertRegex(password, pattern)

    def test_busy_phonefs_port_falls_back_without_using_screen_port(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            occupied = busy.getsockname()[1]
            with mock.patch.object(pfs, "PREFERRED_PORTS", (occupied,)):
                chosen = pfs.choose_port()
        self.assertNotEqual(chosen, occupied)
        self.assertNotEqual(chosen, 5000)
        self.assertGreater(chosen, 0)

    def test_agent_does_not_inherit_bot_or_github_secrets(self):
        secrets = {name: "test-secret" for name in
                   ("TELEGRAM_BOT_TOKEN", "ALLOWED_CHAT_ID", "GITHUB_TOKEN", "GH_TOKEN",
                    "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")}
        with mock.patch.dict(pfs.os.environ, secrets):
            env = pfs.child_environment()
        for name in secrets:
            self.assertNotIn(name, env)
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")

    def test_windows_cleanup_targets_only_own_process_tree(self):
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        with mock.patch.object(pfs.os, "name", "nt"), \
             mock.patch.object(pfs.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             mock.patch.object(pfs.subprocess, "run") as taskkill:
            pfs.terminate_process_tree(process)
        self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
        process.wait.assert_called()

    def test_login_readiness_requires_authentication_with_exact_password(self):
        with mock.patch.object(pfs.urllib.request, "urlopen", return_value=io.BytesIO(
            b'{"ok":true,"csrf":"test-csrf"}'
        )) as request:
            self.assertTrue(pfs._login_ready(8877, "Aa1-example-password"))
        self.assertIn(b'"password": "Aa1-example-password"', request.call_args.args[0].data)
        with mock.patch.object(pfs.urllib.request, "urlopen", return_value=io.BytesIO(b'{"ok":false}')):
            self.assertFalse(pfs._login_ready(8877, "wrong"))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.agent = self.directory / pfs.AGENT_NAME
        self.agent.write_text("# test agent\n")
        self.notify = mock.Mock()
        self.service = pfs.PhoneFSService(self.notify, cloudflared=lambda: str(self.agent),
                                         desktop_url=lambda: "https://screen-test.trycloudflare.com",
                                         install_dir=self.directory)
        self.addCleanup(self.service.stop)

    def online_worker(self, session):
        session.port = 8877
        session.url = "https://files-test.trycloudflare.com/"
        session.state = "online"
        self.ready.set()
        session.stop_event.wait(5)

    def start_fake_session(self):
        self.ready = threading.Event()
        patch = mock.patch.object(self.service, "_run", side_effect=self.online_worker)
        worker = patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(self.service.start(42))
        self.assertTrue(self.ready.wait(2))
        return worker

    def test_repeated_start_is_idempotent_and_status_has_both_links(self):
        worker = self.start_fake_session()
        session = self.service._session
        self.assertFalse(self.service.start(42))
        self.assertIs(self.service._session, session)
        self.assertEqual(worker.call_count, 1)
        text = self.service.status_text(42)
        for expected in (session.password, session.user_id, "8877", "5000",
                         "https://files-test.trycloudflare.com/",
                         "https://screen-test.trycloudflare.com", "password-only"):
            self.assertIn(expected, text)
        self.assertNotIn(session.password, repr(session))

    def test_another_chat_cannot_get_credentials_or_control_session(self):
        worker = self.start_fake_session()
        session = self.service._session
        status = self.service.status_text(999)
        self.assertNotIn(session.password, status)
        self.assertNotIn(session.url, status)
        self.assertFalse(self.service.start(999))
        self.assertFalse(self.service.stop(999))
        self.assertFalse(self.service.restart(999))
        self.assertEqual(worker.call_count, 1)
        self.assertFalse(session.stop_event.is_set())

    def test_restart_rotates_password_and_stop_hides_old_link(self):
        worker = self.start_fake_session()
        old = self.service._session
        self.ready.clear()
        self.assertTrue(self.service.restart(42))
        self.assertTrue(self.ready.wait(2))
        new = self.service._session
        self.assertIsNot(old, new)
        self.assertTrue(old.stop_event.is_set())
        self.assertNotEqual(old.password, new.password)
        self.assertEqual(worker.call_count, 2)
        self.service.stop(42)
        self.assertNotIn(new.password, self.service.status_text(42))
        self.assertIsNone(new.url)

    def test_cancelled_install_cannot_launch_or_announce_a_stale_session(self):
        entered, release = threading.Event(), threading.Event()

        def install(_):
            entered.set()
            release.wait(3)
            return self.agent

        with mock.patch.object(pfs, "install_phonefs", side_effect=install), \
             mock.patch.object(pfs.subprocess, "run") as password_setup:
            self.service.start(42)
            self.assertTrue(entered.wait(2))
            old = self.service._session
            # Model a download that cannot be interrupted immediately by stop.
            with mock.patch.object(old.thread, "join"):
                self.service.stop(42)
            release.set()
            old.thread.join(timeout=2)
            self.assertFalse(old.thread.is_alive())
        password_setup.assert_not_called()
        self.assertEqual(old.state, "stopped")
        self.assertFalse(any("Generated password:" in call.args[1] for call in self.notify.call_args_list))

    def test_start_can_replace_a_cancelled_background_install(self):
        self.start_fake_session()
        old = self.service._session
        old.stop_event.set()
        with mock.patch.object(old.thread, "is_alive", return_value=True):
            self.assertTrue(self.service.start(42))
        self.assertIsNot(self.service._session, old)

    def test_password_is_set_via_stdin_in_a_fresh_config_not_process_arguments(self):
        session = pfs._Session(chat_id=42)
        self.service._session = session
        completed = subprocess.CompletedProcess([], 0, "Password updated", "")
        with mock.patch.object(pfs, "install_phonefs", return_value=self.agent), \
             mock.patch.object(pfs.subprocess, "run", return_value=completed) as password_setup, \
             mock.patch.object(pfs, "choose_port", return_value=8877), \
             mock.patch.object(self.service, "_attempt", return_value=(0, True)):
            self.service._run(session)
        command = password_setup.call_args.args[0]
        self.assertIn("set-password", command)
        self.assertNotIn("--password", command)
        self.assertNotIn(session.password, " ".join(command))
        self.assertEqual(password_setup.call_args.kwargs["input"], session.password + "\n")
        self.assertIn(str(session.config_dir), command)
        self.assertNotEqual(session.config_dir, self.directory)
        self.assertFalse(session.config_dir.exists())

    def test_announce_only_after_url_and_successful_login_and_redact_logs(self):
        session = pfs._Session(chat_id=42, port=8877, config_dir=self.directory / "config")
        self.service._session = session
        process = mock.Mock(pid=12345)
        process.stdout = io.StringIO(
            f"PASSWORD: {session.password}\n\x1b[32m| https://files-test.trycloudflare.com/ |\x1b[0m\n"
        )
        process.poll.return_value = None
        messages = []

        def notified(chat_id, text):
            messages.append((chat_id, text))
            if "PhoneFS online" in text:
                session.stop_event.set()

        self.service.notify = notified
        captured = io.StringIO()
        with mock.patch.object(pfs.subprocess, "Popen", return_value=process) as launch, \
             mock.patch.object(pfs, "terminate_process_tree"), \
             mock.patch.object(pfs, "_login_ready", return_value=True) as login, \
             redirect_stdout(captured):
            code, online = self.service._attempt(session, self.agent, "cloudflared", 1)
        self.assertEqual((code, online), (0, True))
        login.assert_called_once_with(8877, session.password)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][0], 42)
        self.assertIn(session.password, messages[0][1])
        self.assertIn("https://files-test.trycloudflare.com/", messages[0][1])
        self.assertIn("https://screen-test.trycloudflare.com", messages[0][1])
        self.assertNotIn(session.password, captured.getvalue())
        self.assertIn("[redacted]", "\n".join(session.logs))
        self.assertNotIn("https://files-test.trycloudflare.com", captured.getvalue())
        self.assertNotIn(session.password, " ".join(launch.call_args.args[0]))
        self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_rejected_password_is_not_announced_and_retry_uses_http2(self):
        session = pfs._Session(chat_id=42, port=8877, config_dir=self.directory / "config")
        self.service._session = session
        process = mock.Mock(pid=12345)
        process.stdout = io.StringIO("https://files-test.trycloudflare.com/\n")
        process.poll.side_effect = [None, 2]
        with mock.patch.object(pfs.subprocess, "Popen", return_value=process) as launch, \
             mock.patch.object(pfs, "terminate_process_tree"), \
             mock.patch.object(pfs, "_login_ready", return_value=False), \
             redirect_stdout(io.StringIO()):
            code, online = self.service._attempt(session, self.agent, "cloudflared", 2)
        self.assertEqual((code, online), (2, False))
        self.notify.assert_not_called()
        self.assertEqual(launch.call_args.args[0][-2:], ["--protocol", "http2"])

    def test_graceful_browser_close_is_not_reported_as_a_startup_failure(self):
        session = pfs._Session(chat_id=42, port=8877, config_dir=self.directory / "config")
        self.service._session = session
        process = mock.Mock(pid=12345)
        process.stdout = io.StringIO("https://files-test.trycloudflare.com/\n")
        process.poll.side_effect = [None, 0]
        with mock.patch.object(pfs.subprocess, "Popen", return_value=process), \
             mock.patch.object(pfs, "terminate_process_tree"), \
             mock.patch.object(pfs, "_login_ready", return_value=True), \
             redirect_stdout(io.StringIO()):
            code, online = self.service._attempt(session, self.agent, "cloudflared", 1)
        self.assertEqual((code, online), (0, True))
        self.assertIsNone(session.error)
        self.assertIn("PhoneFS online", self.notify.call_args.args[1])

    def test_setup_error_is_reported_without_affecting_desktop(self):
        session = pfs._Session(chat_id=42)
        self.service._session = session
        with mock.patch.object(pfs, "install_phonefs", side_effect=RuntimeError("download failed")):
            self.service._run(session)
        self.assertEqual(session.state, "failed")
        message = self.notify.call_args.args[1]
        self.assertIn("download failed", message)
        self.assertIn("Screen sharing is unchanged", message)
        self.assertNotIn(session.password, message)


if __name__ == "__main__":
    unittest.main()
